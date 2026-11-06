# Research & Design Decisions: festival-ops-dashboard

## ギャップ分析

### 分析の前提

- 対象: requirements.md の R1〜R9 と、`gitops/manifests/prod/ops-dashboard/` の現行コード (main)。
- PR #432 (`fix/festival-monitoring-prep`、未マージ) の差分を前提に含める。
  - collector が kubelet の `/stats/summary` から全ノードのディスク使用率を取る。項目キーは `disk:<node>`。
  - collector の ClusterRole に `nodes/stats` の get を足す。
  - CMS DB の名前の不整合を `prod/directus-db` に直す。
  - UptimeRobot に「Payload CMS API (DB 経由)」の KEYWORD モニターを足し、IdP モニターの名前を Zitadel に直す。
- PR #433 (prod-node-4 の追加) は #434 で revert 済み。`dashboard.toml` の `[[servers]]` は prod-node-1〜3。node-3 の置き換えは検討が続いており、ノード構成は固定でない前提で扱う。
- 開催日は 2026-11-14〜15。分析日 (2026-11-02) から開催初日まで 12 日。リハーサル期間を考えると、実装と本番反映に使えるのは実質 10 日弱。
- 分析は情報と選択肢の提示にとどめる。方式の決定は設計フェーズで行う。

### 1. 現状の調査

#### collector の構造 (再利用できるもの)

| 要素 | 場所 | 内容 | 当日監視画面での使い道 |
|---|---|---|---|
| 情報源の登録 | `app/sources/__init__.py` の `MODULES` | 1 行 1 モジュール。各モジュールが `SOURCES = (Source(id, interval, fetch[, render]),)` を持つ | 新しい情報源を同じ形で足せる |
| 取得ループ | `app/main.py` `Collector` | 情報源ごとに 1 スレッドで、`interval` ごとに取得する。タイムアウトと例外は `Status.ERROR` に変換し、直前の items と `last_success_at` を保つ | 取得失敗の扱い (R2.4) をそのまま使える |
| 結果モデル | `app/model.py` | `Status` は OK / WARN / CRIT / ERROR / STALE / EMPTY。`Item(key, label, status, values, note)`。`SourceResult.fetched_at` を持つ | 3 段階の判定と「取得遅延」「取得失敗」を表せる (R2.3・R2.4・R3.1) |
| 宣言ファイル | `dashboard.toml` と `app/config.py` | 起動時に厳格に検証する。未知のキーや未知の `source_id` は起動失敗になる | 閾値・期待値・期間の宣言先になる (R3.3・R3.4)。ただし parser の拡張が要る |
| HTTP | `app/http_client.py` | 標準ライブラリだけで書かれている。`post_json` もある。エラー文に URL のクエリと本文を含めない | GraphQL・AE SQL・ODPT・Discord で使える。Discord の 204 (空の本文) は `_json` が失敗するため、`request()` を直接使う |
| k8s | `app/k8s.py` | ServiceAccount トークンで API を読む。PR #432 で `kubelet_stats()` が加わる | ノード・Pod・CNPG を読める |
| 永続化 | `app/store.py` | SQLite (WAL)。`CREATE TABLE IF NOT EXISTS` でテーブルを足せる | 通知の状態 (最後の判定・最後の通知時刻) を保持できる |
| 描画 | `app/render/page.py`・`render/sections/` | サーバー側で HTML を組み立てる。節は `load_sections(modules)` で並びを指定できる。`badge()` は色と記号と文字を併記する | 色と文字の両方による表示 (R3.1) を満たす部品がある |
| Web | `main.py` `make_web_server` | `/admin/` と `/admin/healthz` だけを返す。それ以外は 404 | `/admin/festival` の経路を足す |
| 配布 | `kustomization.yaml` の configMapGenerator | ファイルを 1 つずつ列挙する。内容が変わると Pod が作り直される | 新しいファイルは列挙への追加が要る (列挙漏れは本番だけで import に失敗する) |
| 認可 | `portal/nginx.conf` の `location /admin/` | `auth_request /_auth/admin` (allowed_groups=admin)。`proxy_pass $collector` で URI をそのまま渡す。ページ遷移以外の未認証の要求には 401 を返す | `/admin/festival` は nginx を変えずに同じ認可がかかる (R1.2・R1.3)。JS の fetch はセッション切れを 401 で検知できる (R2.5) |

規約と制約:

- collector は Python 3.13 slim + 標準ライブラリだけで動かす (ops-dashboard の design.md の Technology Stack)。コードは ConfigMap で配り、イメージのビルドや `pip install` の工程は無い。
- 依存の向きは `config` → `sources/*` → `store` → `render` → `server`。
- テストは `app/tests/` の unittest。CI では実行されず、手元で実行する。
- ops-dashboard の design.md の境界:
  - Non-Goals に「新たなアラート通知。運用ダッシュボードは閲覧専用」がある。
  - Allowed Dependencies に「collector は外部 API に対して GET (および GraphQL の参照クエリ・S3 ListObjectsV2) 以外を発行しない」がある。
  - R8 の Discord 通知 (POST) は、この 2 点と衝突する。本仕様の設計で境界の更新を明示する必要がある。
- collector は hostPath と local-path PVC を使うため、`prod-node-1` に固定されている (`nodeSelector`)。
- リソースは requests 64Mi / limits 160Mi。2026-11-02 時点の実測は約 44Mi。

#### 既存の画面に関わる注意点

- `/admin/` の「要確認」の集計 (`page.py` の `_flagged`) は、snapshot に含まれる全情報源を対象にする。当日監視用の情報源を同じ `Collector.results` に入れると、その異常が `/admin/` の要確認にも出る。これは R1.5 (既存の表示を変えない) に反する。対策は 2 つ考えられる。
  - `/admin/` の集計対象を節の `source_ids` に限る。
  - 当日監視用の結果を別の名前空間に持つ。
- `security.falco` は描画のたびに評価する情報源 (`interval=None`) で、表示期間のクエリに依存する。R8 は画面を開いていなくても通知することを求めるため、当日監視の判定は描画とは別に、定期的に評価する必要がある。

#### ノード構成の扱い

- `[[servers]]` の役割は 2 つある。
  - `billing.hetzner` (`plan.server_diff`): Hetzner に存在すべきサーバーの宣言。
  - `node.resources`: k8s のノードを絞り込むフィルタ。宣言に無いノードは表示されない。
- 宣言済みでクラスタに無いノード (適用前の node-4 など) は、`node.resources` では項目が作られず、何も表示されない。欠けていることが判定に出ない。
- 既存の `node.resources` は、ノードの Ready 状態を判定しない。CPU 使用率は常に OK で、閾値が無い。
- R6.4 (全ノード) を満たす方法は 2 つある。
  - k8s のノード一覧を正とする。
  - `[[servers]]` を期待集合として「期待したノードが Ready で存在するか」を判定する。
  - どちらにするかは設計で決める。node-3 を除いて node-4 を加える作業中は、両者が一時的に食い違う。

### 2. 要件とアセットの対応

凡例:
- **既存**: 現行の取得と判定をほぼそのまま使える。
- **拡張**: 既存の情報源や部品に手を加える。
- **新規**: 新しく作る。
- タグ: Missing (欠けている機能)、Unknown (設計で調べる)、Constraint (既存の制約)。

#### R1 当日監視画面の提供とアクセス制御

| AC | 判定 | 根拠とギャップ |
|---|---|---|
| 1.1 `/admin/festival` | 新規 (Missing) | collector の Web が `/admin/` だけを返す。経路の追加が要る。nginx は `location /admin/` の前方一致で転送するため変更は要らない |
| 1.2・1.3 認可 | 既存 | nginx の `auth_request /_auth/admin` が `/admin/` 配下全体にかかる。拒否時の 403 ページと、未認証時のログインへの誘導 (ページ遷移は 302、fetch は 401) も同じ |
| 1.4 不要情報を出さない | 新規 | 節の並びを当日監視画面専用にする。`load_sections(modules)` は並びを指定できるが、`page.render` のタイトル・要確認・ボタンは既存の画面向けに固定されている |
| 1.5 既存を変えない | Constraint | 上記の「要確認」の集計範囲の問題がある。ポータルの導線 (`admin-overlay.yaml`) を足すかは任意で、要件には無い |

