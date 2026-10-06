# Research & Design Decisions

## Summary
- **Feature**: `ops-dashboard`
- **Discovery Scope**: New Feature / Complex Integration (新規サービス 1 つ + mailserver・Falco・Zitadel・Terraform・Ansible への統合点と、十数種類の外部 API)
- **Key Findings**:
  - 情報源の大半は「既製ダッシュボードのウィジェットでは表現できない計算・保存」(課金見込み、プラン期間、Falco/DMARC/TLS-RPT の蓄積、宣言との差分) を要する。既製ダッシュボード (Homepage 等) を置いても集計役は別に必要になるため、集計と描画を 1 つの小さな collector にまとめるのが最もメモリと保守点が少ない。
  - mailserver の内部状態 (fail2ban の DB、Postfix のキュー、メールログ) はコンテナのファイルシステム上にしかなく、`kubectl exec` 以外の取得経路がない。docker-mailserver は `/var/mail-state` が存在すると状態をそこへ集約する仕様のため、状態とログを PVC に出し、読み取り専用の別 Pod から読む構成にすると exec 権限を配らずに済む。
  - 外部 API は一次情報で確認した結果、請求額を取得できるのは Cloudflare (subscriptions) と GitHub (enhanced billing) だけで、他は使用量の件数 API と、コードで宣言した上限値の対比になる。

## Research Log

### 既存の構成と統合点
- **Context**: 新規サービスの置き場所と、既存リソースへの変更点を確定するため。
- **Sources Consulted**: `.kiro/steering/{tech,structure,dr}.md`、`gitops/apps/prod/*.yaml`、`gitops/manifests/prod/{mailserver,zitadel,cms}/`、`gitops/helm-values/prod/falco.yaml`、`gitops/manifests/shared/eso/falcosidekick-external-secret.yaml`、`terraform/{tunnel,dns,firewall,main,providers}.tf`、`ansible/roles/{zitadel-bootstrap,os-auto-update}`、`.github/scripts/k3s-version-check.sh`
- **Findings**:
  - 公開経路は Cloudflare Tunnel から ClusterIP への直結。ホスト名ごとに `tunnel.tf` の ingress_rule と `dns.tf` の CNAME を足すパターン。
  - Zitadel の OIDC アプリは `zitadel-bootstrap` の `zitadel_oidc_apps` で宣言し、`infisical_keys` を書くと発行値を Infisical へ自動登録する (argocd が前例)。machine user は `zitadel_machine_users` で宣言する。
  - Falcosidekick の設定は `falco.yaml` の `falcosidekick.config`、秘匿値は `falcosidekick-secrets` (ExternalSecret、envFrom) で渡している。
  - mailserver は `hostNetwork`、`nodeSelector: prod-node-1`、データは `mailserver-data` (local-path) のみ永続化。`/var/mail-state` と `/var/log/mail` はマウントしていない。
  - `postmaster@` は `postfix-virtual.cf` で `admin@` への alias。DMARC (`_dmarc`) と TLS-RPT (`_smtp._tls`) の rua はどちらも `postmaster@`。
  - kustomize を使う Application は `cms` のみで、他は素のマニフェスト。
  - Cilium が NetworkPolicy を担当する (k3s は `--disable-network-policy`)。
  - zitadel Application は自動同期しない (手動 sync)。
  - K3s の最新版は `update.k3s.io/v1-release/channels` の `stable` を正とする (既存の週次チェックと同じ)。
- **Implications**: portal と運用ダッシュボードは 1 つのホスト名・1 つの Application (`ops-dashboard`) に収める (steering の「サブドメイン・Application を不用意に増やさない」)。mailserver 側の読み取り役は mailserver Application に置く。

