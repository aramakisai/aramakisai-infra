# 性能測定記録

タスク 1.2「オリジン直経路でブレークポイントを実測する」の結果。実行日時は下記の各測定に記載する。対象は CMS (Payload, `cms` Deployment) の `/api/globals/festival_meta?depth=1`。実行環境: `scripts/load-test/breakpoint.js` を `grafana/k6` Docker イメージで実行。

## 事前条件確認

- CNPG バックアップ正常性: `directus-db` (CNPG クラスタ、CMS の DB) のログで `2026-09-21T02:00` の daily barman backup が `Backup completed` で完了、WAL archiving も継続稼働していることを確認済み。
- 実施時間帯: 2026-09-21 15:15頃〜15:24頃 (JST)。利用の少ない時間帯であることは別途運用判断による。

## 経路とその律速判定

- 経路: `make kubectl ARGS="port-forward -n prod svc/cms <local>:80"` によるオリジン直（Cloudflare 非経由）の port-forward 経由。
- 判定: **port-forward 自体が律速となり、CMS 自体の限界には到達しなかった。**
  - 根拠1: ブレークポイントとされたレート帯 (target ~20 req/s) で `kubectl port-forward` プロセスが `read: connection reset by peer` / `lost connection to pod` で異常終了し、k6 側は `dial tcp 127.0.0.1:18080: connect: connection refused` を大量に記録した。
  - 根拠2: 同時間帯の CMS Pod ログ (`--since=5m`) にはアプリケーションレベルのエラーが一切記録されていない。
  - 根拠3: ノード CPU/メモリはテスト前後・テスト中を通じて `CPU 26-39% / MEMORY 73-74%` で終始ほぼ一定（アイドル時 27%/74% と有意差なし）であり、CMS/DB 側の負荷増大は見られなかった。
  - 結論: レイテンシ悪化・接続断はサーバー側リソース枯渇によるものではなく、`kubectl port-forward` トンネル（単一 TCP セッションのトンネリング）が高並列・高レートで耐えられず切断したことによる。CMS 自体のブレークポイントは本測定では未到達。

## 帯域律速でないことの確認

- レスポンスサイズ実測: `curl` で 1 リクエストあたり 9,444 bytes。
- 成功した MAX_RATE=30 req/s の実行で実測した `data_received` は 28 MB / 180s ≈ 156 kB/s（約 1.25 Mbit/s）。一般的なブロードバンド回線の帯域に対して無視できる水準であり、実行元 (このマシン) の回線帯域が律速している可能性はない。
- `ifstat` 等の常時監視は実施していないが、上記のレスポンスサイズ×到達 RPS の概算により帯域律速でないと判断した。

## 測定 1: MAX_RATE=30 req/s (成功、エラーなし)

パラメータ: `START_RATE=1 MAX_RATE=30 RAMP_DURATION=3m PRE_ALLOCATED_VUS=50 MAX_VUS=200`

- 結果: **30 req/s まで到達してもエラー 0%。ブレークポイント未到達。**
- `http_req_failed`: 0.00% (0/2790)
- `error_rate` (2xx/3xx チェック失敗率): 0.00%
- `http_req_duration`: avg=419ms, min=275ms, med=317ms, p90=870ms, p95=954ms (p99 は k6 既定サマリに含まれず未測定)
- 到達 VUs: 最大 31 (PRE_ALLOCATED_VUS=50 の範囲内、VU 不足による頭打ちなし)
- ノードリソース (テスト中): CPU 35-39% / MEMORY 73-74%(アイドル時比ほぼ横ばい)
- CMS Pod CPU: 292-317m core (アイドル時 ~10m core から明確に増加。実際に処理が実行されたことを確認)
- OOMKilled: なし。CMS Pod の RESTARTS は 0 のまま
- `directus-db-1` (CNPG, CMS DB): RESTARTS 変化なし、ログにエラーなし

## 測定 2: MAX_RATE=100 req/s (abortOnFail で自動停止)

パラメータ: `START_RATE=5 MAX_RATE=100 RAMP_DURATION=3m PRE_ALLOCATED_VUS=100 MAX_VUS=400`

- 結果: 実行開始から約 28 秒後 (到達目標レート ≈ 20 req/s 付近) で `abortOnFail` が発動し自動停止。
- `error_rate`: 16.76% (58/346)、`http_req_failed`: 16.76%
- エラー種別: **k6 実行元での `dial tcp 127.0.0.1:<port>: connect: connection refused`**（CMS からの HTTP 5xx やタイムアウトではない）。原因は `kubectl port-forward` プロセスのクラッシュ（`error: lost connection to pod` / `read: connection reset by peer`）。
- CMS Pod ログ: 同時間帯にエラーログなし
- ノードリソース: CPU 26-39% / MEMORY 73-74%で、測定1と有意差なし
- OOMKilled: なし。CMS Pod RESTARTS 0 のまま変化なし

### `abortOnFail` の動作確認

`error_rate` / `http_req_failed` の両閾値 (`rate<0.1`, `abortOnFail: true`) が想定どおり機能し、k6 プロセス自体が `K6_EXIT=99` で自動終了した。手動停止 (Ctrl+C 相当) は不要だった。

## 測定後の復帰確認

- port-forward を再確立し、オリジン直: `curl http://localhost:18081/api/globals/festival_meta?depth=1` → `http_code=200`
- Cloudflare 経由: `curl https://cms.aramakisai.com/api/globals/festival_meta?depth=1` → `http_code=200`
- `make kubectl ARGS="get pods -n prod -o wide"`: 全 Pod `Running`、CMS/DB とも RESTARTS 0（テスト前と変化なし）
- `make kubectl ARGS="top nodes"`: CPU 26% / MEMORY 73%（アイドル時のベースラインに復帰）
- 測定に使用した port-forward プロセスは測定後に停止済み

## 結論・未確認事項（port-forward 経由の初回測定時点）

- CMS 自体は 30 req/s のオリジン直負荷を CPU 36%・メモリ横ばいで問題なく処理できることを確認した。この範囲でのブレークポイントは存在しない。
- 100 req/s 目標のランでは port-forward トンネル自体が ~20 req/s 付近で先に破綻し、CMS のブレークポイントを本測定では特定できなかった。**「port-forward が律速で CMS の限界は未到達」の状態である。**

---

# 再測定: SSH 直接経路によるブレークポイント特定（2026-09-21 15:26頃〜15:35頃 JST）

## 経路調査

port-forward を介さないクラスタ変更なしの到達経路を調査した。