#### R2 自動更新と情報の鮮度

| AC | 判定 | 根拠とギャップ |
|---|---|---|
| 2.1 30 秒以内の自動更新 | 新規 (Missing) | 現状は meta refresh も JS も無く、手動の再読み込みボタンだけ。取得間隔も揃っていない (UptimeRobot・Healthchecks は 5 分、cluster は 2 分、tunnel は 2 分、Cloudflare の課金は 1 時間)。画面の更新間隔と取得間隔は別に決める必要がある |
| 2.2 取得時刻の表示 | 拡張 | `SourceResult.fetched_at` と `last_success_at` がある。表示は情報源単位で、項目単位には持っていない。Cloudflare GraphQL は「取得時刻」と「データの時刻」(1〜2 分遅れ) がずれる |
| 2.3 取得遅延 | 拡張 (Missing) | `Status.STALE` はあるが、「項目ごとの鮮度の上限」の宣言と判定は無い |
| 2.4 取得失敗 | 既存 | 情報源ごとに `Status.ERROR` になり、他の情報源の取得は続く |
| 2.5 更新失敗の明示 | 新規 (Unknown) | meta refresh では、失敗するとページがエラーページやログイン画面に置き換わり、「古い内容を表示中」と示せない。JS の fetch なら、401 や 5xx、通信断を検知して、表示を保ったまま警告を出せる。oauth2-proxy の `--cookie-expire=12h` により、常時表示でも 12 時間ごとの再ログインが要る |

#### R3 状態判定と表示

| AC | 判定 | 根拠とギャップ |
|---|---|---|
| 3.1 3 段階と色・文字 | 既存 | `badge()` が記号・文字・色を併記する。STALE・ERROR を「取得遅延」「取得失敗」として別表示するのも既存どおり |
| 3.2 区分の順序 | 新規 | 来場者影響 → 機能別 → 容量 → 基盤・セキュリティの 4 区分の節。既存の節の分け方 (情報源別) とは異なる |
| 3.3・3.4 宣言による閾値 | 拡張 (Constraint) | `dashboard.toml` に宣言の場所を足す。`config.py` は未知のキーで起動に失敗する設計のため、`Thresholds` か新しい表 (例: `[festival]`) の parser と検証を足す。宣言を変えると ConfigMap のハッシュが変わり Pod が作り直されるため、3.4 は「同期と再起動で反映」になる |
| 3.5 最上部の件数と一覧 | 拡張 | `page._summary` の部品を使える。対象は当日監視の項目に限る |
| 3.6 現在値と閾値 | 拡張 | `Item.values` に現在値はある。閾値を項目に載せる規約は無い |
| 3.7 参照先リンク | 新規 | 項目ごとのリンクの宣言が要る。既存の節のアンカー (`#sec-*`) は使える |

#### R4 来場者影響

| AC | 判定 | 根拠とギャップ |
|---|---|---|
| 4.1 Worker の requests・エラー率・リソース超過・CPU 分位 | 新規 | `workersInvocationsAdaptive` は現在のトークン (account スコープ) で取れる。`billing_cloudflare.py` に呼び出しの実装がある (`_raise_on_errors` と `post_json`)。scriptName で絞り、status ごとの件数を取る。リソース超過を表す status の値は Unknown |
| 4.2・4.3 CMS の件数・5xx 比率・origin 応答時間 | 新規 | account スコープの `httpRequestsAdaptiveGroups` を、`clientRequestHTTPHost` と `requestSource` で絞る。edgeWorkerCacheAPI の 504 は除外する。試作クエリは scratchpad の `inc/q*.graphql` にある |
| 4.4 `/api/media/` の 4xx・5xx | 新規 | 同じ dataset で path を絞る。前方一致のフィルタ名は Unknown |
| 4.5 UptimeRobot | 既存 | `monitor.uptimerobot` の項目がそのまま使える。取得間隔は 5 分 |
| 4.6 遅延による過小評価の回避 | 新規 | Cloudflare の集計は 1〜2 分遅れるため、時間窓の終端を現在より前にずらす (例: [now−7m, now−2m])。窓の幅は設計で決める |

#### R5 機能別の健全性

| AC | 判定 | 根拠とギャップ |
|---|---|---|
| 5.1 カメラごとの最終受信時刻 | 新規 (web 側に口が要る) | `parking_cameras.last_captured_at` は CMS 内部の値で、コレクションは非公開。collector から読む口が無い |
| 5.2 AI 判定の成功・失敗・見送りの件数と最終成功時刻 | 新規 (web 側に口が要る) | `parking_snapshots` の `judge_status` と `judged_at` は、役員 (`executiveOnlyField`) だけが読める。users に API キー認証は無い。前例は `cms/src/endpoints/parking-ingest.ts` の Bearer 方式 (カメラのトークンをハッシュで照合) |
| 5.3 サイネージの接続数 | 新規 | `durableObjectsPeriodicGroups.max.activeWebsocketConnections` で取れる。ハブは固定名の 1 インスタンスで、namespaceId で絞れる。dev と PR プレビューのサイトも本番のハブに繋ぐため (`signage-hub/wrangler.toml` のコメント)、接続数に本番以外の接続が混ざり得る。ハブには公開 URL も `[observability]` も無いが、5.3 には不要 |
| 5.4 地震リレーの定期起動 | 新規 | `durableObjectsInvocationsAdaptiveGroups` で QuakeRelay の jsrpc の呼び出しを分単位で数える。`workersInvocationsScheduled` は 2026-10-27 以降の記録が欠けているため使わない |
| 5.5 上流ごとの接続・最終受信・Discord の成否 | 新規 | 地震リレー Worker の `GET /status` (Bearer `SIGNAGE_QUAKE_TOKEN`) が `channels.*.connected / lastReceivedAt / receiving`、`unreachable`、`discord.lastSuccessAt / lastFailureAt / disabled` を返す。ただし同じトークンで `POST /dismiss` (状態変更) もできる (Constraint、R9.3) |
| 5.6 スタンプラリーの失敗 | 新規 | AE `aramakisai_stamp_metrics`。blob3 が outcome、double7 が d1Failed。prod と dev が同じ dataset に書くため、blob1 (`STAMP_ENV`) で本番に絞る必要がある。レート制限を表す outcome の値は Unknown。SQL API の呼び出しは `billing_cloudflare.py` に実装がある |
| 5.7 バスの公開フィード | 新規 | ODPT の公開フィード (鍵は不要) を collector が直接取る。`header.timestamp` で鮮度を判定できる。web 側は 185 行の自前デコーダ (`frontend/src/lib/gtfs-realtime.ts`) で解析しており、依存ライブラリを使っていない。`FeedMessage.header` (field 1) の `timestamp` (field 3, varint) を読むだけなら、標準ライブラリで数十行で書ける。protobuf ライブラリの追加は、イメージのビルドか initContainer での `pip install` が要り、現行の配布方式に無い |
| 5.8 期間外の扱い | 新規 (Constraint) | 開催期間と時間帯の宣言が要る。駐車場の監視時間は CMS の global `site_settings.parking_monitor` (管理画面から変更できる。既定は 09:00〜17:30) が正本。`dashboard.toml` に重複して宣言すると、ずれる恐れがある |

#### R6 容量