### Zitadel: ログイン後の遷移先と認証イベント
- **Context**: 要件 1.1 と 12。
- **Sources Consulted**: zitadel/zitadel `apps/login/src/lib/client.ts` (v4.19.2 タグ)、`cmd/defaults.yaml` (v4.19.2)、`internal/repository/user/*.go`、https://zitadel.com/docs/guides/integrate/zitadel-apis/event-api
- **Findings**:
  - login v2 の `resolveRedirectUri` は、OIDC リクエストを伴わずにフローが完了したとき「環境変数 `DEFAULT_REDIRECT_URI` → 組織設定の defaultRedirectUri → 組み込みの signedin ページ」の順で遷移先を決める (v4.19.2 で確認)。
  - イベント取得は Admin API `POST /admin/v1/events/_search` (ListEvents)。権限 `events.read` を持つ instance ロールは `IAM_OWNER` と `IAM_OWNER_VIEWER` だけ。読み取り専用の最小は `IAM_OWNER_VIEWER`。
  - 認証失敗のイベント種別: `user.human.password.check.failed`、`user.human.mfa.otp.check.failed`、`user.human.otp.sms.check.failed`、`user.human.otp.email.check.failed`、`user.human.passwordless.token.check.failed`、`user.locked`。パスワード検証は Session API (login v2・Dovecot の Lua 連携) と旧フローで共通の関数を通り、失敗時に `user.human.password.check.failed` を発行する。
- **Implications**: 遷移先は login コンテナの env で宣言する。イベント取得用に `IAM_OWNER_VIEWER` の machine user を追加する。`IAM_OWNER_VIEWER` はインスタンス全体を閲覧できるため、PAT は collector だけが読む Secret に限定する。

### oauth2-proxy + nginx によるサーバー側の出し分け (spike で検証済み)
- **Context**: 要件 1・3・4。Cloudflare Access は利用者数の上限 (50) があり全委員向けに使えない。
- **Sources Consulted**: ローカルの検証環境 (mock OIDC + oauth2-proxy v7.15.5 + nginx 1.27 + Homer v26.08.3)、oauth2-proxy `pkg/apis/options/providers.go`・`providers/provider_data.go`
- **Findings**:
  - `--set-xauthrequest` で `X-Auth-Request-Groups` がカンマ連結で返る。`/oauth2/auth?allowed_groups=<g>` は未認証 401、グループ不一致 403、一致 202。
  - nginx の `rewrite`/`return` は `auth_request` より前に評価されるため、グループによるファイル切り替えは `try_files` で行う必要がある。
  - `auth_request /oauth2/auth?allowed_groups=...` と書くと `?` がエスケープされて別 location に落ちる。グループ判定ごとに internal location を作り、`proxy_pass` 側にクエリを書く。
  - グループの完全一致は `",$groups,"` に `",admin,"` が含まれるかで判定する (部分一致の誤判定を検証済み)。
  - Homer の service worker は `config.yml` をキャッシュしない。応答に `Cache-Control: private, no-store` を付ける。
  - Homer 設定の admin 版は、単一ソースから yq の 1 式で生成できる。
  - Zitadel の `groups` claim は Action により userinfo と access token に入り、ID token には入らない。oauth2-proxy は ID token に無い claim を userinfo で補うが、アプリに `id_token_userinfo_assertion: true` を付けて ID token に入れるのが確実 (cloudflare-access アプリと同じ扱い)。
  - アイドル時メモリ: nginx 約 13MiB、oauth2-proxy 約 13MiB。
- **Implications**: portal は nginx + oauth2-proxy の 1 Pod。Homer は静的ファイルとして nginx から配信し、専用コンテナを持たない。

### 運用ダッシュボードの実装形態
- **Context**: 情報源が多く (課金 API、k8s リソース、Falco の蓄積、メールボックスの取り込み、Zitadel、GitHub)、計算と保存を伴う。
- **Sources Consulted**: gethomepage.dev (customapi ウィジェット)、my-home-network の Homer 採用記録、今回の各 API 調査
- **Findings**:
  - Homepage の customapi ウィジェットは JSON のフィールドを表示できるが、期間比較・見込み費用・プラン期間判定・履歴グラフは表現できない。Falco/DMARC/TLS-RPT の保存先も別に要る。
  - Python 標準ライブラリだけで、HTTP クライアント (urllib)、SQLite、zip/gzip、XML、JSON、Maildir (mailbox)、HTTP サーバー、TOML 読み込み (tomllib) がそろう。S3 の SigV4 署名も hmac/hashlib で書ける。
  - RSA 署名 (GitHub App の JWT) は標準ライブラリにない。ESO の `GithubAccessToken` generator (v0.16 で利用可) がインストールトークンを発行・更新するため、collector は署名を持たなくてよい。