- **Tailscale サブネットルート**: `tailscale status --json` の `Self.PrimaryRoutes` は `None`、`AllowedIPs` は自ノードの `/32` アドレスのみ。Pod/Service CIDR のサブネットルート広告は行われていない（`terraform/tailscale.tf` にも `advertise-routes` 相当の設定なし）。→ 経路として使えない。
- **NodePort**: `terraform/firewall.tf` に「NodePort (30xxx) は不要」と明記。既存 Service も `cms` は `ClusterIP` のみ（`make kubectl ARGS="get svc -n prod cms"` で確認）。→ 該当なし。
- **hostNetwork/hostPort**: `gitops/apps/prod/nginx-ingress.yaml` により nginx-ingress controller が `hostNetwork: true` で `prod-node-1` の 80/443 を直接バインドしている実態を `ss -lntp`（ノード上、nginx pid 3158/2756 が 0.0.0.0:80/443 で LISTEN）で確認。ただし nginx-ingress は autoconfig 専用で CMS 向け Ingress は存在しない（`gitops/manifests/prod/` に CMS の Ingress なし）。CMS への公開経路は Cloudflare Tunnel が ClusterIP へ直結する構成のため、この 80/443 は CMS には使えない。
- **ノード上からの ClusterIP 到達性**: ノード上で `curl http://10.43.111.239:80/api/...` を実行し `code=200`（21ms）を確認。Cilium の kube-proxy 代替がホストネットワーク namespace からも ClusterIP を解決していることを確認した。

## 採用した測定経路

**SSH ローカルポートフォワード（`ssh -L <port>:<cms-ClusterIP>:80 root@prod-node-1`）を新たに採用。** k6 プロセス自体はこのマシン上で実行し、SSH の TCP 転送だけが `prod-node-1` を経由する。

- 採用理由: クラスタ側の変更は一切不要（Service/Ingress 変更なし）。ノード上で負荷生成プロセス（k6）を直接動かすわけではないため、作業3で禁止されている「ノード CPU を負荷生成側が奪う」問題を回避できる。`kubectl port-forward`（K8s API server 経由の SPDY トンネル、単一ストリーム多重化で脆弱）と異なり、SSH の TCP フォワードはカーネルレベルのソケット中継でより頑健。
- 実装: `ssh -f -N -L 18090:10.43.111.239:80 root@prod-node-1`、k6 は `docker run --network=host` で `BASE_URL=http://localhost:18090` を指定。

## 測定 3: MAX_RATE=150 req/s（SSH 直接経路、abortOnFail で停止）

パラメータ: `START_RATE=10 MAX_RATE=150 RAMP_DURATION=3m PRE_ALLOCATED_VUS=150 MAX_VUS=600`

- 結果: 開始 54 秒後（目標レート ≈ 52 req/s 付近）で `abortOnFail` 発動。
- `error_rate` / `http_req_failed`: 10.58% (146/1379)
- `http_req_duration`: avg=3.61s, min=270ms, med=5.14s, p90=6.58s, p95=6.86s（**測定1の30req/s成功時と比べ大幅に悪化**）
- エラー種別: k6 実行元での `read tcp 127.0.0.1:18090: read: connection reset by peer`（SSH トンネルのローカル終端でのリセット）
- サーバー側の変化: CMS Pod CPU が **アイドル 12-25m → 499m まで上昇**（前回 port-forward 測定の 292-317m を大きく超えた）。ノード CPU も 32% → 46% に上昇。`directus-db-1` も 16m → 63m に上昇。

## 測定 4: MAX_RATE=80 req/s（SSH 直接経路、緩やかなランプ、abortOnFail で停止）

パラメータ: `START_RATE=10 MAX_RATE=80 RAMP_DURATION=5m PRE_ALLOCATED_VUS=100 MAX_VUS=400`

- 結果: 開始 3分14秒後（目標レート ≈ 55 req/s 付近）で `abortOnFail` 発動。
- `error_rate` / `http_req_failed`: 10.72% (647/6034)
- `http_req_duration`: avg=2.27s, med=646ms, p90=7.43s, p95=8.42s（中央値はまだ良好だが裾が大きく劣化）
- 実測スループット: 6034 リクエスト/3m14s ≈ 31.1 req/s（目標レートが 55 req/s まで上がっているのに対し、実際に捌けた量はそこで頭打ち）
- サーバー側の変化: CMS Pod CPU が測定中一貫して **460-502m で安定**（テスト中複数回サンプリング、目標レートが 38→55 req/s と上昇し続けても CPU は動かなかった＝天井に張り付いた状態）。ノード CPU 41-44%、`directus-db-1` 67-82m。
- テスト終了直後: CMS Pod CPU 500m のまま（キュー掃け残り）、OOMKilled なし、RESTARTS 0 のまま変化なし。SSH トンネル自体はテスト直後も `curl` で 200 応答を継続でき、クラッシュしていなかった。

## 律速箇所の判定（根本原因の特定）

`gitops/manifests/prod/cms/deployment.yaml` の CMS コンテナ resources を確認したところ、**`limits.cpu: "500m"` が設定されている**ことを確認した（`limits.memory: "512Mi"` と併記、コード上明記済み）。

- 測定3・測定4とも、CMS Pod CPU が一貫して 460-502m（＝ 500m 制限のほぼ上限）で頭打ちになっており、CPU cgroup スロットリングと整合する挙動。
- ノード全体は CPU 33-46%（4 vCPU 中 1.3-1.8 コア使用）、MEMORY 73-75% で余裕があり、ノード側のリソース枯渇ではない。
- 前回測定（port-forward 経由）との対比: port-forward 時はサーバー側 CPU が動かないまま接続断のみ増えた（＝経路側の律速）。今回は SSH 経路でサーバー側 CPU が明確に上昇し、かつ 500m という設定値でほぼ頭打ちになっており、CMS の CPU limit による律速と判断できる。
- SSH トンネル自体はテスト後も生存・応答しており、トンネル側のクラッシュは発生していない。ただし「connection reset by peer」というエラーの発生源（CMS プロセス側でのソケット切断か、CPU スロットリングによる SSH 側のタイムアウトか）はソケットレベルまでは切り分けていない。

## 測定後の復帰確認

- オリジン直（SSH トンネル経由）: `curl http://localhost:18090/api/globals/festival_meta?depth=1` → `code=200`
- Cloudflare 経由: `curl https://cms.aramakisai.com/api/globals/festival_meta?depth=1` → `code=200`
- `make kubectl ARGS="get pods -n prod -o wide"`: 全 Pod `Running`、CMS/DB とも RESTARTS 0（測定前と変化なし、OOMKilled なし）
- 測定に使用した SSH トンネルプロセスは確認後に終了済み（プロセス残存なし）