| AC | 判定 | 根拠とギャップ |
|---|---|---|
| 6.1 CMS Pod の Ready・再起動・OOM・CPU 対 limit | 拡張 | Pod の再起動と OOM は `cluster.workloads` に判定がある (閾値を超えた Pod だけを出す)。宣言数は Deployment の `spec.replicas` を読むか (RBAC に `apps/deployments` が無い) 宣言で持つ。Pod の CPU は `metrics.k8s.io` の pods が要るが、RBAC は nodes だけ (Missing) |
| 6.2 DB primary の CPU・接続数 | 新規 | primary は CNPG の Cluster `status.currentPrimary` で分かる (RBAC はある)。CPU は pods のメトリクスが要る (Missing)。接続数と上限は instance の `:9187/metrics` で取れる (`cnpg_backends_total`、`cnpg_pg_settings_setting{name="max_connections"}`)。認証無しの HTTP で応答することを確認した |
| 6.3 replica の遅延 | 新規 | primary の `cnpg_pg_stat_replication_replay_lag_seconds{application_name=…}`、または各 replica の `cnpg_pg_replication_lag` で取れる。CNPG の Cluster status には遅延が無い |
| 6.4 全ノードの Ready・CPU・メモリ・ディスク | 拡張 | PR #432 で全ノードのディスクが取れる。Ready の判定と CPU の閾値は無い (Missing)。ノードは `[[servers]]` で絞っている (上記) |
| 6.5 Tunnel | 既存 | `connect.tunnel` (2 分間隔) の状態と接続数 |

DB のメトリクスへの経路 (6.2・6.3):

- NetworkPolicy:
  - prod Namespace にあるのは `mail-agent-ingress` だけで、DB の Pod への ingress を制限するものは無い。
  - collector の NetworkPolicy は Ingress だけで、egress を制限しない。
  - Cilium に既定の拒否は無い。
  - したがって、collector から Pod IP の 9187 番に直接届く見込み。実際に届くかは未検証。
- RBAC:
  - Pod IP の取得には既存の pods の list で足りる。
  - API サーバー経由 (`pods/proxy`) なら NetworkPolicy に依存しない。ただし、collector の RBAC が意図して外している proxy 系の権限を足すことになる。
- 9187 の応答は、手元から API サーバーの proxy 経由で確かめた。collector の Pod から直接は確かめていない。

#### R7 基盤とセキュリティ