- **Implications**: 外部パッケージを持たない Python の collector を ConfigMap で配布し、公式 Python イメージ (digest 固定) で動かす。独自イメージのビルド・レジストリ運用を発生させない。

### mailserver の状態取得
- **Context**: 要件 11.1・14・15・11.2。
- **Sources Consulted**: docker-mailserver v15.1.0 `target/scripts/startup/setup.d/mail_state.sh`、`target/logwatch/maillog.conf`、`setup.d/log.sh`
- **Findings**:
  - `/var/mail-state` がディレクトリとして存在すると、`spool/postfix`・`lib/postfix`・`lib/fail2ban` (ENABLE_FAIL2BAN=1 時)・`lib/rspamd`・`lib/dovecot` 等をそこへ移してシンボリックリンクを張る。
  - メールログは `/var/log/mail/mail.log` (logrotate あり)。
  - fail2ban の DB は既定で `/var/lib/fail2ban/fail2ban.sqlite3` → 集約後は `/var/mail-state/lib-fail2ban/fail2ban.sqlite3`。
  - Dovecot の配送先は `/var/mail/<domain>/<localpart>` (Maildir)。
- **Implications**: mailserver に `/var/mail-state` と `/var/log/mail` 用の PVC を追加し、別 Deployment の mail-agent が読み取り専用でマウントする。DMARC/TLS-RPT は専用の配送専用アドレスへ fan-out し、その Maildir だけを別 PVC として入れ子マウントする。

### 外部サービスの課金・利用枠 API
- **Context**: 要件 5。
- **Sources Consulted**: Hetzner Cloud OpenAPI (`docs.hetzner.cloud/cloud.spec.json`)、Cloudflare OpenAPI (`cloudflare/api-schemas`)、developers.cloudflare.com (GraphQL 認証、Workers/R2 料金)、docs.github.com/en/rest/billing/usage、HCP Terraform Explorer API、Tailscale OpenAPI (`api.tailscale.com/api/v2?outputOpenapiSchema=true`)、Netdata Cloud swagger と terraform-provider-netdata、uptimerobot.com/api/v2、healthchecks.io/docs/api、infisical.com/pricing
- **Findings**:
  - **Hetzner Cloud**: 請求 API なし。`GET /servers` に当期の `outgoing_traffic`・`included_traffic`、`GET /pricing` に `price_hourly`・`price_monthly`・`price_per_tb_traffic`。読み取り専用の API トークンを発行できる。
  - **Hetzner Object Storage**: 管理 API なし。使用量は S3 互換 API の ListObjectsV2 でサイズを合計する。認証情報はプロジェクト単位で読み取り専用の区別がない。
  - **Cloudflare**: `GET /accounts/{id}/subscriptions` (Billing Read) で契約中のプランと料金。Workers の当期リクエスト数は GraphQL `workersInvocationsAdaptive`、R2 は `r2StorageAdaptiveGroups`・`r2OperationsAdaptiveGroups` (Account Analytics Read)。Zero Trust の利用者は `GET /accounts/{id}/access/users` (Access: Audit Logs Read)。トンネル状態は `GET /accounts/{id}/cfd_tunnel/{id}` (Cloudflare Tunnel Read)。seat 数の上限を返す API はない。Workers Free は 10 万リクエスト/日、Paid は月 1,000 万リクエストまで基本料金に含まれる。R2 無料枠は 10GB-月・Class A 100 万/月・Class B 1,000 万/月。
  - **GitHub**: `GET /organizations/{org}/settings/billing/usage` (Administration: read)。GitHub App のインストールトークンで使える。対象 organization で応答を確認済み (enhanced billing 対象)。
  - **HCP Terraform**: Explorer API `GET /api/v2/organizations/{org}/explorer?type=workspaces` の `current-rum-count` が Free プランで取得できることを確認。`GET /organizations/{org}/subscription` の feature-set 名 (`Free`) で実プランを判別できる。上限値 (500) は API で返らない。Free プランではチームが owners のみのため、読み取り専用トークンは作れない。
  - **Tailscale**: `GET /tailnet/{tailnet}/devices` (devices:core:read、`connectedToControl`・`lastSeen`)、`GET /tailnet/{tailnet}/users` (users:read)。Terraform provider ~> 0.29 に `tailscale_oauth_client` があるが、Terraform 用クライアントに `oauth_keys` スコープがなく使わない。tailnet のプランは Free。
  - **Netdata Cloud**: 公開 swagger にノード一覧はないが、`GET /api/v2/spaces/{space}/rooms` と `GET /api/v2/spaces/{space}/rooms/{room}/nodes` が `scope:grafana-plugin` のトークンで 200 を返すことを確認した。API トークンは発行したアカウントに紐づく。
  - **UptimeRobot**: v2 `getAccountDetails` が `monitor_limit`、`getMonitors` が状態を返す。読み取り専用キーあり。Free は 10 req/分。
  - **Healthchecks.io**: `GET /api/v3/checks/` は読み取り専用キーで状態を返す。上限値の API はない。
  - **Infisical**: 利用量 API なし。identity 数は組織レベルの一覧で数える必要があるが、既存の K3s 用 identity は組織権限を持たず 403。Free は人とマシンの合算で 5 identities で、専用 identity の作成は上限到達で失敗した。