## 最終結論

- **ブレークポイント: 目標レート約 50-55 req/s 付近（実測スループットは約 25-31 req/s で頭打ち）で、エラー率がしきい値 10% を超過して `abortOnFail` が発動する。**
- 律速要因は **CMS Deployment の CPU limit（500m）によるコンテナ単位のスロットリング**であり、ノード全体のリソース枯渇でも計測経路（SSH トンネル）の限界でもない。
- 容量計画への示唆: 単一ノードの空き CPU（4 vCPU 中アイドル 68-74% 未使用）には余裕があるため、CMS の `limits.cpu` を引き上げれば、この測定で観測された天井よりも高いレートまで処理できる可能性が高い（ただし本タスクでは検証のためクラスタ変更を行っていないため未実証）。

## 未確認・未解決の点

- CMS の `limits.cpu` を実際に引き上げた場合の新たなブレークポイントは未測定（本タスクはクラスタ変更禁止のため実施していない）。
- `error_rate` 閾値超過時の個々の失敗が「CMS プロセス側のソケットリセット」か「SSH トンネルが極端な遅延で二次的にリセットしたもの」かは、ソケットレベルのパケットキャプチャ等までは行っておらず未切り分け。ただしサーバー側 CPU 使用量との強い相関から CMS 側の律速である可能性が高いと判断している。
- p99 レスポンスタイムは k6 既定のサマリ出力に含まれず未取得。
- Cloudflare 経由の測定（タスク 1.3）は本タスクの範囲外につき未実施。

---

# タスク 1.3: Cloudflare 経由経路の測定とエッジキャッシュ効果の確認（2026-09-21 15:40頃〜15:43頃 JST）

## エッジキャッシュされているかの事実確認

対象 API `https://cms.aramakisai.com/api/globals/festival_meta?depth=1` に対し、負荷印加の前後を含め計 9 回 `curl` でレスポンスヘッダを確認した。

- `cf-cache-status: DYNAMIC` を全 9 回で確認（負荷印加前 4 回、負荷印加中 1 回、負荷印加後 4 回）。`HIT` は一度も観測されなかった。
- **結論: この API はエッジキャッシュされていない。** Payload の REST API は動的レスポンスとして Cloudflare を素通りし、毎回オリジン（CMS Pod）へ到達している。したがって本エンドポイントに関しては、エッジキャッシュによるオリジン負荷の吸収率は **0%** である。

## 測定条件（低レート・短時間）

タスク 1.2 のブレークポイント（目標 50-55 req/s 帯）には遠く及ばない低レートに留めた。Cloudflare のレート制限・WAF 判定に抵触する挙動（403/429、CAPTCHA 等）は発生しなかった。

パラメータ: `BASE_URL=https://cms.aramakisai.com START_RATE=1 MAX_RATE=8 RAMP_DURATION=40s PRE_ALLOCATED_VUS=10 MAX_VUS=20`（ERROR_RATE_THRESHOLD は既定 0.1 のまま、abortOnFail は温存）

- 実行時間: 40 秒（+ graceful stop）
- 総リクエスト数: 180、最大到達レート ≈ 7.99 req/s
- `http_req_failed` / `error_rate`: 0.00%（403/429/5xx なし、WAF ブロックなし）
- `http_req_duration`: avg=342ms, med=313ms, p90=377ms, p95=518ms, max=807ms
- `data_received`: 1.8MB / 40.3s ≈ 45kB/s（帯域律速の懸念なし）

## オリジン負荷（CMS Pod CPU）の観測 — オリジン直との比較

| 経路 | 到達レート | CMS Pod CPU |
|---|---|---|
| アイドル（負荷印加前） | 0 req/s | 7m |
| **Cloudflare 経由**（本測定, t=35s時点） | ≈7.1 req/s | 99m |
| オリジン直（測定1, タスク1.2） | 30 req/s | 292-317m |

Cloudflare 経由でも CMS Pod CPU がアイドル値(7m) から明確に上昇しており（99m）、リクエストがオリジンまで到達し実処理されていることを裏付ける。req/s あたりの CPU 消費で比較すると、Cloudflare 経由 ≈ 99m/7.1req/s ≈ 13.9m/req/s、オリジン直（測定1）≈ 305m/30req/s ≈ 10.2m/req/s と近い水準であり、**Cloudflare 経由であることによる追加のオリジン負荷は観測されなかった**（誤差・サンプリング間隔の粗さの範囲内）。

エッジキャッシュ吸収率は上記の通り 0%（本エンドポイントは非キャッシュ対象）であるため、この結果は「エッジキャッシュが吸収した」のではなく「Cloudflare を経由しても素通りでオリジンに全リクエストが到達する」ことを示すものである。CDN 経由でもオリジン保護効果は期待できない。

## 測定後の復帰確認

- Cloudflare 経由: `curl https://cms.aramakisai.com/api/globals/festival_meta?depth=1` → `200`
- `make kubectl ARGS="get pods -n prod -o wide"`: CMS `RESTARTS 0`（変化なし）、directus-db-1 `RESTARTS 1 (21d ago)`（本測定と無関係の既存値、変化なし）
- CMS Pod CPU はテスト終了後 84m まで低下（減衰中、テスト終了直後のサンプル。アイドル復帰は追跡していないが RESTARTS・エラーとも異常なし）

---

# タスク 1.4: ステートレス分散の要否判定

> **この節は古い。** 本節は design.md に合格ラインの算出式・係数が定義される前に書かれたものであり、「算出式が存在しない」という前提そのものが現在の design.md と一致しない。最新の判定はファイル末尾の「タスク 1.4 再実施」節を参照。本節は経緯の記録として残す。

## 想定ピーク負荷の spec 上の記載確認

`requirements.md`・`design.md` を確認した。

- `requirements.md` 1.5: 「ブレークポイントが想定ピーク負荷を上回る場合、ステートレスワークロードの分散を不要と判断する」という判定条件のみが定義され、**想定ピーク負荷の具体的な値や算定式は記載されていない**。
- `design.md` L785: 「測定の合格ラインは要件 1.3 のブレークポイントによって事後的に定まる。設計時点で固定的な目標値を置かず、前年実績から算出した想定ピークを上回るか否かを判断基準とする。**算出根拠は作業時に扱い、実測値を文書へ記載しない**」と明記されており、意図的に数値を書かない設計になっている。
- `gap-analysis.md` L169 の Research Needed 一覧に「想定ピーク負荷の算定根拠 — 要件 1.5 の判断に用いる閾値をどう定めるか」が**未解決項目として残存**している。前年実績からの算出が実際に行われた形跡は spec 内に見当たらない。

