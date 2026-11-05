# 運用ポータル・運用ダッシュボード 運用手順

`dash.aramakisai.com` で提供する。executive はポータル (委員会サービスへのリンク集)、admin は加えて運用ダッシュボード (`/admin/`) を使える。構成は ArgoCD Application `ops-dashboard` (namespace `ops-dashboard`、portal = nginx + oauth2-proxy + Homer、portal-redis = oauth2-proxy のセッションストア (Valkey)、collector = Python)。マニフェストは `gitops/manifests/prod/ops-dashboard/`。変更はすべて PR → マージ → ArgoCD 同期で反映する (クラスタへの直接操作はしない)。

## リンクの追加・変更

リンクの単一ソースは `gitops/manifests/prod/ops-dashboard/portal/links.yaml`。

- 公開してよい URL: `services[].items[]` に `name` / `icon` / `subtitle` / `url` を追加する。
- 内部向け URL (公開リポジトリに書かない): 項目に `url_env: <環境変数名>` を書き、次の手順で値を渡す。環境変数が空だと、その項目だけが表示されない。
  1. Infisical (prod) にキー (例 `PORTAL_NOTION_URL`) を登録する。
  2. `portal/external-secret.yaml` の内部向けリンク用 ExternalSecret の `data` に `remoteRef.key` を追加し、`portal/deployment.yaml` の環境変数 (optional) に同名で渡す。
  3. PR をマージし、`kubectl get externalsecret -n ops-dashboard` で反映を確認する (`spec.data` を足した場合は ArgoCD が反映を見逃すことがあるため、`.kiro/steering/tech.md` の既知問題の手順で確認する)。
- admin 専用の項目は `portal/admin-overlay.yaml` に書く。
- 生成の検証: `portal/test-generate.sh`。

## ノード表示の範囲

CPU・メモリ使用率と K3s バージョン (ノード間で異なれば両方を表示して警告) は `[[servers]]` に列挙した全ノードを表示する。ディスク使用率と OS パッチ・再起動要否は collector Pod の hostPath から読むため、collector の動く `prod-node-1` の値のみを表示し、行名にノード名を付ける。

## プラン・期待サーバーの宣言

`gitops/manifests/prod/ops-dashboard/collector/dashboard.toml` を編集して PR を出す。collector は起動時に検証し、誤りがあると起動に失敗する (ConfigMap 反映後に Pod を再起動して確認する)。

- プランの切り替え: `[[plans]]` の `from` / `until` を書き換える。同一 `service` の期間は重ねない。
- 稼働しているべきサーバー: `[[servers]]` に `name` を列挙する (イベント期間中に `prod-node-2` / `prod-node-3` を追加する場合は、期間に合わせて追加・削除する)。
- 資格情報の有効期限: 期限のあるトークンを再発行したら `[[credentials]]` の `expires_on` を更新する。
- 凍結中のワークロード: `[exclude]` に Namespace または名前を列挙する。
- 閾値・情報源の宣言: `[thresholds]`、`[sources."<source_id>"]`。

## 当日監視画面

admin は `/admin/festival` で、開催日当日の各機能・容量・基盤・セキュリティの項目を正常・注意・異常で見られる。collector が取得・評価し、異常への遷移・再通知・回復を当日専用の Discord webhook (Infisical キー `DISCORD_FESTIVAL_WEBHOOK_URL`) に通知する。`dashboard.toml` に `[festival]` が無ければ何もせず、画面は「未設定」を表示する。

### 推移と画面内の詳細

各タイルは直近 60 分の推移 (スパークライン) を表示し、タイルを開くと同じ画面内に、当日 (JST 0:00 から現在) の折れ線と閾値の線、内訳の詳細を表示する。推移の値は collector の SQLite の `festival_history` に 1 分ごとに 1 点、24 時間分だけ持つ (24 時間を過ぎた行は評価のたびに消える。時系列の保存基盤は置かない)。判定と内訳の推移は持たない。

`festival_history` は復元の対象外。失うとその時点より前の推移が消え、次の評価から記録し直される。

### 宣言の変更

`collector/dashboard.toml` の `[festival]` 配下を編集して PR を出す。ConfigMap のハッシュが変わり collector Pod が再作成されて反映される。誤りがあると起動に失敗する。