- **Implications**: プラン名・上限値・警告閾値・適用期間・常時稼働すべきサーバーの集合は collector の宣言ファイルに置く。実プランを API で取れるもの (Cloudflare subscriptions、HCP subscription) は宣言と突き合わせて戻し忘れを検出する。

### Falco の検知イベントの収集
- **Context**: 要件 13.1 (既存の Discord 通知を止めたり置き換えたりせずに収集する)。
- **Sources Consulted**: falcosidekick `docs/outputs/webhook.md`
- **Findings**: Falcosidekick は出力先ごとに独立して送信し、`webhook.address` を設定すると webhook 出力が有効になる。`webhook.customheaders` で認証ヘッダーを付けられる。送信内容は `output`・`priority`・`rule`・`time`・`output_fields`・`hostname`・`tags`・`source`。
- **Implications**: Falcosidekick に出力先を 1 つ追加する。webhook 側の失敗は Discord 出力に波及しない。

### 本仕様のコンポーネント自身が Falco に検知されるか
- **Context**: collector は k8s API を定期的に読む。mail-agent は root で他ユーザー所有のファイルを読む。
- **Sources Consulted**: `gitops/helm-values/prod/falco.yaml` (rules-custom、falcosidekick の minimumpriority)
- **Findings**: collector は上流ルール「Contact K8S API Server From Container」(NOTICE) に当たり、Discord 出力の `minimumpriority: notice` を満たすため定常的に通知される。mail-agent・nginx・oauth2-proxy・initContainer は k8s API 接続・`/etc` 書き込み・シェル実行・`sensitive_file_names` の読み取りを行わず、投入中のルールに当たらない。
- **Implications**: collector だけを既存の除外マクロ `user_known_contact_k8s_api_server_activities` に Namespace + イメージで追記する。統計 (13.2) が自分自身の検知で埋まることも防げる。

### Zitadel の OIDC アプリと refresh token
- **Context**: 要件 4.5。oauth2-proxy の `--cookie-refresh` は refresh token で ID token を取り直す。
- **Sources Consulted**: `ansible/roles/zitadel-bootstrap/tasks/_oidc_app.yml`
- **Findings**: 作成・更新の API 呼び出しで `grantTypes` が AUTHORIZATION_CODE 固定になっており、refresh token が発行されない。更新要否の判定にも grant type が含まれていない。
- **Implications**: 任意の `grant_types` パラメータを追加し (既定値は従来どおり)、判定条件に grant type の差分を加える。`ops-portal` だけが REFRESH_TOKEN を宣言する。