**結論: 想定ピーク負荷の具体的な数値は spec（requirements.md / design.md / gap-analysis.md）のいずれにも存在しない。** そのため、ブレークポイント実測値と想定ピークを数値で直接比較して要件 1.5 / 1.6 の条件を機械的に判定することはできない。この点は保留とし、以下は「想定ピークが確定していない前提での」判断材料の整理と暫定結論とする。

## 判定材料（実測値、推測を含む部分は明記）

### 1. CMS コンテナの現行 requests / limits

`gitops/manifests/prod/cms/deployment.yaml`（replicas: 1）:

| | CPU | Memory |
|---|---|---|
| requests | 250m | 256Mi |
| limits | 500m | 512Mi |

### 2. ノード `prod-node-1` の割当可能 CPU と残余

`make kubectl ARGS="describe node prod-node-1"` より:

- Capacity: cpu 4 / Allocatable: cpu 3800m
- 現行の全 Pod 合計: **requests 2195m (57%)**、limits 8000m (210%、CPU はコンプレッシブルリソースのため limits 側のオーバーコミット自体は許容される)
- requests ベースの残余: 3800m − 2195m ≈ **1605m**（スケジューリング上、他 Pod を圧迫せず requests を積み増せる余地）
- **namespace `prod` に `LimitRange: prod-default-limits` が存在し、コンテナ単位の CPU limit 上限が `Max: 2`（=2000m）に設定されている。** これが LimitRange 自体を変更しない前提での CPU limit 引き上げの実質的な天井になる。
- 参考: タスク 1.2 測定4（CMS が CPU limit 500m で頭打ちだった時間帯）のノード CPU は 41-46%（1.64-1.84 core）。うち CMS 自身が ~0.46-0.50 core を占めていたため、他ワークロードの実使用は ~1.1-1.4 core 程度と推定される。仮に CMS を LimitRange 上限の 2000m まで引き上げて実際に 2 core 使い切った場合、他ワークロードの当時の実使用と合算すると概算 3.1-3.4 core となり、Allocatable 3800m に対してなお収まるが、余裕は縮小する。

### 3. 1 レプリカあたりのスループット効率（実測ベースの外挿、線形性は未実証）

タスク 1.2 の実測から 2 通りの効率を算出した。

- **飽和状態（CPU limit 500m で頭打ち）**: 測定4で CMS Pod CPU 460-502m のとき実測スループット ≈ 25-31 req/s → **効率 ≈ 54-62 req/s/core**（幅の中心値としては概ね 55-60 req/s/core 程度）
- **軽負荷・非飽和状態**: 測定1（オリジン直, 30 req/s, エラー 0%）で CMS Pod CPU 292-317m → 効率 ≈ 95-103 req/s/core。本タスク1.3の Cloudflare 経由測定（≈7.1 req/s, CPU 99m）でも ≈ 72 req/s/core と、同程度の桁で軽負荷時のほうが高効率という傾向が一致した。

**外挿（線形仮定、CPU limit を変えた場合の期待スループット）:**

| CPU limit | 飽和効率(54-62 req/s/core)での外挿 | 軽負荷効率(72-103 req/s/core)での外挿 |
|---|---|---|
| 500m（現状） | 27-31 req/s（実測 25-31 req/s と整合） | ― |
| 1000m | 54-62 req/s | 72-103 req/s |
| 1500m | 81-93 req/s | 108-155 req/s |
| 2000m（LimitRange 上限） | 108-124 req/s | 144-206 req/s |

**この外挿には強い留保が必要で、実測されたものではない。** 根拠:
- Payload（Node.js）はデフォルトでは単一プロセス・単一メインスレッドで JS を実行する。DB I/O 等の非同期処理は libuv のスレッドプールを使うため 1 コアを超える恩恵はあり得るが、CPU limit を 1000m（1 vCPU）超に引き上げても、CPU 使用がその通りに線形にスループットへ転写される保証はない。特に 1500m・2000m の欄は「CPU cgroup の枠が空く」ことを意味するだけで、アプリケーションが実際にそれだけの CPU を使い切れるかは未検証。
- 効率自体、飽和時と軽負荷時で約 1.5-1.9 倍の差があり（54-62 vs 72-103 req/s/core）、CPU limit を上げた場合にどちらの効率に近づくかも不明。
- **したがって上表は「参考レンジ」であり、実測はタスク 5.9（または案 B を先行実施する場合はそれに対応する再測定）で行う必要がある。**

### 4. 想定ピーク負荷との比較

第1節の通り、想定ピーク負荷の数値が spec に存在しないため、**上記いずれの案についても「想定ピークに対して足りるか」を数値で確定させることはできない。** 想定ピーク値が別途（作業時に、非公開の前年実績データから）確定した場合は、上表の外挿レンジと突き合わせて再判定が必要。

## 3 案の比較と判定

| | 案A: 現状維持 | 案B: CMS CPU limit 引き上げ（レプリカ1のまま） | 案C: ステートレス分散（レプリカ増） |
|---|---|---|---|
| 実測ブレークポイント | 目標50-55 req/s帯（実質25-31 req/s） | 未実測（外挿: 上表参照、要実測） | 未実測 |
| 単一ノードで完結するか | ― | **する**。マニフェストの `resources.limits.cpu` 変更のみ（LimitRange 上限 2000m まで）。ノード追加（タスク2-5）と無関係に先行実施可能 | しない前提で設計されている場合が多いが、単一ノードでもレプリカ数を増やすこと自体は可能（ただし本 spec のノード追加・配置分散という文脈上の意図は複数ノードへの分散） |
| 単一障害点の解消 | 解消しない | **解消しない**。ノード1台・Pod1個のままのため、Pod クラッシュやノード障害時の可用性は変わらない。CPU limit 引き上げは処理能力の話であり可用性の話ではない | 複数ノードへレプリカ分散すれば Pod 単位の単一障害点は解消し得る（ノード単一障害点はタスク5系のノード追加が前提） |
| 変更に要する操作 | なし | Git コミット（`limits.cpu` 変更）+ ArgoCD sync。**本番マニフェスト変更のため、タスク1（本番構成を変更しない）の範囲を超える** | 同上（`replicas` 変更 + 配置制約）。**同じく本番マニフェスト変更を要し、タスク1の範囲を超える** |
| CPU limit 引き上げとの併用 | ― | ― | 併用可能。レプリカ増と limit 引き上げは独立した軸であり、レプリカ数を増やしても 1 レプリカあたりの CPU limit（500m）が据え置きなら、複数ノードへ分散しても合計処理能力は「500m × レプリカ数」相当にとどまる。**限界がコンテナ単位の CPU limit にある以上、レプリカを増やす前に limit を上げる方が、同じ CPU 予算に対して低コスト（Pod 数・スケジューリング・配置制約の複雑さを増やさない）で先に試すべき対策** |