| AC | 判定 | 根拠とギャップ |
|---|---|---|
| 7.1 `dr-incident`・`infra-alert` | 既存 | `ci.github` が `incident.<label>.<n>` の項目を作る (dr-incident は CRIT、infra-alert は WARN)。取得間隔は 10 分 |
| 7.2 Falco の優先度別件数 | 拡張 | `falco_events` テーブルに受信記録がある。既存の `security.falco` は描画時に評価する。通知のためには定期的に評価する必要がある (上記) |
| 7.3 IdP と CMS DB 経由の応答 | 既存 (PR #432 に依存) | UptimeRobot の「Zitadel IdP」と「Payload CMS API (DB 経由)」で判定できる。後者は PR #432 のマージと `terraform apply` の後に存在する |
| 7.4 Healthchecks.io | 既存 | `monitor.healthchecks` |
| 7.5 内部アドレスの BAN | 拡張 | `mail.fail2ban` は BAN 中の IP の一覧を持ち、すべて OK で返す。内部アドレスを照合する判定が要る。照合先は、ノードの Ready 一覧と同じ k8s の `status.addresses`、Pod の CIDR、tailnet の範囲。アドレスを直書きしない規約 (tech.md の設計上の禁止事項) とのすり合わせが要る。画面には IP を出さず件数だけにする (R9.5) |

#### R8 Discord 通知

| AC | 判定 | 根拠とギャップ |
|---|---|---|
| 8.1〜8.3 異常・回復・再通知の抑止 | 新規 (Missing) | collector には外向きの通知の仕組みが無い (閲覧専用)。<br>・状態遷移の検出と再通知の間隔に要る状態は SQLite に持てる。<br>・Webhook の Secret は Infisical の `DISCORD_OPS_WEBHOOK_URL`。cms-secrets が同じキーを駐車場カメラの停止通知に使っている。`collector-secrets` への追加が要る |
| 8.4 情報源単位にまとめる | 新規 | 情報源の `Status.ERROR` を単位にすれば、まとめられる |
| 8.5 送信失敗の表示 | 新規 | 送信結果を画面の項目として出す |
| 8.6 既存の通知を変えない | Constraint | 同じ Webhook を Falcosidekick・CMS のカメラ監視・infra-health-check・dr-trigger と共用する。Discord の Webhook ごとのレート制限 (既知の挙動として、高頻度時に一部が 400 になる) を共有するため、送信量を抑える設計が要る |

#### R9 運用上の制約

| AC | 判定 | 根拠とギャップ |
|---|---|---|
| 9.1 時系列の保存基盤を置かない | 既存 | collector の SQLite と外部の集計で足りる |
| 9.2 レート制限 | Unknown | Cloudflare GraphQL・AE SQL・ODPT・UptimeRobot の公開値を、取得間隔と照らす必要がある。ODPT は web 側が利用ガイドラインに合わせて 30 秒間隔にしている。Cloudflare は `billing.cloudflare` (1 時間ごと) と同じトークンの枠を共有する |
| 9.3 読み取りのみ | Constraint | `SIGNAGE_QUAKE_TOKEN` は `/dismiss` を許すため、collector が状態を変えうる資格情報を持つことになる。CMS に足す読み取りの口は、GET だけを受けるトークンにできる |
| 9.4 シークレットを出さない | 既存 | `safe_error` と `HttpError` がクエリと本文を落とす。Discord の Webhook URL 自体がシークレットのため、送信失敗のログで URL を出さない (`HttpError` は path を含む。Webhook の path にトークンが入る) 点に注意が要る |
| 9.5 個人を識別できる情報を出さない | Constraint | GraphQL のクエリで `clientIP` などの次元を選ばない。fail2ban は件数だけを出す |
| 9.6 メモリの明示 | 拡張 | limits 160Mi、実測は約 44Mi (2026-11-02)。追加後に `make kubectl ARGS="top pod -n ops-dashboard"` で実測する |
| 9.7 停止しても他に影響しない | Constraint | 案 A・C では、`/admin/` と同じプロセスで動く。当日監視の取得や描画の例外が `/admin/` を巻き込まない分離が要る。collector は `prod-node-1` に固定されており、node-1 の障害時は当日監視画面と通知も止まる (既存の DR Trigger は動き続ける) |

### 3. web 側 (aramakisai-web) の読み取りの口

| 対象 | 現状 | 必要なもの | 前例と配布 |
|---|---|---|---|
| 駐車場 (R5.1・R5.2) | `parking_snapshots` と `parking_cameras` は非公開か役員限定。users に API キーは無い | CMS に GET の endpoint を足す。返すのは、カメラごとの最終受信時刻、時間窓ごとの judge_status の件数、最終成功時刻、監視時間 | Bearer の照合は `parking-ingest.ts` が前例 (共有トークンなら env と定数時間の比較)。トークンは Infisical に置き、`cms-secrets` と `collector-secrets` の両方の ExternalSecret に載せる。<br>配布は web の PR → CMS イメージ → infra の bump PR (`cms-image-<sha>`) → ArgoCD。リードタイムは最短でも 1 日程度 |
| サイネージハブ (R5.3) | 公開 URL が無く、`/api/signage/*` は CF Access (サービストークン) で保護されている | 5.3 は DO の periodic で満たせるため、口は必須ではない。要件の Boundary Context で In scope に挙がっている「サイネージハブの状態」を、接続数以上に求めるかは要件の確認事項 | 足すなら site Worker の `/api/signage/*` に GET を足すことになり、CF Access のサービストークンを collector に配る必要がある |
| 地震リレー (R5.5) | `/status` がある | 読み取り専用のトークンを分けるかどうか (R9.3) | 分けるなら quake Worker の改修 (`http.ts` の `authorized`) と Wrangler の secret 追加が要る |

代替案: collector から CMS の DB を読み取り専用ロールで直接読む。web 側の変更は要らない。ただし、次の問題がある。

- Payload のスキーマ (テーブル名や列名) に直接依存する。
- CNPG の managed role と Secret の追加が要る。
- Boundary Context の「aramakisai-web の既存の認証方式に従う」から外れる。

### 4. 実装方式の選択肢

#### 案 A: 既存の collector を拡張する

既存の collector に、情報源と当日監視画面の描画を足す。

- **変更するもの**:
  - `sources/` に新しい情報源を足す。例は `cf_traffic`、`cf_functions`、`odpt_bus`、`cnpg_metrics`、`quake_relay`、`parking`。
  - `config.py`・`dashboard.toml` に `[festival]` の宣言 (期間・閾値・期待値・鮮度の上限・リンク) を足す。
  - `main.py` に `/admin/festival` とデータの経路を足す。
  - `render/` に当日監視画面のページと節を足す。
  - 通知の評価器を定期スレッドとして足す。
  - `store.py` に通知状態のテーブルを足す。
  - `rbac.yaml` に `metrics.k8s.io` の pods の get・list を足す。
  - `external-secret.yaml` に `DISCORD_OPS_WEBHOOK_URL`、`SIGNAGE_QUAKE_TOKEN`、CMS 読み取りトークンを足す。
  - `kustomization.yaml` のファイル列挙を足す。
  - `/admin/` の要確認の集計範囲を限定する (R1.5)。
- **互換性**: `KNOWN_SOURCE_IDS` と `MODULES` に追加するだけで、既存の情報源の interface は変わらない。`page._flagged` の集計範囲を変えないと、既存の画面に当日の項目が混ざる。
- **トレードオフ**:
  - ✅ 取得ループ・失敗の扱い・認可・Secret・配布の仕組みをすべて使い回せる。nginx の変更は要らない。
  - ✅ PR #432 のディスク取得や既存の UptimeRobot・Healthchecks・Falco・fail2ban・GitHub の各情報源を直接使える。
  - ❌ collector の責務が「閲覧専用」から「通知も行う」に広がり、ops-dashboard の design.md の Non-Goals と Allowed Dependencies を更新する必要がある。
  - ❌ 当日監視の不具合 (例外・メモリ・スレッド) が `/admin/` と同じプロセスに及ぶ (R9.7 の分離を設計で担保する必要がある)。
  - ❌ `config.py` の厳格な検証と ConfigMap の列挙に、毎回手を入れる。

#### 案 B: 別のコンポーネントを作る

例: ops-dashboard Namespace に `festival-monitor` の Deployment を置く。

- **内容**: 当日監視専用のプロセスを作り、portal の nginx から `/admin/festival` をそこへ転送する。コードは collector の部品 (`http_client`、`k8s`、`model`) を複製するか共有する。
- **トレードオフ**:
  - ✅ 障害の分離 (R9.7) と既存を変えないこと (R1.5) が構造で保証される。
  - ✅ 開催後に Deployment ごと止めるか消せる。
  - ❌ 既存の情報源 (UptimeRobot・Healthchecks・GitHub・fail2ban・Falco・Tunnel・ディスク) を二重に取得するか、collector から何らかの形で受け取る必要がある。二重取得は外部 API の枠 (R9.2) を倍に使う。Falco は collector の SQLite にしか無い。
  - ❌ 次の追加が要る: nginx の location、NetworkPolicy、ServiceAccount と RBAC、ExternalSecret、configMapGenerator の一式、Falco の除外 (k8s API への接続の誤検知、tech.md の除外の組)。
  - ❌ 残り日数に対して、新しく作る範囲が広い。

#### 案 C: 組み合わせ

- **取得**: 案 A と同じく collector に情報源を足し、結果を `Collector.results` で共有する。
- **当日監視層**: 評価・描画・通知は collector 内の独立したモジュールにする (例: `festival/`)。
  - 当日監視層は snapshot を読むだけにする。
  - `/admin/` の描画と要確認の集計は、既存の節の `source_ids` に限る。
  - 通知スレッドの例外を `/admin/` に波及させない。
- **web 側**: CMS の読み取り専用 endpoint は別の PR (aramakisai-web) で並行して進める。
- **段階的な導入**:
  - 第 1 段: Cloudflare・k8s・既存の情報源・ODPT・地震リレー・CNPG のメトリクス。web の変更が不要な範囲で、画面と自動更新を作る。
  - 第 2 段: Discord 通知。
  - 第 3 段: 駐車場 (CMS の口ができ次第)。
  - 各段で `[festival]` の宣言を足す。宣言の無い項目は表示しない作りにすると、段階的に有効にできる。
- **トレードオフ**:
  - ✅ 取得を共有して API の枠と実装を節約しつつ、当日監視の責務を分けられる。
  - ✅ web 側の PR とリードタイムを並行できる。
  - ❌ プロセスは共有するため、メモリやスレッドの枯渇は分離されない。R9.7 は「例外の分離」までになる。
  - ❌ モジュールの境界 (snapshot の読み取り専用の契約) を設計で明文化する必要がある。

#### 工数とリスク

| 範囲 | 工数 | リスク | 理由 |
|---|---|---|---|
| 当日監視画面の枠 (経路・節・要確認・JS の自動更新・鮮度表示) | M | Medium | 既存の描画部品は使えるが、JS による更新と更新失敗の表示は前例が無い |
| Cloudflare GraphQL・AE の情報源 (R4.1〜4.4・4.6、R5.3・5.4・5.6) | M | Medium | 呼び出しの実装とトークンはある。時間窓・status の値・レート制限が未確定 |
| ODPT (R5.7) | S | Low | 公開フィードで、header.timestamp だけを標準ライブラリで読む |
| 地震リレー (R5.5) | S | Medium | API はある。トークンの権限 (R9.3) の扱いを決める必要がある |
| k8s・CNPG の容量 (R6.1〜6.4) | M | Medium | metrics の pods の RBAC 追加、9187 への経路の実測、ノード構成が変わる途中であること |
| 既存の情報源の当日監視向け判定 (R4.5、R6.5、R7.1〜7.5) | S | Low | 既存の結果を読み直すだけ。fail2ban の内部アドレスの照合だけが新しい |
| Discord 通知 (R8) | M | Medium | 状態遷移・再通知・まとめ・送信失敗の表示。共用の Webhook のレート制限と、design.md の境界の更新 |
| CMS の読み取りの口 (R5.1・5.2、web 側) | M | High | 別リポジトリで、イメージの bump を経て本番に入るまでのリードタイムがある。期間内の CMS の変更凍結の方針次第で間に合わない恐れがある |
| 全体 (案 C) | L | Medium〜High | 合計で 1〜2 週間規模で、開催まで 12 日。段階的な導入と、駐車場を切り離せるかが鍵 |

### 5. 設計フェーズへの推奨 (情報として)

推奨は案 C。理由は次の 3 点。

- 既存の情報源と配布の仕組みを最大限使い回せる。
- R1.5 と R9.7 をモジュールの境界で担保できる。
- web 側の変更を切り離して並行できる。

設計で決めることは次のとおり。

- **K-1**: 当日監視の結果を `/admin/` の要確認から外す方法。集計を `source_ids` に限るか、結果の名前空間を分けるか。
- **K-2**: 画面の更新方式。JS の fetch で HTML の断片か JSON を取るか、meta refresh か。R2.5 を満たすのは前者だけ。
- **K-3**: 判定と通知を描画から切り離した定期評価にする。評価間隔、再通知の間隔、まとめの単位も決める。
- **K-4**: `[festival]` の宣言の形。次の項目を含む。
  - 期間・時間帯
  - 閾値・期待値 (CMS の宣言数)
  - 項目ごとの鮮度の上限
  - リンク
  - `[[servers]]` との関係
- **K-5**: ノードの期待集合を何で決めるか。k8s のノード一覧か `[[servers]]` か。node-4 の追加と node-3 の除外の途中で、どう判定するか。
- **K-6**: ops-dashboard の design.md の Non-Goals「新たなアラート通知」と、Allowed Dependencies「GET 以外を発行しない」の更新。
- **K-7**: 駐車場の監視時間の正本。CMS の `site_settings.parking_monitor` を CMS の口から受け取るか、宣言で重複して持つか。
- **K-8**: 地震リレーの資格情報。現行の `SIGNAGE_QUAKE_TOKEN` を使うか、読み取り専用のトークンを分けるか。
- **K-9**: 駐車場の口の方式。CMS の endpoint か、DB への直接の読み取りか。web 側の変更の締切 (開催前の CMS の変更凍結の有無) も決める。

### 6. 設計で調べる項目 (Research Needed)

- **RN-1**: Cloudflare GraphQL Analytics API と AE SQL API の公開されたレート制限 (クエリ数と時間窓の上限)。`billing.cloudflare` と合わせた 1 分あたりの問い合わせ数で収まるか。
- **RN-2**: `workersInvocationsAdaptive` の `status` 次元が取りうる値。リソース超過 (CPU・メモリ・サブリクエスト等) を数えるには、どの値か `sum` のどのフィールドを使うか。
- **RN-3**: `httpRequestsAdaptiveGroups` (account スコープ) で、`/api/media/` の前方一致に使えるフィルタ (`clientRequestPath_like` 等)。`requestSource` の値の一覧。
- **RN-4**: 各 dataset の実際の反映遅延の分布。R4.6 の時間窓の終端と幅を決めるため。試作の `cf/q_lag*.graphql` の結果を当日に近い負荷で確かめる。
- **RN-5**: `durableObjectsPeriodicGroups` の `activeWebsocketConnections` の粒度と意味 (分内の最大値か、サンプルか)。dev・PR プレビューのサイネージ接続を区別できるか。
- **RN-6**: QuakeRelay の jsrpc の呼び出しを、cron の起動 (5 分ごと) の指標として使えるか。`/status` の呼び出しで DO を起こすと jsrpc が数えられ、判定が汚れないか。
- **RN-7**: AE の `aramakisai_stamp_metrics` で、本番の `STAMP_ENV` の値とレート制限を表す `outcome` の値。
- **RN-8**: collector の Pod から CNPG の instance の Pod IP:9187 と、他ノードの kubelet:10250 (PR #432) に、実際に届くか。Cilium と Hetzner のファイアウォールの経路を確認する。
- **RN-9**: `metrics.k8s.io` の pods の値 (CPU 使用量) の更新間隔。CMS の Pod と DB の primary の CPU の判定に足りるか。
- **RN-10**: ODPT の公開フィードの利用ガイドラインが定める取得間隔と、サーバーからの継続取得の可否。web 側の 30 秒の根拠を一次情報で確認する。
- **RN-11**: Discord Webhook のレート制限の公開値と、共用の送信元 (Falcosidekick・CMS のカメラ監視・CI) の当日の想定量。
- **RN-12**: oauth2-proxy のセッション (`--cookie-expire=12h`、`--cookie-refresh=1h`) の下で、バックグラウンドの fetch が refresh 後の Set-Cookie を受け取り、開催日の稼働時間内に再ログインなしで続くか。
- **RN-13**: CMS の `site_settings` global の `parking_monitor` が、認証なしの API で読めるか。
- **RN-14**: aramakisai-web 側の開催前の変更凍結の有無と期限。CMS の口を本番に入れる最終日。

---

## 設計フェーズの調査結果

### Summary
- **Feature**: `festival-ops-dashboard`
- **Discovery Scope**: Extension (既存 collector の拡張。light discovery)
- **Key Findings**:
  - 新しいライブラリは不要。Cloudflare GraphQL・AE SQL・ODPT の protobuf の header・CNPG の Prometheus テキストは、いずれも標準ライブラリで扱える。
  - 外部 API の問い合わせ量は、毎分の GraphQL 1 回と AE 1 回で、公開された上限の 1 割未満に収まる。
  - 駐車場の判定履歴は CMS に残らない。カメラごとの最新スナップショットしか保持されないため、判定の状態はカメラの項目として記録する (D-10 の改定)。

### Research Log

#### RN-1 レート制限
- **Sources**: https://developers.cloudflare.com/analytics/graphql-api/limits/ 、実際のレスポンスヘッダ。
- **Findings**:
  - GraphQL Analytics は 1 ユーザーあたり 5 分で 300 クエリ。一般 API の上限 (5 分で 1200) とは別枠。
  - AE SQL のクエリ数の上限は公式ドキュメントに記載がない。
  - レスポンスヘッダはどちらも一般 API の `Ratelimit-Policy: "default";q=1200;w=300` だけ。GraphQL の残り回数はヘッダでは分からない。
  - 試算: 毎分 GraphQL を alias でまとめて 1 回、AE を 1 回送ると、既存の billing (毎時) と tunnel (2 分ごと) を合わせて 5 分あたり GraphQL 約 5、全体 約 18。dataset ごとに 4 回に分けても GraphQL 約 20、全体 約 33。どちらも上限の 1 割未満。
- **Implications**: GraphQL は alias で 1 回にまとめ、毎分送る。429 と errors は取得失敗として扱い、リトライを重ねない (D-11)。

#### RN-2 Workers の status
- 型は string。値は success / clientDisconnected / scriptThrewException / exceededResources / internalError。
- リソース超過は `exceededResources` で、errors にも数えられる。

#### RN-3 フィルタと requestSource
- `clientRequestPath_like: "/api/media/%"` は account スコープで動作する (実測)。
- requestSource の実測値は eyeball / edgeWorkerFetch / edgeWorkerCacheAPI / earlyHintsCache。
- edgeWorkerCacheAPI は Worker の Cache API の GET・PUT で、キャッシュミスが 504 として記録される。
- **Implications**: 5xx の集計は許可リスト (`eyeball`・`edgeWorkerFetch`) で絞る。

#### RN-4 反映遅延 (平日の低トラフィック時に実測)
- 分 m の値が確定する時刻:
  - Workers: m+1:40
  - HTTP: m+2:40 (m+1:40 の時点では確定値の 5〜9 割)
  - DO periodic: 出現した時点で確定 (m+1:40〜2:40 に出現)
  - DO invocations: m+1:40〜2:40
- 要求の無い分は行が返らない。
- AE は直近の書き込みが無く、未実測。リハーサルで実測するまで同じ窓を仮置きにする。
- **Implications**: 件数の時間窓は `[floor(now)-8分, floor(now)-3分)`。接続数系は最新の分を使い、その分が `floor(now)-4分` より古ければ取得遅延とする (D-11)。

#### RN-5 activeWebsocketConnections
- objectId (DO インスタンス) 単位で、各分の最大値。`name:"signage-hub"` で絞り込める。
- dev もプレビューも `idFromName('signage-hub')` で同じインスタンスに接続する (site-entry.ts、frontend/wrangler.toml)。ハブは tag を付けずに接続を受け付けている (signage-hub/index.ts)。
- 7 日間の接続要求のうち、本番は約 4 割。analytics では本番だけを区別できない。
- **Implications**: 本番の接続数は、ハブが接続元ごとのタグで数えて返す (D-9)。analytics の値は全接続元の合計として参考表示に使う。

#### RN-6 地震リレーの jsrpc
- cron (ensure)、GET /status、/dismiss、/dev/inject はすべて jsrpc として数えられる。alarm は別の type。
- cron だけを区別できる dimension は無い。
- `workersInvocationsScheduled` は 2026-10-27 以降の記録が欠けている。
- Telemetry API は POST で、Workers Observability Write の権限が要るため使えない。
- **Implications**: 定期起動の継続は、リレー自身が記録する最終実行時刻で判定する (D-8)。

#### RN-7 スタンプの AE
- 本番の `STAMP_ENV` は `'prod'` (frontend-ci.yml の deploy-prod)。dev は `'dev'`、プレビューは `'preview'`。プレビューも本番の Worker 名で動くため、`blob1='prod'` での絞り込みが必須。
- レート制限の outcome は `rate_limited`。来場者向けの verify / stamps / exchange_token で発生する。
- 件数は `SUM(_sample_interval)` で数える。
- 環境変数の読み込みに失敗すると AE に何も書かれない (stamp-api.ts)。

#### RN-8 到達性
- 経路を遮る設定は無い。
  - NetworkPolicy はすべて Ingress 専用。CNP / CCNP は 0 件。
  - Cilium v1.17.3 で enable-policy=default。hostFirewall は無効。
  - kubelet は全インターフェースの 10250 番で待ち受け、authz は Webhook。
- metrics-server は通常の Pod から全ノードの kubelet を読めている。
- collector の Pod から直接届くかは未実測。
- 9187 番は認証なしの平文 HTTP。CNPG は 1.23.3。
- メトリクス名:
  - `cnpg_backends_total{application_name,datname,state,usename}` (全系列の和を取る)
  - `cnpg_pg_settings_setting{name="max_connections"}`
  - `cnpg_pg_replication_lag` (replica 側。ラベルなし、秒)
  - `cnpg_pg_stat_replication_{write,flush,replay}_lag_seconds` (primary 側)
- PR #432 (collector の `nodes/stats`) はマージ済みで、本番に反映済み。

#### RN-9 metrics-server
- v0.9.0、`--metric-resolution=15s`。
- collector の RBAC に `metrics.k8s.io` の pods は無い。

#### RN-10 ODPT
- センター利用規約には、取得間隔の数値の規定も継続取得の禁止も無い。
  - 第 4 条 4 項: センターが判断で制約を定められる。
  - 第 10 条 2 項 (8): 著しい負荷をかけた場合に停止できる。
- 関越交通のデータは CC BY 4.0。
- 実測では `header.timestamp` は取得時刻の約 29 秒前で、`Cache-Control: no-store`。生成間隔が約 30 秒なので、30〜60 秒ごとの取得で足りる。

#### RN-11 Discord
- webhook ごとの上限値は公開されていない。`X-RateLimit-*` ヘッダと 429 の `retry_after` に従う。
- 401 / 403 / 429 が 10 分で 10,000 件を超えると一時遮断される。
- `DISCORD_OPS_WEBHOOK_URL` を使う送信元は 9 つ:
  - Falcosidekick (notice 以上を抑制なしで送る)
  - CMS の駐車場カメラ通知 (cms-secrets の `PARKING_CAMERA_DISCORD_WEBHOOK_URL` の元が `DISCORD_OPS_WEBHOOK_URL`)
  - dr-trigger
  - infra-health-check
  - intrusion-response
  - k3s-version-check
  - k3s-upgrade
  - os-auto-update
  - Healthchecks.io (手動設定)
- 地震リレーは別の webhook (`DISCORD_QUAKE_WEBHOOK_URL`) を使っている。

#### RN-12 oauth2-proxy v7.15.5
- 認証付きの要求のたびに、前回から 1 時間を超えていれば refresh し、Redis の TTL と Cookie を 12 時間に延長する。nginx はその Set-Cookie をそのまま返している。
- 期限切れ後の fetch は 401 になる (`Sec-Fetch-Mode` が navigate でも空でもない場合)。
- Zitadel の refresh token の既定は idle 720h、絶対期限 2160h。instance 側で変更されていないかは未確認。
- **Implications**: 15 秒ごとの fetch が refresh を兼ねるため、開催日の稼働時間内は再ログインなしで続く見込み。401 を受けたら「表示が古い・再ログイン要」を出す (D-2)。

#### RN-13 site_settings
- 認証なしで 200。
- `parking_monitor` は `{start,end}` の HH:mm 文字列で、JST 固定。`end` ちょうどは時間内として扱う。
- `event_days` は `{id,start_at,end_at,label}` で、UTC の ISO 形式。
- 公開ページの判定は「start_at を JST に直した日付が今日」かつ「時間窓の内側」。

#### RN-14 リリース経路
- CMS の経路 (web の main マージ → cms-ci → bump PR を人がマージ → ArgoCD) の所要は、直近 6 件で機械処理分の中央値 8.4 分、全体 10.8 分。
- web (frontend) の main マージから本番デプロイまでは、中央値約 5 分、最大約 6.5 分。
- 開催前の凍結や締切は、どの文書にも無い。

#### 駐車場 (CMS) の判定データ
- **Context**: R5.2 の「判定の成否」を CMS から読み取れるか。
- **Sources**: aramakisai-web origin/main の `cms/src/endpoints/parking-ingest.ts`、`cms/src/lib/parking-judge-task.ts`、`cms/src/collections/parking-*.ts`。
- **Findings**:
  - parking-ingest は、新しいスナップショットを作った直後に、同じカメラの他のスナップショットを無条件で削除する。parking_snapshots にはカメラごとに最新の 1 件しか残らない。
  - 判定結果 (`judge_status`・`judge_error`・`judged_at`) は、そのスナップショット自身にだけ書かれる。失敗・見送りは、ほかには `payload.logger.warn` に出るだけ。
  - 判定中に次の画像が届いて元のスナップショットが消えると、判定は `superseded` で終わり、何も記録しない。
  - `same_window` の見送りは ingest が付ける。同じカメラの同じ時間窓で、すでに判定を予約済みのときに付く。判定の失敗ではなく、判定頻度を絞るための正常な動作。
  - `rate_limited` の見送りは判定側 (cms-worker の 1 プロセス) のレート制限で付き、判定は行われない。
  - `unknown` (混雑を判定できない) は `failed` として記録される。`invalid_quality` は `succeeded` のまま `judge_error` に付く。
  - `parking_cameras` は非公開。`last_captured_at` は監視時間内に受信したときだけ更新される。`device_status` は受信のたびに、画像の検証より前に更新される。
  - `parking_statuses` は公開。`updatedAt` は判定の成功で更新され、手入力でも更新される。
  - parking-ingest はカメラごとのトークンを管理画面で発行し、DB には sha256 だけを保存している。CMS が env の共有シークレットで照合する前例は無い。呼び出し側として env を持つ前例はある (CMS の `SIGNAGE_QUAKE_TOKEN`、collector の `OPS_MAIL_AGENT_TOKEN`)。
  - `last_judged_window` は ingest が SQL の 1 文 (`update ... where ...`) で原子的に更新している。同じ方法で連続失敗回数を加算できる。
- **Implications**: 判定の状態は `parking_cameras` の 4 項目 (最終成功・最終失敗・直近のエラー・連続失敗回数) として、判定のたびに上書きする (改定後の D-10)。`same_window` と `superseded` は記録を変えない (design.md の判断)。

#### 地震リレー
- `/status` が返すもの: `inPeriod`、`period`、`alert`、`channels` (p2p / wolfxEew / wolfxEqlist のそれぞれに connected / lastReceivedAt / receiving / test)、`unreachable`、`discord` (configured / disabled / lastSuccessAt / lastFailureAt / lastFailure)。
- `SIGNAGE_QUAKE_TOKEN` で GET /status、POST /dismiss、POST /dev/inject/* (本番では 404) が認可される (http.ts)。
- `alarm()` も `ensure()` を呼ぶ。cron の最終実行時刻は、cron だけが通る経路 (`handleScheduled`) で記録する必要がある。期間外は `handleScheduled` が DO を呼ばない。
- 読み取り専用トークンに分けるための最小変更:
  1. relay-config.ts に読み取り用キーを追加する。
  2. http.ts で /status だけを読み取り用トークンでも通す。
  3. テストを足す。
  4. frontend-ci.yml の relay の secrets 書き出しに追加する。
  5. Infisical prod にキーを追加する。
  6. infra の collector-secrets に 1 行追加する。
- CMS 側は変更不要。リレーの URL は cms Deployment の env `SIGNAGE_QUAKE_URL` として、すでに公開リポジトリに載っている。

#### サイネージハブとサイトの Worker
- frontend-ci.yml の deploy-prod は、現行の main では毎回ハブを `wrangler deploy` している。web PR #297 (dev 向け、未マージ) で、ハブに差分があるときだけデプロイするように直してある。ハブのデプロイでは全端末の WS が切れる。
- プレビューは本番と同じ Worker 名 (`wrangler versions upload`) で動き、wrangler.toml の vars を共有する。環境を区別できる値は、デプロイ時の secrets-file で渡す `STAMP_ENV` だけ。
  - Workers の指標 (`scriptName=aramakisai-web`) には、プレビューの要求も混ざる。
- CF Access のアプリ `aramakisai-web (signage)` は、`aramakisai.com/signage` と `aramakisai.com/api/signage` を保護する。ポリシーはアプリ単位で、HTTP メソッドでは絞れない。
  - サービストークンの前例: `signage_notify` (8760h、更新は期限の 30 日前以降の apply で作り直す、`non_identity` のポリシー)。
- ハブの `/api/signage/notify` は Worker 側で認証しておらず、Access だけで守られている。

#### collector の既存部品
- `HttpError` のメッセージは URL の path を含む。Discord の Webhook URL は path にトークンを含むため、失敗のログにトークンが出る。
- `Http.request` は応答本文だけを返し、429 の `Retry-After` と `X-RateLimit-*` を読めない。
- deployment の initContainer は、configMap ごとにディレクトリを平らにコピーしている。新しいパッケージのディレクトリには、configMap とコピーの 1 行が要る。
- `render/labels.py` は ops-dashboard の design.md を文言の正本としている。D-6 で既存の spec は書き換えないため、当日監視の文言は別のモジュールに持つ。
- `Collector.snapshot()` は描画のたびに `security.falco` の render を実行する。当日監視の評価は snapshot を経由せず、結果の写しを読む必要がある。

### Architecture Pattern Evaluation

| Option | Description | Strengths | Risks / Limitations | Notes |
|--------|-------------|-----------|---------------------|-------|
| A 既存 collector を拡張 | 情報源・画面・通知を collector に直接足す | 取得・配布・認可を使い回せる | `/admin/` との混在。例外の波及 | ギャップ分析の案 A |
| B 別コンポーネント | 当日監視専用の Deployment | 障害の分離 | 既存の情報源の二重取得。追加の manifest が多い | ギャップ分析の案 B |
| C 組み合わせ (採用) | 取得は collector の Source に足し、評価・通知・描画は `festival/` パッケージに分ける | 取得の共有と責務の分離。web 側を並行できる | プロセスは共有し、分離は例外の範囲まで | D-1〜D-3 と整合 |

### Design Decisions

D-1〜D-15 は design.md の Overview と各節に反映した (D-10 は改定版)。以下は、design で新たに決めたもの。

#### Decision: 当日監視の情報源の名前空間
- **Context**: D-1。`/admin/` の要確認は snapshot 全体を集計する。
- **Alternatives Considered**:
  1. 当日監視の Collector を別インスタンスにする。`store.load_results()` が全行を読むため、結局は除外が要る。
  2. source_id に `festival.` の接頭辞を付け、`/admin/` の snapshot から除外する。
- **Selected Approach**: 2。Collector は共有し、`/admin/` に渡す snapshot から `festival.` で始まる結果を除く。
- **Rationale**: 除外点が 1 か所で済み、取得スレッド・永続化・失敗の扱いをそのまま使える。
- **Trade-offs**: 当日監視の情報源が増えるとスレッドが増える (9 本)。
- **Follow-up**: `/admin/` の表示が変わらないことをテストで確かめる。

#### Decision: 通知の状態を差分で決める
- **Context**: D-3・D-14。再起動や送信失敗でも、通知の取りこぼしと重複を避けたい。
- **Alternatives Considered**:
  1. 送信待ちのキュー (outbox) を持つ。
  2. 項目ごとに「通知済みの状態」を保存し、評価のたびに現在の状態との差分から送る内容を決める。
- **Selected Approach**: 2。
- **Rationale**: 送信に失敗しても状態は変わらないので、次の評価で自然に再送される。再起動後も SQLite から続きを再開できる。
- **Trade-offs**: 異常が短時間で解消すると、通知が 1 度も届かないことがある (評価間隔 15 秒以内の解消)。

#### Decision: 駐車場の判定状態の記録規則
- **Context**: 改定後の D-10。`same_window` と `superseded` の扱いは、事実に基づいて design で決める。
- **Selected Approach**:
  - `same_window` は記録しない。判定頻度を絞るための正常な見送りで、失敗ではないため。
  - `superseded` は記録しない。結果が出ておらず、後続の画像の判定 (または `same_window`) に置き換わるため。判定が止まった場合は、最終成功時刻の経過で検知できる。
  - `rate_limited` は失敗として記録する。判定が行われず、続けば駐車場の表示が更新されないため。
- **Follow-up**: リハーサルで、`same_window` の頻度と最終成功時刻の間隔を確かめる。

#### Decision: 設計中に追加されたユーザーの決定
- R1〜R9 はすべて開催前の必須とし、web 側の改修 3 件を先に進める。駐車場の項目は設定で有効・無効を切り替えられる形を保つ。
- 通知の送信先は当日専用の Discord チャンネルと webhook (`DISCORD_FESTIVAL_WEBHOOK_URL`) にし、既存の ops webhook は使わない (D-14 の改定)。RN-11 の共用 9 件は、ops webhook についての事実として残す。
- 当日監視の停止は、評価処理から Healthchecks.io への定期的な ping で検知する。要件に R8.7 を足した。
- 姉妹 spec `committee-timeline` も `ops-dashboard/portal/links.yaml` を変更する予定。当日監視画面へのポータルの導線を足す場合は、衝突に注意する。

#### Decision: 未決事項 U-1〜U-8 への回答
- U-1: サイネージの接続元の識別値は `STAMP_ENV` を再利用する。
- U-2: `/api/signage/stats` 専用の Access アプリにする。公式ドキュメントで確認できているのは、1 つのアプリ内のパス規則と private アプリ同士では、より具体的な方が優先されることだけ。公開 HTTP アプリ同士が重なった場合の優先は、実装の最初に実測で確かめる。
- U-3: 閾値は仮の値で始め、リハーサル (2026-11-11〜12、最終確認 11-13) で見直す。本番反映は 2026-11-10 までに終える。
- U-4: 通知は本番に入れた時点から常に有効。開催日に限る切り替えは設けない。
- U-5: バスは開催日で絞らず、常に判定する。
- U-6・U-7・U-8: design.md の推奨どおり。

#### Decision: 監視項目の追加 (Q-23〜Q-29)
- **Context**: 開催前の見直しで、容量の監視に DB・cloudflared・CMS の負荷の項目を足す。ユーザーが Q-23〜Q-29 の全採用を決めた。閾値はすべて仮で、リハーサル (U-3) で見直す。
- **Q-23 DB の instance ごとのメモリ (R6.6)**:
  - kubelet `/stats/summary` の `pods[].containers[].memory.rssBytes` を、コンテナの memory limit で割る。注意 0.7・異常 0.85 (`db_memory_rss_ratio`)。
  - `workingSetBytes` と cgroup の `memory.current` はページキャッシュを含み、平常時でも limit 近くになるため使わない。
  - `/stats/summary` は既存の `nodes/stats` の権限で読める。kubelet への経路は festival.nodes と同じ k8s モジュールの呼び出しを使う。項目は festival.workloads に入れる (Pod の memory limit をすでに読むため)。
- **Q-24a DB の再起動・OOM・CrashLoop (R6.7)**: CMS と同じ規則を DB の全 instance に広げる。窓は CMS と共通にし、`[festival.cms]` の `cms_restart_window` を `restart_window` に改めて両方から参照する。`[festival.cms]` は DB の Cluster 名も持つため、節は変えない。
- **Q-24b DB の instance ごとの接続数 (R6.8)**: 9187 の `cnpg_backends_total` の Pod ごとの和を、全 instance について判定する。注意 25・異常 40 (`db_backends`)。primary の接続率 (`db_connections_ratio`) は残す。festival.cnpg に入れる。
- **Q-25 cloudflared の CPU 対 limit (R6.9)**: `metrics.k8s.io` の pods で、CMS と同じ取り方 (Pod ごとの比の最大値)。閾値は `pod_cpu_ratio` を共用する。Pod 名はロールアウトで変わるため、項目は Pod ごとに分けない。festival.workloads に入れる。
  - 対象は Deployment `cloudflared`、namespace `cloudflared`、セレクタ `app=cloudflared` (`gitops/manifests/prod/cloudflared/deployment.yaml`)。
- **Q-26 CMS と cloudflared の CPU スロットリング率 (R6.10)**:
  - kubelet `/metrics/cadvisor` の `container_cpu_cfs_throttled_periods_total` の増分 ÷ `container_cpu_cfs_periods_total` の増分 (前回の取得との差分)。注意 0.25・異常 0.5 (`cpu_throttle_ratio`)。festival.workloads に入れる。
  - kubelet の認可では `/metrics/*` が resource `nodes`・subresource `metrics` に対応し、GET は verb `get` になる (Kubernetes v1.36 のドキュメント。稼働中の k3s は `ansible/inventory/tailscale.yml` の `k3s_version: v1.36.3+k3s1`)。v1.36 で GA の細粒度の認可 (`KubeletFineGrainedAuthz`) でも `/metrics/*` の対応は同じ。RBAC に `nodes/metrics` の get を足す。`nodes/proxy` は他の kubelet API (コンテナでのコマンド実行を含む) を許すため使わない。
- **Q-27a CMS へのリクエストのレート (R6.11)**: 既存の `httpRequestsAdaptiveGroups` の alias `cms` (eyeball と edgeWorkerFetch) の件数から、alias `media` の件数を引き、窓の秒数で割る。`media` に件数の和を足すだけで、問い合わせは増えない。注意 80・異常 115 (`cms_rps`)。根拠は、負荷試験で測った容量に余裕を残す値。DB の改善後に測り直す。
- **Q-27b Cache API のヒット率 (R6.12・R6.13)**:
  - requestSource が edgeWorkerCacheAPI の GET のうち、キャッシュの状態が hit の比率。既存の問い合わせに alias `cms_cache` を 1 つ足す。GraphQL の HTTP 要求は毎分 1 回のままで、alias が 4 つから 5 つになる。
  - 閾値の根拠が無いため、閾値を宣言せず表示のみの項目にする。design に「表示のみ」の扱い (`Judgement.judged`、`fest.status.info`) を足した。閾値名 `cms_cache_hit_ratio` は省略できる閾値名とし、宣言すれば通常の判定に変わる。リハーサルで平常値を取って閾値を決める。
  - フィールド名は、スキーマの introspection で確定した。HTTP メソッドの絞り込みは `clientRequestHTTPMethodName` (`AccountHttpRequestsAdaptiveGroupsFilter_InputObject`)、キャッシュの状態は `cacheStatus` (`AccountHttpRequestsAdaptiveGroupsDimensions`)。分位は `originResponseDurationMsP95` (`AccountHttpRequestsAdaptiveGroupsQuantiles`、ms) と `cpuTimeP99` (`AccountWorkersInvocationsAdaptiveQuantiles`、マイクロ秒)。公開ドキュメントには一覧が無い。
- **Q-28 `cms_origin_p95_ms`**: 注意を 1500 にする (異常 3000 は据え置き)。平常時の p95 で注意に入らないようにするため。
- **Q-29 統計情報の無い表の数 (R6.14)**:
  - `pg_stat_user_tables` で `last_analyze` と `last_autoanalyze` が両方 NULL の表の数。注意 1・異常 10 (`db_unanalyzed_tables`)。festival.cnpg が primary の値だけを評価する。
  - CNPG のカスタムクエリで出す。オペレーターは 1.23.3 (`gitops/apps/prod/cloudnativepg.yaml` の chart `0.21.*`、`gitops/manifests/prod/cms/db-cluster.yaml` のコメント、RN-8)。
  - CNPG 1.23 のドキュメントによる書式: Cluster の `.spec.monitoring.customQueriesConfigMap` に `name`・`key` を並べる。クエリは `query`・`primary`・`target_databases`・`metrics` (列ごとの `usage`・`description`) で書く。指標名は `cnpg_<クエリ名>_<列名>`。ラベル `cnpg.io/reload` を付けた ConfigMap は自動で読み直される。指標は既存の 9187 番に出る。
  - クエリは既定で Cluster の main database で実行される。`directus-db` は recovery で作った Cluster で、CMS のデータベース `payload` は PreSync Job (`db-init-job.yaml`) が作るため、`target_databases: ["payload"]` を明示する。
- **CNPG の `spec.monitoring` の変更の影響**: `customQueriesConfigMap` の追加・変更では、Pod の再作成も PostgreSQL の再起動も起きず、primary は切り替わらない。CNPG v1.23.3 のソースで確かめた。再作成の判定 (`internal/controller/cluster_upgrade.go` 292〜356 行・572〜617 行) が比べる PodSpec に `Spec.Monitoring` は入らない (`pkg/specs` での参照は `roles.go` 151〜178 行と `podmonitor.go` 48〜50 行だけ)。instance manager は reconcile のたびに ConfigMap を読み直し、エクスポーターのクエリだけを差し替える (`internal/management/controller/instance_controller.go` 754〜830 行)。
- **Trade-offs**:
  - 容量のタブの項目は約 35 個に増える。1 タブ 40 個以下の想定と、レイアウトの予算 (54 個) の内に収まる。
  - festival.workloads が対象の Pod のあるノードの kubelet にも問い合わせる。festival.nodes の `/stats/summary` と重なるが、クラスタ内の問い合わせで負荷は小さい。
  - スロットリング率は前回の取得との差分のため、collector の起動直後の 1 周期は値が出ない (取得遅延と表示する)。
- **Follow-up**:
  - リハーサルで、全閾値と Cache API のヒット率の平常値を確かめる。

### Risks & Mitigations
- collector の Pod から 9187 番・10250 番・CMS の Service に届かない → 実装の最初のタスクで確かめる。届かない項目は取得失敗として表示し、設定で無効にできる。代替経路は、届かなかった時点で原因を添えてユーザーに諮る (design.md の U-8)。
- web 側の改修の本番反映が開催に間に合わない → 駐車場の項目は設定で無効にできる。地震リレーとハブは開催前に反映する (D-8・D-9・D-15)。
- ハブのデプロイで全端末の WS が切れる → D-9 の改修のデプロイは開催前に済ませる。PR #297 のマージ後は、ハブに差分があるときだけデプロイされる。
- Discord webhook のレート制限 → 通知は当日専用の webhook (`DISCORD_FESTIVAL_WEBHOOK_URL`) に送り、他の送信元と共用しない。1 回の評価で送るのは最大 1 件 (まとめて送る)。`retry_after` と `X-RateLimit-*` に従い、再通知に間隔を設ける。
- 当日監視そのものの停止に気づけない → 評価が成功するたびに Healthchecks.io に ping する (チェック `festival-monitor`)。ping URL は mailserver-backup の前例と同じく、Terraform の sensitive な output を Infisical に登録して渡す。
- AE の反映遅延が未実測 → 時間窓は仮置き。リハーサルで実測して見直す。
- Workers の指標にプレビューの要求が混ざる → 閾値の判定に最低要求数を設ける。開催中はプレビューのデプロイが少ない前提で受け入れる。

### References
- [Cloudflare GraphQL Analytics API limits](https://developers.cloudflare.com/analytics/graphql-api/limits/): GraphQL のクエリ数の上限。
- [Kubelet authentication/authorization (v1.36)](https://v1-36.docs.kubernetes.io/docs/reference/access-authn-authz/kubelet-authn-authz/): kubelet の API の経路と、認可の resource・subresource の対応。
- [CloudNativePG 1.23 Monitoring](https://cloudnative-pg.io/documentation/1.23/monitoring/): カスタムクエリの ConfigMap の書式、指標名、既定のデータベース、`cnpg.io/reload`。
- [k8s.io/kubelet stats v1alpha1](https://pkg.go.dev/k8s.io/kubelet@v0.36.0/pkg/apis/stats/v1alpha1): `/stats/summary` の `MemoryStats.rssBytes` と Pod・コンテナの構造。
- `.kiro/specs/ops-dashboard/design.md`: collector の既存の設計 (ADR として参照)。
- aramakisai-web origin/main: `cms/src/endpoints/parking-ingest.ts`、`cms/src/lib/parking-judge-task.ts`、`frontend/workers/signage-quake/`、`frontend/workers/signage-hub/`、`frontend/workers/site-entry.ts`、`.github/workflows/frontend-ci.yml`。