### 境界の考え方
- **Context**: 当初の requirements は「既存監視の設定・ルール・通知先を変更しない」と書いており、Falco の除外マクロへの追記や Falcosidekick の送信先追加まで禁じる読み方ができた。
- **Findings**: 制約の本旨は「既存の監視・通知ツールを情報源として残し、置き換えないこと」で、本仕様のコンポーネントを組み込むための追加的な設定変更 (Tunnel の経路追加と同種) は禁じていない。
- **Implications**: 範囲外は「既存ツールの置き換え」と「既存ワークロードに対する検知・通知・遮断の方針の変更」に限定し、追加的な統合変更を範囲内に明記した。

## Architecture Pattern Evaluation

| Option | Description | Strengths | Risks / Limitations | Notes |
|--------|-------------|-----------|---------------------|-------|
| A. Homepage + customapi + 別の集計役 | 表示は Homepage、計算・保存は別プロセス | 既製 UI | 2 プロセス・2 設定体系。Homepage 約 100MB 級。グラフ・期間判定は結局集計役側 | 不採用 |
| B. collector が集計・保存・描画を担う (採用) | 標準ライブラリだけの Python 1 プロセスが情報源ごとに取得し、SQLite に保存し、HTML をサーバー側で描画 | 1 プロセス・1 設定ファイル・外部依存なし。グラフはインライン SVG で JS ライブラリ不要 | 自作コードの保守 | 情報源ごとの取得を独立させ、1 つの失敗を他へ波及させない |
| C. 情報源ごとの CronJob + 静的 HTML | 取得を CronJob に分け、結果ファイルを nginx が配信 | 取得の分離が強い | Falco の受信には常駐プロセスが必要。Pod 起動の繰り返しでメモリの瞬間値が上がる。共有ボリュームが増える | 不採用 |

## Design Decisions

### Decision: portal と運用ダッシュボードを 1 ホスト名に収める
- **Context**: steering の「サブドメインを冗長に増やさない」。
- **Alternatives Considered**: 1. `portal` と `ops` の 2 ホスト名 2. 1 ホスト名 + パス分け
- **Selected Approach**: `portal.aramakisai.com` の `/` をポータル、`/admin/` を運用ダッシュボードにする。認可は同じ oauth2-proxy で、パスごとに必要なグループを変える。
- **Rationale**: DNS・Tunnel・OIDC アプリ・cookie が 1 つで済む。
- **Trade-offs**: 両者が同じ cookie を共有する。admin 判定はリクエストごとにサーバー側で行うので問題ない。

### Decision: collector は外部パッケージを持たない Python を ConfigMap で配布する
- **Context**: 要件 16 (GitOps・バージョン固定・引き継ぎ)、要件 17 (メモリ)。
- **Alternatives Considered**: 1. 専用イメージをビルドして GHCR へ 2. 公式イメージ + ConfigMap のスクリプト
- **Selected Approach**: `python:<固定版>-slim` (digest 固定) に、kustomize の configMapGenerator で生成した `.py` 群をマウントする。
- **Rationale**: ビルド・レジストリ・イメージ更新の運用が増えない。コードの変更は Git の差分と ArgoCD の同期だけで反映される。
- **Trade-offs**: 外部ライブラリを使えない。RSA 署名は ESO generator に、YAML は TOML/JSON に置き換える。

### Decision: mailserver の状態は PVC に出し、別 Pod の mail-agent が読み取り専用で読む
- **Context**: exec 権限を配らずに fail2ban・キュー・ログ・レポートを読みたい。
- **Alternatives Considered**: 1. collector に `pods/exec` 権限 2. mailserver Pod へのサイドカー (emptyDir 共有) 3. 状態・ログを PVC に出し別 Pod から読む
- **Selected Approach**: 3。`mailserver-state`・`mailserver-logs`・`mailserver-ops-reports` の 3 つの PVC を追加し、mail-agent が読み取り専用でマウントする。
- **Rationale**: exec は任意コマンド実行と同義で読み取り専用にならない。サイドカーは mailserver の hostNetwork を共有し待受ポートがノード上に開くうえ、サイドカーの異常が mailserver Pod の Ready に影響する。別 Pod なら mail-agent の障害がメール配送に波及しない。
- **Trade-offs**: `/var/mail-state` の永続化により、Postfix のキュー・fail2ban の BAN・rspamd の学習状態が Pod の再作成をまたいで残るようになる (docker-mailserver の推奨構成と同じ挙動)。BAN の判定ポリシーや jail 定義は変わらない。
- **Accepted**: Pod 再作成をまたいで BAN・キュー・rspamd の学習状態が残る挙動の変化は、受け入れ済みのトレードオフとする。
- **Follow-up**: 投入後、mailserver 再起動時に fail2ban が DB から BAN を復元すること、Postfix がキューを引き継ぐことを確認する。