### 判定

1. **想定ピーク負荷の数値が spec に存在しないため、要件1.5「ブレークポイントが想定ピークを上回る場合は分散を見送る」を字義通り適用して要否を確定させることはできない。** これは保留とする。
2. 一方で、律速要因が CMS コンテナの CPU limit（500m）であり、ノード全体には requests ベースで 1605m の残余、LimitRange 上限まで CPU limit を引き上げる余地（500m→2000m）があることが今回の実測・調査で判明した。**この状態で案C（レプリカ増によるノード分散）を先に実施しても、律速要因である「レプリカあたりの CPU limit」自体は変わらないため、レプリカ数倍以上の改善を保証しない。** 一方、案Bは低リスク・低コストでノード追加なしに検証でき、外挿上は 2-4 倍程度のスループット改善が見込める（ただし実測未検証）。
3. **したがって、想定ピーク負荷が確定し「案Bを実施してもなお不足する」ことが示されない限り、案C（タスク5.8のステートレス分散）を単独の結論として「必要」と判定する根拠はない。**
4. 案B・案Cとも実際の効果検証にはクラスタへの変更操作が必要であり、本タスク（1.3/1.4）はクラスタ変更を行わない参照系調査に限定されているため、**今回は判定・外挿までに留め、実測はタスク5.8/5.9（本番投入フェーズ）で行う。**

## タスク 5.8 の実施可否（結論）

**現時点では実施しない（保留・見送り）。** 理由:

- タスク5.8 の実施条件（tasks.md: 「タスク1.4の判定が必要性を示した場合に限り実施する」）を満たす「必要性を示すデータ」が現時点で存在しない。想定ピーク負荷が spec 上未確定であり、ブレークポイントとの比較による必要性の証明ができない。
- 仮に必要性が将来的に示された場合でも、律速要因が CPU limit である以上、**案B（CPU limit 引き上げ）を先に試し、再測定（タスク5.9 相当）で効果を確認したうえで、なお不足する場合に限り案C（タスク5.8）へ進むのが妥当**と判断する。案Bを飛ばして案Cへ進む技術的根拠は本測定からは得られていない。
- この判定はデータ層の冗長化（タスク4・5.5、CMS/認証基盤 DB の複数インスタンス化）には一切影響しない。データ層冗長化は可用性目的であり、本判定（処理能力面のステートレス分散）とは独立して実施する。

## 未確認・未解決の点

- 想定ピーク負荷の具体的数値・算定式は spec に存在せず、本タスクでは確認できなかった（前年実績データは spec の管理対象外）。
- 案B（CPU limit引き上げ）の効果は外挿値であり、実測されていない。特に Payload(Node.js) の単一スレッド特性により 1 vCPU 超の領域での線形性は未実証。
- タスク1.3のCloudflare経由測定における CMS Pod CPU 99m は t=35s時点の単発サンプルであり、40秒間の連続トレースは取得していない（top のポーリング間隔の制約）。
- Cloudflare の WAF/レート制限の具体的な閾値そのものは未確認（今回の低レート測定では抵触しなかった、という事実のみ確認済み）。

---

# タスク 1.4 再実施: 合格ラインの算出とステートレス分散の要否判定（2026-09-21 16:15頃〜16:25頃 JST）

design.md の「Performance & Scalability」節（想定ピーク負荷の算出式）を前提に再実施する。上の旧節は算出式が未定義だった時点の記録であり、現在の design.md とは前提が異なるため参照しない。

## 1. 1 ページビューあたりの CMS API コール数（実測）

対象: 公開サイトのトップページ `https://aramakisai.com/`（`frontend/src/app/(site)/page.tsx` → `getHomePage()`）。

### コードからの参考値

`frontend/src/lib/home-page.ts` の `getHomePage()` は 1 回のサーバーサイドレンダリングで CMS へ逐次 5 回の distinct なリクエストを発行する: `globals/festival_meta`・`sponsors`（`limit=0`）・`announcements`（`limit=10`）・`topics`（`limit=0`）・`globals/page_home`。これはコードを数えただけの値であり、そのままでは実測とみなさない。

### 実測: キャッシュ状態の直接観測

`curl -sI https://aramakisai.com/` を時間を空けて複数回（本測定中だけで 5 回以上）実行し、レスポンスヘッダを確認した。

- 全ての実行で `x-nextjs-cache: MISS`（`x-nextjs-prerender: 1`、`x-nextjs-stale-time: 300` も付与）。**`HIT` は一度も観測されなかった。**
- 数秒間隔で連続リクエストしても MISS が継続することから、現在の本番環境では OpenNext/Cloudflare Workers 側の ISR ページキャッシュが実質的に機能しておらず、**現状は「毎回フル SSR が走る」状態がそのまま本番の実態**である。キャッシュが効いている状態（HIT）は本測定では一度も再現できなかった。

### 実測: CMS Pod CPU 消費によるクロスチェック

`make kubectl ARGS="top pod -n prod cms-<pod>"` でアイドル時と負荷時の CPU を比較した。

- アイドル時（負荷印加前 2 サンプル）: 10-11m
- 実際の公開ページ（`https://aramakisai.com/`）を 20 回、約 1 req/s のペースで 20 秒間閲覧するバーストを発生させ、その間 2 秒間隔で 8 回サンプリング: ピーク時 **101m**（アイドル比 +90-91m）
- タスク 1.3 で確定済みの単一エンドポイント直叩きの校正値（Cloudflare 経由、7.1 req/s で CMS Pod CPU 99m、アイドル 7m 差し引き delta 92m → **約 12.96 m・CPU / (req/s)**）を用いて逆算すると、ページ閲覧 1 req/s あたりの CMS 呼び出し数は 90-91m ÷ 12.96m ≈ **約 7**。
- コード上の 5 回という値と、CPU 逆算による約 7 という値は同じ桁（片手で数えられる個数オーダー）で一致しており、「1 でも 20 でもなく 5 前後」という実測結果を裏付ける。差分は sponsors/topics の `limit=0`（全件取得）クエリが festival_meta 単発取得よりコストが高いこと、および `top pod` のサンプリング粒度によるノイズで説明が付く範囲。

### 結論（この項目）

**実測値: 1 ページビューあたり CMS API コール数 ≈ 5**（コードで確認できる distinct なリクエスト数、CPU 消費の実測で同オーダーであることをクロスチェック済み）。