- 閾値: `[festival.thresholds]` (注意 `warn`・異常 `crit`)。比率の判定に要る最低要求数は `[festival.min_requests]`。`cms_cache_hit_ratio` は省略でき、省略中は判定せず表示だけを行う。
- 再通知の間隔 `renotify`、情報源の失敗を通知するまでの猶予 `source_failure_grace`、情報源ごとの鮮度の上限 `[festival.max_age]` (超えると「取得遅延」)。
- サイネージ設置台数 `[festival.signage] installed`: リハーサルで全端末を接続して確認した台数を設定する。0 の間は判定しない。
- 開催日と駐車場の監視時間は宣言せず、CMS の `site_settings` が正本。

### 情報源の無効化

`[festival] disabled_sources` に source_id (例 `festival.signage`) を書くと、その情報源を取得も評価もしない (画面は「無効」)。依存する項目がすべて無効なら項目自体を作らない。到達できない情報源の通知を止めたいときに使う。

### 駐車場の有効化

既定は `[festival.parking] enabled = false` (問い合わせず、項目を作らない)。web 側の `/api/ops/parking-health` が本番に入り、Infisical の `OPS_PARKING_HEALTH_TOKEN` が `collector-secrets` と `cms-secrets` の両方に渡っていることを確認してから `true` にして PR を出す。

### 生存確認 (Healthchecks.io)

collector は評価が例外なく終わるたびに、60 秒以上空けて Healthchecks.io のチェック `festival-monitor` (timeout 60 秒・grace 180 秒) へ ping する。評価の停止・例外の継続・Pod の停止で ping が途絶え、約 4 分で通知される。ping URL は Infisical の `HEALTHCHECKS_FESTIVAL_MONITOR_PING_URL` (未設定だと画面に「生存確認が未設定」の注意が出る)。

通知先は Terraform で管理しないため、チェック作成後に Healthchecks.io のコンソールで `festival-monitor` の Integrations (通知先) を手動で設定する。

### 戻し方

- 通知・取得を止める: 該当の `disabled_sources` を足す、または `[festival]` 一式を外す PR をマージする。
- 閾値の誤りによる誤通知: `[festival.thresholds]` を直す PR をマージする。
- collector 全体を元に戻す: 当日監視を入れた PR を revert する。

### collector のメモリ

collector の limit は 160Mi。当日監視の追加分の見積り (仮) は +15〜25Mi (スレッド 11 本、項目 100 未満、GraphQL の応答は数 KB)。測定は `make kubectl ARGS="top pod -n ops-dashboard"`。推移と内訳の追加分の見積り (試算) は +6〜10Mi (書き込みの接続のページキャッシュ最大 2MiB、断片 1 回の描画で一時的に約 4MiB、同時に 2 画面で最大約 8MiB、内訳は 100 行未満)。合計の見込み (試算) は 44 + 25 + 10 = 約 79Mi で、limit の約 49%。いずれも実測ではない。

実測が limit の 75% (120Mi) を超えたら、増加の要因を調べて limit の見直しを検討する。

実測値: 本番反映後に記録し、上記の合計の見込み (試算) と比べる。

## executive・admin の付与・剥奪

ロールは Zitadel で管理する (手順は Zitadel の既存運用)。反映の仕組みは次のとおり。

1. Zitadel のロール `executive` / `admin` が ID token の `groups` claim に入る。
2. oauth2-proxy が `groups` claim を読む (`--oidc-groups-claim=groups`)。ポータル全体は `executive`、`/admin/` 配下 (運用ダッシュボード) と admin 用リンクは `admin` を持つ場合だけ通す。
3. セッションは Valkey (`portal-redis`) に保存され、`--cookie-refresh=1h` で 1 時間ごとに refresh token から ID token を取り直してロールの変更を取り込む。refresh は redis のロックで直列化される。

付与は最大 1 時間以内に反映され、剥奪は最大 1 時間以内に効く (cookie の有効期限は 12 時間だが、refresh 時にロールを失っていれば拒否される)。即時に反映したい場合は、利用者がログアウト (`/oauth2/sign_out`) して再ログインする。

Valkey は永続化しないため、`portal-redis` Pod が再起動 (再作成) されると保存中のセッションが失われ、全員が 1 回再ログインになる。障害ではなく想定された挙動で、対応は不要。

## 資格情報

collector が使う読み取り専用資格情報の権限・所有アカウント・ローテーション手順は `.kiro/steering/tech.md` の「手動発行する資格情報 (ops-dashboard)」を参照。

## データの扱い

collector の SQLite と mailserver の状態・ログ・レポート用の PVC は再構築時に復元されない。詳細は `.kiro/steering/dr.md`。