### Decision: DMARC/TLS-RPT は専用の配送専用アドレスへも配送する
- **Context**: 要件 15.1・15.6・11.2・11.4。
- **Alternatives Considered**: 1. `admin@` の Maildir を直接読む 2. `postmaster@` を `admin@` と配送専用アドレス `ops-reports@` の両方へ配送する
- **Selected Approach**: 2。`ops-reports@` の Maildir を独立した PVC として入れ子マウントし、mail-agent はその PVC だけを読む。
- **Rationale**: 人が使うメールボックス全体を読める権限を作らない。DMARC/TLS-RPT の DNS レコード (rua) は変えない。
- **Trade-offs**: `postmaster@` 宛ての他のメール (バウンス等) も `ops-reports@` に届く。collector はレポート以外を読み飛ばす。この二重配送は受け入れ済み。

### Decision: Infisical の使用量は取得せず宣言値だけを表示する
- **Context**: 要件 5.7。既存の K3s 用 identity は組織権限を持たず identity 一覧が 403 になる。専用 identity (組織ロール Member) の作成は、Free プランの上限 (人とマシンの合算) に達していて失敗した。
- **Alternatives Considered**: 1. 既存 identity に組織権限を足す (ESO 用の資格情報の権限を広げるため不採用) 2. 現在数を宣言ファイルに手で書く (公開リポジトリに利用者数を書くことになるため不採用) 3. 上限の宣言値だけを表示する
- **Selected Approach**: 3。プランと上限を宣言から表示し、「API で取得できないため使用量は表示しません」と注記する。
- **Trade-offs**: 要件 5.7 の Infisical の項目は使用量との対比ができない。上限に達すると identity・利用者を追加できなくなる形で顕在化する。

### Decision: 手動発行の資格情報は起票中にブラウザで発行し Infisical へ登録した
- **Context**: Cloudflare API トークン・GitHub App・UptimeRobot/Healthchecks.io の読み取り専用キー・HCP Terraform トークン・Netdata トークン・Hetzner Cloud トークン・Tailscale の読み取り用 OAuth クライアントは Terraform/Ansible で発行できない。
- **Selected Approach**: 本仕様の起票中にブラウザで各管理画面から発行し、値はクリップボードまたは一時ダウンロードファイル (登録後に shred) から infisical CLI で Infisical (prod のルートパス) に登録した。発行後に各 API が 200 を返すことを確認済み。ローテーションも同じ方法で行う。権限・所有アカウント・キー名・ローテーションは design.md の「手動発行する資格情報」を正とする。
- **Cloudflare**: ユーザー所有ではなくアカウント所有の API トークンにした。運用者の交代でトークンが失効しないため。
- **Netdata**: `scope:all` ではなく `scope:grafana-plugin` で必要な GET が通ることを確認し、狭い方を採用した。
- **Rationale**: 値を端末の標準出力・リポジトリ・会話ログに残さない。
- **Follow-up**: 有効期限を持つ資格情報は `dashboard.toml` に期限を宣言し、ダッシュボードで期限切れ前に気づけるようにする。

### Decision: 稼働中サーバーの差分は「宣言ファイルの期待集合」と比べる
- **Context**: 要件 5.6。`terraform/main.tf` はイベント期間だけの一時ノードも常に列挙しており、`main.tf` と比べると一時ノードの残存を検出できない。
- **Selected Approach**: collector の宣言ファイルに、サーバーごとの稼働すべき期間 (常時 / 期間指定) を書き、Hetzner の実稼働と比べる。
- **Trade-offs**: `main.tf` とは別にサーバー名を宣言する。期間付きノードを追加・削除するときに宣言も更新する (手順を運用文書に書く)。