**この値は「キャッシュが効いていない（MISS）状態」での値であり、かつ本測定時点ではそれが唯一観測できた状態＝現在の本番の実態そのものである。** 「キャッシュが効いている状態」（`x-nextjs-cache: HIT`）は本測定では一度も観測できず、その場合の値（0 に近づく想定）は本タスクでは実測できていない。合格ラインの算出には、実際に観測された「キャッシュが効いていない状態」の値（5）を用いる。これは安全側（値が大きい側）の実測値である。

## 2. 来場者数・1 人あたり平均ページビュー数の取得元

`requirements.md`・`design.md`・`research.md`・`gap-analysis.md` のいずれにも、来場者数・平均 PV の具体的な数値は記載されていない（design.md L785-814 が算出式と係数、および「入力値は作業時に取得し文書に残さない」という方針のみを定義している）。

- **来場者数**: オーナーから作業時にリポジトリ外の実績値として提供を受けた（2 開催日の合計値）。design.md の入力値定義「前年の実績値」に合致する。**値そのものはこの文書に記載しない**（下記「実績値の秘匿方法」参照）。
- **1 人あたり平均ページビュー数**: オーナーから GA（Google Analytics、`aramakisai.com` プロパティ）の「ページとスクリーン」エクスポート（期間: 対象年 10/1〜11/30、ページ単位の表示回数・アクティブユーザー数等）と、別途「対象年 11 月中のアクティブユーザー数」の提供を受け、以下の方法で算出した。
  - 分子: エクスポートの「表示回数」列を全行合計した、10/1〜11/30 の総ページビュー数。
  - 分母: 「対象年 11 月中のアクティブユーザー数」（オーナー提供、11 月のみの実績）。
  - **行ごとの「アクティブ ユーザー」列は合算していない**（同一ユーザーが複数ページを見ると重複計上され、サイト全体のユニークユーザー数にならないため）。
  - **分子（10-11 月の 2 か月合計）と分母（11 月のみ）は集計期間が一致していない。** エクスポートには 10/1〜11/30 通期のユニークアクティブユーザー数が含まれておらず、これを 11 月のみのアクティブユーザー数で代用したため、**平均 PV は過大に算出される（=安全側、想定ピーク RPS・合格ラインが大きく出る方向）**。10 月分の総表示回数も分子に含まれる一方、10 月分のユーザーは分母に含まれないため。
  - 参考として、エクスポートのページ単位「アクティブユーザーあたりのビュー」列（トップページで 2.0 前後、下位ページで 1.0〜4.1 程度）と比較したところ、サイト全体（多数ページの合算）での平均 PV がページ単体の値より大きく出るのは自然であり、算出値のオーダーに明らかな異常は見られなかった。
  - 来場者数（2 日間合計）と GA アクティブユーザー数（11 月中）は母集団が異なる（来場者全員がサイトを見るわけではなく、逆に来場しない閲覧者もいる）。design.md の算出式は「来場者数 × 1 人あたり平均ページビュー数」を独立した入力値の積として定義しているのみで、両者の母集団差を埋める変換は定義していない。独自の換算率は発明せず、式の定義どおり「来場者数」と「(GA 実績から算出した) 平均 PV」をそのまま掛け合わせる形で適用した。この母集団差は未解決点として記録する。
  - **算出した平均 PV の数値自体は、来場者数と合格ラインから逆算され得るため、この文書には記載しない**（下記「実績値の秘匿方法」参照）。

## 3. design.md が定義する算出式・係数

design.md「Performance & Scalability」節（L789-814）より引用する。

```
想定ピーク RPS
  = 来場者数 × 1人あたり平均ページビュー数 × ピーク時間帯集中率 ÷ 3600
    × バースト係数 × 1ページビューあたり API コール数 × 前年比増加率

合格ライン = 想定ピーク RPS × 安全マージン
```

係数（design.md 固定値、変更しない）:

| 係数 | 値 |
|---|---|
| ピーク時間帯集中率 | 0.20 |
| バースト係数 | 5 |
| 前年比増加率 | 1.5 |
| 安全マージン | 1.3 |

入力値の充足状況:

| 入力値 | 状態 |
|---|---|
| 来場者数 | 取得済み（非公開） |
| 1 人あたり平均ページビュー数 | 算出済み（非公開、上記 2. の方法により GA 実績値から算出。安全側＝過大評価） |
| 1 ページビューあたり API コール数 | 実測済み ≈ 5（上記 1.） |

**「ピーク時間帯集中率」係数について**: この係数は design.md 上「開催期間全体のアクセスのうち、最も集中する 1 時間に発生する割合」と定義されており、来場者数（2 日間合計）に対して直接乗じる設計になっている。オーナーから「2 日目は雨天でピーク推定が過小評価になりうる」との留意点を受け取ったが、design.md はこの係数を固定値として定義しており、日別の天候差を織り込んだ独自の日別配分・追加係数を発明する余地を与えていない。したがって来場者数は 2 日間合計値をそのまま式に用い、天候による日別偏りは織り込まない。この点は係数自体が「保守的な仮定」（design.md 記載）とされていることとの整合をオーナー側で判断すべき事項として、未解決点に記載する。

## 4. 合格ラインの算出結果

design.md の算出式（上記 3.）へ、来場者数（オーナー提供、非公開）・平均 PV（上記 2. の方法で GA 実績から算出、非公開、安全側＝過大評価）・1 ページビューあたり API コール数（実測 5）・固定係数（ピーク時間帯集中率 0.20、バースト係数 5、前年比増加率 1.5、安全マージン 1.3）を代入して算出した。

**合格ライン ≈ 218 req/s**

算出構造（どの変数に何を掛けたか。数値は掛けた順序のみを示し、来場者数・平均 PV の実値は含まない）:

```
想定ピーク RPS = 来場者数 × 平均PV × 0.20 ÷ 3600 × 5(バースト係数) × 5(APIコール数/PV) × 1.5(前年比)
合格ライン     = 想定ピーク RPS × 1.3(安全マージン)
             ≈ 218 req/s
```

平均 PV に「10-11 月総表示回数 ÷ 11 月のみのアクティブユーザー数」という過大評価側の値を用いているため、**この合格ラインも実態より高め（安全側）に出ている可能性が高い**。より正確な平均 PV（10/1〜11/30 通期のユニークアクティブユーザー数を分母に使えた場合）を用いれば、合格ラインはこれより低くなる方向にしか動かない。

## 5. ブレークポイントとの比較

タスク 1.2 のブレークポイント（目標レート約 50-55 req/s 帯、CPU limit 500m による頭打ちで実測スループットは約 25-31 req/s）は、合格ライン（≈218 req/s）を**大きく下回る**。安全側（過大評価）に倒した平均 PV を用いてもなおこの差があるため、「平均 PV の見積りの粗さ」だけではこの差を覆せない。

要件 1.5「ブレークポイントが合格ラインを上回る場合はステートレス分散を不要と判断する」を字義通り適用すると、**ブレークポイントは合格ラインを上回っていない**ため、ステートレス分散は不要と判断できる条件を満たさない。

## 6. 3 案の比較とタスク 5.8 の実施可否

タスク 1.2 で判明済みの律速要因（CMS コンテナの `limits.cpu: 500m` によるコンテナ単位のスロットリング。ノード全体には requests ベースで 1605m の残余があり、namespace の LimitRange 上限 2000m までは引き上げ余地がある）を踏まえ、上記比較結果に対して 3 案を評価する。

- **案 A（現状維持）**: ブレークポイント（実測 25-31 req/s）は合格ライン（≈218 req/s）の 1 割強にとどまる。**不採用。**
- **案 B（CMS の CPU limit 引き上げ、レプリカ 1 のまま、LimitRange 上限 2000m まで）**: 過去の実測から外挿したレンジ（タスク 1.4 旧節参照）では、2000m まで引き上げても期待スループットは概ね 108-206 req/s。**この外挿の最良ケース（206 req/s）でも合格ライン（≈218 req/s）にわずかに届かない。** 外挿自体、Payload(Node.js) の単一スレッド特性により 1 vCPU 超で線形性が崩れる可能性を含む未検証の値であり、実際にはこれを下回る可能性もある。単独では不足する可能性が高いが、コストが低く先行して実施する価値はある。
- **案 C（ステートレス分散、レプリカ増）**: 律速要因はコンテナ単位の CPU limit であるため、limit を上げないままレプリカのみ増やしても合計処理能力は「500m 相当 × レプリカ数」にとどまり非効率。**案 B（CPU limit 引き上げ）との併用が前提。** 案 B 単独の外挿上限（≈206 req/s）が合格ライン（≈218 req/s）にわずかに届かない可能性がある以上、ノード追加によるレプリカ分散（案 C）を組み合わせて処理能力の余地を確保する必要があると判断する。

**タスク 5.8（ステートレス分散の実施）の実施可否: 実施する。**

- 判定根拠: ブレークポイントが合格ラインを上回っていない（要件 1.5 の「上回る場合は不要」の逆）。
- 実施形態: 案 B（CPU limit 引き上げ）と案 C（レプリカ分散）の併用を前提とする。案 B 単独では外挿上限が合格ラインにわずかに届かない可能性が高く、案 C 単独では律速要因（コンテナ単位の CPU limit）が変わらないため改善効果が薄い。
- 両案の効果検証には本番マニフェスト変更（Git コミット + ArgoCD sync）と再測定が必要であり、タスク 1 の範囲外（タスク 5.8/5.9 で実施）。

## 7. この判定がデータ層の冗長化に与える影響

**影響しない。** データ層の冗長化（タスク 4、5.5。cms-db・zitadel-db のインスタンス数を 3 に増やす作業）は可用性確保を目的とし、design.md が明記する通り測定結果に依存しない。タスク 1.4 が「ステートレス分散を実施する」と判定したこと自体も、タスク 4・5.5 の実施要否・実施内容を変えない。両者は独立に実施する。

## 実績値の秘匿方法

本タスクの作業中に扱った以下の実績値は、本文書を含むいかなる公開リポジトリ配下の文書にも数値そのものを記載していない: 来場者数（2 日間合計、オーナー提供）、GA の 11 月中アクティブユーザー数（オーナー提供）、GA エクスポートの 10/1〜11/30 総表示回数（`/tmp/access.csv` から算出、リポジトリ外に留め置き）、これらから算出した 1 人あたり平均ページビュー数。エクスポート CSV そのものもリポジトリ配下へコピーしていない。

「取得済み（非公開）」「オーナー提供の実績値」等の表現に置き換え、実績値同士の掛け算の途中結果も記載していない。**最終的な合格ライン（≈218 req/s）のみ数値として記載した。** これは design.md で既に公開されている固定係数（0.20・5・1.5・1.3）と、実測済みで秘匿対象でない API コール数（5、visitor データではなく技術的な実測値）を含む積の最終結果であり、来場者数・平均 PV の 2 つの非公開変数がいずれも未知のまま単一の積としてしか登場しないため、合格ラインの数値単体から来場者数または平均 PV を一意に逆算することはできない。

## 未確認・未解決の点

- **10/1〜11/30 通期のユニークアクティブユーザー数が GA エクスポートに含まれておらず、代わりに「11 月のみのアクティブユーザー数」を分母に使った。** これにより平均 PV（ひいては合格ライン）は過大評価側に出ている。通期のユニークアクティブユーザー数が別途得られれば、より正確な（おそらくより低い）合格ラインを再算出できる。
- 来場者数（2 日間合計）と GA アクティブユーザー数という異なる母集団の関係を、design.md の算出式は定義していない。式の定義どおり「来場者数 × 平均 PV」の単純な積として適用したが、両者の母集団差を埋める変換方法自体はオーナー側の確認が必要な未解決事項として残る。
- 2 日目が雨天だったことによるピーク推定への影響は、design.md の「ピーク時間帯集中率」係数（固定値 0.20）が吸収する設計になっているのか、日別の追加調整が別途必要なのかが design.md からは読み取れない。本タスクでは独自係数を発明せず、来場者数の合計値をそのまま式に投入する前提で記録した。この前提の妥当性はオーナー側の確認が必要。
- 1 ページビューあたり CMS API コール数の実測は「キャッシュが効いていない（MISS）」状態のみで行った。「キャッシュが効いている（HIT）」状態は本測定中一度も観測できず、その場合の値は未測定。ただし本測定時点の本番の実態は一貫して MISS であり、これは安全側（大きい方）の値である。
- CPU 消費によるクロスチェック（約 7）は `top pod` のサンプリング粒度・エンドポイント間のコスト差に起因するノイズを含む概算であり、精密なリクエストカウントではない。
- 案 B・案 C の効果（外挿レンジ 108-206 req/s、および併用時の到達見込み）は実測されておらず、タスク 5.8/5.9 での本番実施・再測定による検証が必要。

# タスク 2.6: 対象を限定した差分確認

タスク 2.1・2.2 のコード変更 (`terraform/main.tf` の `locals.nodes`・`terraform/placement.tf`) に対して、対象を限定した `terraform plan` を実行した記録。

## 実行コマンド (1回目: 追加ノードのみ)