### Decision: GitHub の認証は GitHub App + ESO GithubAccessToken generator
- **Context**: 個人の PAT は引き継ぎ時に失効する。RSA 署名を collector に持たせたくない。
- **Selected Approach**: organization 所有の GitHub App `aramakisai-ops-dashboard` (Administration: read、Actions: read、Pull requests: read、Issues: read、Metadata: read) を、`aramakisai-infra`・`aramakisai-web` だけにインストールした。ESO の generator でインストールトークン (有効 1 時間) を 30 分ごとに更新する。対象リポジトリは `dashboard.toml` に宣言し、インストール対象と一致させる。
- **Trade-offs**: GitHub App の作成は Terraform 管理外の手作業になる (運用文書に記載)。

## Risks & Mitigations
- mail-agent が読み取り専用でもメール関連の状態に触れる — PVC は `readOnly`、コンテナは root だが capability は `DAC_READ_SEARCH` のみ・読み取り専用ルート・特権昇格不可、NetworkPolicy で collector 以外からの接続を拒否し、さらに Bearer トークンを要求する。
- `IAM_OWNER_VIEWER` はインスタンス全体を閲覧できる — PAT は collector の Secret にだけ置き、collector はイベント検索 API 以外を呼ばない。
- Hetzner Object Storage の認証情報に読み取り専用の区別がない — 既存の認証情報 (CNPG・VolSync と同じ) を流用し、collector は ListObjectsV2 以外を呼ばない。
- HCP Terraform の Free プランでは読み取り専用トークンを作れない — collector 専用の organization トークンを発行し、Explorer と subscription の GET だけに使う。漏洩時は単独で失効できる。
- Netdata Cloud の API トークンが発行したアカウントに紐づく — 発行元が space を所有する運用アカウントであることを運用文書に書き、引き継ぎ時の再発行手順を書く。
- 外部 API の仕様変更 (Cloudflare の billing 系は一部 alpha) — 情報源ごとに失敗を独立させ、取得失敗として表示する。alpha の `billable-usage` は使わない。
- Tailscale の読み取り用 OAuth クライアント — Terraform が使うクライアントのスコープは `auth_keys`・`devices:core:read` だけで `oauth_keys` を持たない。Terraform の資格情報を差し替えず、秘密値を state に載せないため、管理画面から手動で発行する (`devices:core:read`・`users:read`)。

## References
- [Zitadel Event API](https://zitadel.com/docs/guides/integrate/zitadel-apis/event-api) — ListEvents と必要ロール
- zitadel/zitadel v4.19.2 `apps/login/src/lib/client.ts` — `DEFAULT_REDIRECT_URI` の優先順位
- docker-mailserver v15.1.0 `target/scripts/startup/setup.d/mail_state.sh` — `/var/mail-state` への状態集約
- [falcosidekick webhook output](https://github.com/falcosecurity/falcosidekick/blob/master/docs/outputs/webhook.md)
- [ESO GithubAccessToken generator](https://external-secrets.io/v0.16.2/api/generator/github/)
- [GitHub billing usage REST API](https://docs.github.com/en/rest/billing/usage)
- [Hetzner Cloud API spec](https://docs.hetzner.cloud/cloud.spec.json)
- [Cloudflare API schemas](https://github.com/cloudflare/api-schemas)
- [Cloudflare GraphQL API token permissions](https://developers.cloudflare.com/analytics/graphql-api/getting-started/authentication/api-token-auth/)
- [Workers limits](https://developers.cloudflare.com/workers/platform/limits/) / [R2 pricing](https://developers.cloudflare.com/r2/platform/pricing/)
- [HCP Terraform Explorer API](https://developer.hashicorp.com/terraform/cloud-docs/api-docs/explorer)
- Tailscale API OpenAPI (`https://api.tailscale.com/api/v2?outputOpenapiSchema=true`)
- netdata/terraform-provider-netdata `internal/client/node_room_member.go` — ノード一覧の呼び出し
- [UptimeRobot API v2](https://uptimerobot.com/api/v2/) / [Healthchecks.io API](https://healthchecks.io/docs/api/)
- [Infisical pricing](https://infisical.com/pricing)