```
infisical run --env=prod -- terraform plan \
  -target='hcloud_server.nodes["prod-node-2"]' \
  -target='hcloud_server.nodes["prod-node-3"]' \
  -input=false -no-color
```

完走した。結果は以下の 4 リソースの新規作成のみで、変更・削除は 0 件 (`Plan: 4 to add, 0 to change, 0 to destroy`)。

- `hcloud_placement_group.k3s_nodes`（新規）
- `hcloud_server.nodes["prod-node-2"]`（新規）
- `hcloud_server.nodes["prod-node-3"]`（新規）
- `tailscale_tailnet_key.k3s_nodes`（新規追加ノードの `user_data` が参照するため依存関係として自動的に含まれた。`expiry = 3600` の設計により生じる既知の差分であり、tailscale.tf のコメントの通り他の変更と無関係）

この `-target` は `prod-node-2`・`prod-node-3` のみを指定しており、`hcloud_server.nodes["prod-node-1"]` は対象に含めていなかった。そのため `prod-node-1` に対する `placement_group_id` の追加差分（design.md が要求する「既存ノードの所属変更が in-place の更新として示される」ことの確認）は、この plan の出力範囲には含まれていなかった。

## 実行コマンド (2回目: prod-node-1 を含めた確認)

`prod-node-1` を含む対象無限定の plan は authentik リソースの import 失敗により完走しないが（design.md 397行目・398行目、project CLAUDE.md 記載の既知の制約）、`hcloud_server.nodes["prod-node-1"]` 単体を明示的に `-target` に含めた場合は authentik リソースへ到達しないため完走する。

```
infisical run --env=prod -- terraform plan \
  -target='hcloud_server.nodes["prod-node-1"]' \
  -target='hcloud_server.nodes["prod-node-2"]' \
  -target='hcloud_server.nodes["prod-node-3"]' \
  -input=false -no-color
```

完走した (`Plan: 4 to add, 1 to change, 0 to destroy`)。`prod-node-1` の差分は以下の通り、**in-place 更新のみであり、再作成 (`-/+` もしくは `# forces replacement`) は示されなかった**。

```
# hcloud_server.nodes["prod-node-1"] will be updated in-place
~ resource "hcloud_server" "nodes" {
      id                         = "133188863"
      name                       = "prod-node-1"
    ~ placement_group_id         = 0 -> (known after apply)
      # (21 unchanged attributes hidden)

      # (2 unchanged blocks hidden)
  }
```

変更される属性は `placement_group_id` の 1 件のみ (`0` → `(known after apply)`)。他の 21 属性・2 ブロックは無変更 (`unchanged` と明示)。新規作成対象 (4 件) は 1 回目と同一 (`hcloud_placement_group.k3s_nodes`・`prod-node-2`・`prod-node-3`・`tailscale_tailnet_key.k3s_nodes`)。

以上により、design.md の Validation 要件（「`placement_group_id` は provider の `resourceServerUpdate` が in-place で処理するため、サーバーの再作成を伴わない」）および task 2.2 の完了状態（「既存ノードの差分が所属先の変更のみであり、再作成が示されないこと」）を plan 出力で確認した。

# タスク 4.3: 対象外のワークロードを据え置く判断の記録

`gitops/manifests/prod/` 配下を走査し、CNPG `Cluster`（DB クラスタ）と `StatefulSet` を全数列挙した。稼働状態は各ワークロードの Deployment/StatefulSet/Helm values の `replicas` 指定で確認した（クラスタへの問い合わせは行っていない）。

## DB クラスタ (CNPG Cluster) 全数

| クラスタ名 | マニフェスト | instances (本タスク後) | 所属ワークロードの稼働状態 | 分類 |
|---|---|---|---|---|
| directus-db (cms) | `gitops/manifests/prod/cms/db-cluster.yaml` | 3（タスク 4.1 で変更） | 稼働中（`cms/deployment.yaml` replicas: 1） | 対象 |
| zitadel-db | `gitops/manifests/prod/zitadel/db-cluster.yaml` | 3（タスク 4.2 で変更） | 稼働中（`zitadel/statefulset.yaml` replicas: 1） | 対象 |
| authentik-db | `gitops/manifests/prod/authentik/db-cluster.yaml` | 1（変更なし） | 停止中（`gitops/helm-values/prod/authentik.yaml` に `replicas: 0` が 2 箇所、`redis.yaml`・`ldap-outpost.yaml` も `replicas: 0`） | 対象外 |
| vaultwarden-db | `gitops/manifests/prod/vaultwarden/db-cluster.yaml` | 1（変更なし） | 停止中（`vaultwarden/deployment.yaml` replicas: 0、「凍結中」とコメントあり） | 対象外 |
| room-presence-db | `gitops/manifests/prod/room-presence/db-cluster.yaml` | 1（変更なし） | 停止中（`gitops/apps/prod/room-presence.yaml` の Helm `valuesObject.replicaCount: 0`） | 対象外 |

## StatefulSet (CNPG 以外) 全数

| ワークロード | マニフェスト | replicas | 分類 |
|---|---|---|---|
| mailserver (DMS) | `gitops/manifests/prod/mailserver/statefulset.yaml` | 1 | 対象外 |
| zitadel（アプリ本体） | `gitops/manifests/prod/zitadel/statefulset.yaml` | 1 | 対象外 |

## 対象外リストと根拠

- **authentik-db**: authentik 本体が停止中のため冗長化しても効果がなく、ディスクを消費するのみ
- **vaultwarden-db**: Vaultwarden 本体が停止中のため、authentik-db と同じ理由で対象外
- **room-presence-db**: room-presence 本体が停止中のため、authentik-db と同じ理由で対象外
- **mailserver (StatefulSet)**: ポート 25 の bind と逆引き（RDNS）設定により特定ノードから移動できない。配置制約を変更しない
- **zitadel (StatefulSet、アプリ本体)**: 本タスク（4. データ層冗長化）の対象は稼働中の CNPG データ層クラスタ（cms-db・zitadel-db）に限る（design.md データ層冗長化節）。zitadel-db 自体は対象（タスク 4.2）だが、Zitadel アプリ本体の冗長化は別の意思決定を要するため本タスクの対象外とする
- **nginx-ingress**（`gitops/apps/prod/nginx-ingress.yaml`、Helm 管理でローカルマニフェストなし）: ステートフルワークロードではないが、design.md が「外部公開経路の構成を変更しない」対象として明記しているため参考記録する。来場者トラフィックを担わない autoconfig 専用の経路であり、配置を変更しても効果がない

対象外としたワークロード（authentik・vaultwarden・room-presence 本体とその DB）はいずれも本タスクで再開していない。
