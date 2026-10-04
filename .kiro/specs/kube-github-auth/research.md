# Research & Design Decisions: kube-github-auth

## Summary
- **Feature**: `kube-github-auth`
- **Discovery Scope**: Complex Integration (kube-apiserver 認証・GitHub Actions OIDC・CSR API・k3s・ArgoCD・DR を横断)
- **Key Findings**:
  - `kubernetes.io/kube-apiserver-client` signer は subject を制限しないが、既定で有効な `CertificateSubjectRestriction` admission plugin が `O=system:masters` を含む CSR の作成を拒否する。k3s は `enable-admission-plugins=NodeRestriction` を「既定に追加」する形で渡すため、この plugin は k3s でも有効。gap-analysis の「system:masters も署名される」は誤り。ただし公式ドキュメントのとおり system:masters 以外の cluster-admin 相当 subject (例: ServiceAccount のユーザー名 `system:serviceaccount:<ns>:<name>` を CN に持つ証明書は RBAC 上その SA と同一視される) は防げないため、発行ワークフローの承認権限が cluster-admin 相当である点は変わらない。
  - Kubernetes の CEL 環境には x509 / ASN.1 / base64 デコードのライブラリがない (strings / lists / regex / URL / IP / CIDR / authorizer / quantity / semver / format のみ)。**ValidatingAdmissionPolicy で CSR の `spec.request` 内の subject を検査することはできない**。VAP で検査できるのは `spec.signerName` / `spec.usages` / `spec.expirationSeconds` / `spec.username` (作成者) などの API フィールドだけ。
  - k3s v1.36.3+k3s1 は `--authentication-config` が渡されると `anonymous-auth=false` の設定をやめる (`Not setting kube-apiserver 'anonymous-auth' flag due to user-provided 'authentication-config' file.`)。AuthenticationConfiguration 側で `anonymous.enabled: false` を明示しないと匿名認証が有効になる。
  - k3s は kube-controller-manager に `cluster-signing-kube-apiserver-client-{cert,key}-file` を client CA で渡しており、CSR API で発行した証明書は kube-apiserver の client CA で検証できる。`cluster-signing-duration` は k3s が設定しないため既定の 1 年。上限を短くするには `kube-controller-manager-arg` で明示する必要がある。
  - kubelet は `/stats/*` を常に `nodes/stats` で認可する (v1.36 で GA の fine-grained authz はそれ以外のパスにも細粒度サブリソースを追加)。**ただし API サーバー経由の `/api/v1/nodes/<n>/proxy/...` は API サーバー側で `nodes/proxy` を要求する**。`nodes/proxy` を外すには kubelet (10250) を直接叩く必要がある。

## Research Log

### Structured Authentication Configuration
- **Context**: Req 1, 6, 11.3。GitHub Actions OIDC を kube-apiserver で直接検証する。
- **Sources Consulted**: Kubernetes 公式 Authenticating (Authentication configuration from a file)、KEP-3331、k3s `pkg/daemons/control/server.go` (v1.36.3+k3s1)
- **Findings**:
  - `apiserver.config.k8s.io/v1` `AuthenticationConfiguration`。`--authentication-config` と `--oidc-*` を併用すると API サーバーはエラーで即終了する。
  - `jwt[].issuer.url` は authenticator 間で一意。GitHub 用の authenticator は 1 つにまとめ、ワークフローごとの条件は同一 authenticator の CEL 内で分岐させる必要がある。
  - `claimValidationRules[].expression` (変数 `claims`) はすべて true のときだけ認証成功。`claimMappings.username.expression` の結果には暗黙の prefix が付かない。`groups` を指定しなければグループは `system:authenticated` のみ。`userValidationRules` (変数 `user`) で最終的なユーザー名の形式を検査できる。存在しない claim は `claims.?x.orValue(...)` で安全に扱える (公式例で使用)。
  - ファイルは監視され自動再読込される。再読込時に不正ならば旧設定を維持し `apiserver_authentication_config_controller_automatic_reload_failures_total` が増える (実機確認済み。実測したメトリクス名は `…automatic_reloads_total{status="failure"}`。`anonymous` は再読込で変更できない。「実機検証結果」の 2)。issuer は起動時にオフラインでもよい (自己ホスト IdP 向けの仕様)。issuer に到達できない間は JWT が検証できず拒否され、x509 認証は影響を受けない。
  - k3s は `authentication-config` 指定時に `anonymous-auth` を設定しない。`authorization-mode` は `authorization-config` を指定しない限り `Node,RBAC` のまま。
- **Implications**: 認証設定に `anonymous.enabled: false` を必ず含める。ワークフロー許可リストは 1 authenticator 内の CEL マップで表現する。起動時の不正設定の挙動は k3d で実測し、Ansible 側では再起動後の `/readyz` 確認で失敗を検知する。

### GitHub Actions OIDC トークン
- **Context**: Req 1, 2, 3, 12。claim 照合と長時間ジョブでの再取得。
- **Sources Consulted**: GitHub Docs「OpenID Connect reference」、第三者情報 (Spacelift サポート記事ほか)
- **Findings**:
  - issuer `https://token.actions.githubusercontent.com`。`aud` の既定値はリポジトリオーナーの URL で、要求側が `&audience=` で任意に指定できる。
  - 照合に使う claim: `repository_id` / `repository_owner_id` (数値 ID。トークン内では文字列)、`ref`、`job_workflow_ref` (再利用ワークフローなら呼ばれた側のパス)、`workflow_ref`、`event_name`、`environment` (job が environment を参照したときだけ存在)、`runner_environment`、`actor` / `actor_id`。
  - トークンの有効期限は公式に明記がなく、実測で `exp - iat` = 300 秒 (「実機検証結果」の 5)。取得には `permissions: id-token: write` が必要で、`ACTIONS_ID_TOKEN_REQUEST_URL` / `ACTIONS_ID_TOKEN_REQUEST_TOKEN` はジョブ内の任意のプロセスから使える。
- **Implications**: CI・DR の kubeconfig は `exec` 資格情報プラグインで kubectl 実行ごとにトークンを取得する。数値 ID は文字列として比較する。

### CSR API と k3s の署名
- **Context**: Req 3, 5, 9。人向け短命証明書と発行ワークフローの権限。
- **Sources Consulted**: Kubernetes 公式「Certificates and Certificate Signing Requests」「Admission Controllers」、k3s `server.go`
- **Findings**:
  - `kubernetes.io/kube-apiserver-client`: kube-controller-manager は自動承認しない。subject 制限なし。key usages は `client auth` 必須で `digital signature` / `key encipherment` 以外は不可。CA ビット不可。subjectAltName と key usage 拡張は CSR から引き継がれる。期間は `--cluster-signing-duration` と `spec.expirationSeconds` (最小 600 秒) の小さい方。
  - 承認に必要な権限: `certificatesigningrequests` の get、`certificatesigningrequests/approval` の update、`signers` (resourceNames で signer 名を限定) の approve。作成は `certificatesigningrequests` の create。
  - GC: 承認・拒否・失敗済みは 1 時間後、保留中は 24 時間後に自動削除。
  - `CertificateSubjectRestriction` (既定有効): signer が kube-apiserver-client で organization に `system:masters` を含む CSR の作成を拒否。
  - X.509 の UID (OID `1.3.6.1.4.1.57683.2`) を証明書から読む機能があるが、RBAC の binding はユーザー名で照合するため識別子の不変性の担保には使えない。
  - k3s: `cluster-signing-kube-apiserver-client-*` = client CA。`cluster-signing-duration` は未設定 (既定 8760h)。
- **Implications**: 期限の上限は `kube-controller-manager-arg: cluster-signing-duration` でクラスタ側に強制する (発行ワークフローが侵害されても長期証明書を作れない)。CSR の明示削除は不要 (GC で Req 3.9 を満たす) なので、発行ワークフローに delete 権限を与えない。

### ValidatingAdmissionPolicy で CSR subject を制限できるか
- **Context**: ギャップ分析の問題 1 (発行ワークフローの権限が cluster-admin 相当)。
- **Sources Consulted**: Kubernetes 公式「Common Expression Language in Kubernetes」(CEL options, language features, and libraries)
- **Findings**: Kubernetes の CEL ライブラリ一覧に x509 / ASN.1 解析・base64 デコードはない。`spec.request` は bytes 型で、PEM 内の base64 化された DER を構造的に解釈する手段がない。文字列一致による近似は DER のエンコーディング差 (PrintableString / UTF8String、属性順序) で迂回できる。
- **Implications**: VAP による subject 制限は不採用。API サーバー側で強制できるのは期限 (cluster-signing-duration) と system:masters 拒否 (既定) まで。subject の検証は発行ワークフロー内の検証と、GitHub 側の二者承認で担保する。

### GitHub Environment の保護
- **Context**: 問題 1 の緩和策、Req 1.6。
- **Sources Consulted**: GitHub Docs「Deployments and environments」
- **Findings**: required reviewers は最大 6 名・1 名の承認で進行。「prevent self-reviews」で起動者自身の承認を禁止できる。**既定では管理者が保護ルールをバイパスできる**ため、無効化を明示する必要がある。required reviewers と wait timer は Free / Team プランでは public リポジトリでのみ利用可 (本リポジトリは public)。deployment branches は「Selected branches」で main に限定できる。environment の secrets と OIDC の `environment` claim は job が environment を参照したときだけ得られる。
- **Implications**: DR は Environment `dr-recovery` を参照させ、kube-apiserver の CEL で `environment` claim を必須にする。承認者は team `infra` とし、起動者本人の承認を認める (別メンバーの承認を求めない方針は発行ワークフローの D1 と同じ。design D9)。保護設定 (reviewers・管理者バイパス禁止・main 限定) はワークフロー冒頭で `gh api` により検査する (PR #287 と同じ方式)。発行ワークフローはユーザー決定により Environment を使わない (下記 Decision)。承認者のいない Environment に残るのは main 限定のデプロイブランチ制限だけで、`ref` / `job_workflow_ref` の照合と重複する。

### workflow_dispatch の run 特定
- **Context**: Req 4.1。手元コマンドから起動した run を確実に待つ。
- **Sources Consulted**: GitHub Changelog 2026-02-19「Workflow dispatch API now returns run IDs」
- **Findings**: dispatch API に `return_run_details` を付けると `workflow_run_id` / `run_url` / `html_url` を返す。gh CLI v2.87.0 以降の `gh workflow run` は作成された run の URL を表示する。
- **Implications**: 手元スクリプトは dispatch API の戻り値で run ID を得て `gh run watch` → `gh run download` する。相関 ID 用の input は不要。前提として gh >= 2.87.0。

### kubelet の細粒度認可と nodes/proxy
- **Context**: 問題 5 (health-check の `nodes/proxy` get は kubelet exec に転用できる)。
- **Sources Consulted**: Kubernetes 公式「Kubelet authentication/authorization」、Kubernetes Blog「v1.36: Fine-Grained Kubelet API Authorization Graduates to GA」
- **Findings**: kubelet は `/stats/*` を `nodes/stats` で認可する。v1.36 GA の fine-grained authz は `/pods`・`/healthz`・`/configz` 等にも細粒度サブリソースを足し、`nodes/proxy` へのフォールバック前に細粒度チェックを行う。いずれも kubelet 自身が SubjectAccessReview を行う直接アクセスの話で、API サーバーの node proxy サブリソース (`/api/v1/nodes/<n>/proxy/*`) は API サーバー側で `nodes/proxy` を要求する。kubelet は bearer token を TokenReview で API サーバーに検証させるため、API サーバーが受け入れる JWT (GitHub OIDC) は kubelet でも使える (k3d で確認済み)。
- **Implications**: 採用案 (D6) は「health-check が kubelet の `https://<node>:10250/stats/summary` を OIDC トークンで直接呼び、RBAC は `nodes/stats` の get だけ」。10250 への tailnet 経路と kubelet serving 証明書 (k3s では server CA 署名) の SAN は本番の読取と `tag:ci` の実ノードで確認済み (「実機検証結果」の 1)。

### k3s の CA 配布・ローテーション
- **Context**: Req 7, 9.3〜9.4、問題 3。
- **Sources Consulted**: K3s Docs「certificate」、k3s トークン形式 (`K10<CA の SHA256>::...`)
- **Findings**: クライアント・サーバー証明書は 365 日。起動時に期限まで 120 日を切っていれば自動更新 (鍵は再利用)。`k3s certificate rotate` はリーフのみで CA は変わらない。`rotate-ca` は cross-signed (非破壊) と `--force` (破壊的、旧 CA 署名のクライアント証明書は無効) がある。cross-signed (非 forced) では旧 CA 署名のクライアント証明書が引き続き受け入れられることを k3d で確認した (「実機検証結果」の 7)。`/cacerts` は 6443 (supervisor) で認証なしに server CA を返す。
- **Implications**: 旧共有 admin 証明書を確実に無効化できるのは `--force` のみ。client CA だけを差し替える forced rotation を k3d で事前検証してから本番で実施する (D7)。

### 既存コードの消費者と進行中 PR
- **Context**: Req 8, 10。
- **Findings**:
  - 本番は prod-node-1 の単一ノード (読み取り確認済み)。inventory の `k3s_server_worker` (prod-node-2/3) と terraform の定義は削除済みノードの残骸。
  - DR (`recovery.sh`) と k3s-upgrade は CI 用 SSH 鍵 (`CI_SSH_PRIVATE_KEY`) で Ansible を実行しており、ノードへの SSH 経路は既に CI に存在する。
  - PR #288 は k3s-server ロールに etcd 健全性待ち・Ready 待ちと `tasks/apply_manifests.yml` を追加している。Play 6 が kubeconfig 登録 (`OPS_INFISICAL_*`)、`infisical-auth` 作成が `ESO_INFISICAL_*`。
  - PR #287 は dr-recovery を `workflow_dispatch` + `environment: dr-recovery` + Environment 保護の実行時検査に変更。`refresh_kubeconfig` と `dr_infisical_token` が `OPS_INFISICAL_*`、`infisical-auth` 修復が `ESO_INFISICAL_*`。
  - zitadel 系 playbook はすべて手元実行 (CI からは呼ばれない)。`zitadel-bootstrap` は env `KUBECONFIG` を中身としてファイルに書き出している。
  - 過去の Tailscale ACL スナップショット (時期不明) は既定の全許可ポリシーだった。現行値は既存 OAuth client の scope 不足で取得できず (「実機検証結果」の 1)、代わりに `tag:ci` の実ノードから 6443・10250 に届くことを実測した。

## Architecture Pattern Evaluation

| Option | Description | Strengths | Risks / Limitations | Notes |
|--------|-------------|-----------|---------------------|-------|
| A: 既存入口の拡張のみ | 各ワークフローに inline で OIDC kubeconfig を組む | ファイルが少ない | kubeconfig 生成が 4〜5 箇所に重複 | gap-analysis Option A |
| B: 認証部品を全面新設 | 共通スクリプト・発行ワークフロー・login・専用 Application | 責務明確 | `make kubectl` 廃止でドキュメント改修が膨らむ | gap-analysis Option B |
| **C: ハイブリッド (採用)** | 入口 (`make kubectl`・k3s-server ロール・各ワークフロー) は拡張、共通部品 (CI 用 OIDC スクリプト・発行ワークフロー・login・RBAC) は新設 | 重複を 1 箇所に集約しつつドキュメント差分を抑える。段階移行しやすい | 段階数が多い | gap-analysis Option C |
| D: 人も SA トークン (TokenRequest) | 発行ワークフローが個人別 SA のトークンを作り、利用者の公開鍵で暗号化して返す | 剥奪が SA 削除で効く。発行権限を SA の token 作成に限定できる | ユーザー名が `system:serviceaccount:...` になり Req 3.3 の前提 (CSR・`github:` 名) と異なる。暗号化の受け渡しを自作する | ユーザー決定 (CSR 方式) により不採用。記録のみ |

## Design Decisions

### Decision: 発行ワークフローの権限 (問題 1、design D1)
- **Context**: CSR 承認権限は subject を制限できず、system:masters 以外の cluster-admin 相当 subject (SA 名など) を含む証明書を作れる。main の保護は管理者 bypass 可能で、運用上も管理者マージが使われている。
- **Alternatives Considered**:
  1. main 保護とワークフロー内検証のみ — 侵害時の防壁が実質なし。
  2. Environment `kube-cert-issue` (required reviewers・自己承認禁止・管理者バイパス禁止・main 限定) + kube-apiserver の CEL で `environment` claim 必須 — 発行ごとに別メンバーの承認が要る。
  3. VAP で subject を制限 — CEL に x509 解析がなく不可能。
  4. VAP で `spec.expirationSeconds` / `spec.usages` / 作成者を制限 — 期限は 5 の方が強く単純。usages は signer が既に制限。作成者制限は CSR 作成権限を持つ主体が発行ワークフローとクラスタ管理者しかいないため効果が小さい。
  5. `cluster-signing-duration` で期限上限をクラスタ側に強制 — 侵害時の残存期間を上限で抑える。
  6. 承認を既存の証明書保持者が `kubectl certificate approve` で行う — 初回ブートストラップと承認者不在時の可用性が悪い。
- **Selected Approach**: ユーザー決定により 2 は採らず、5 + ワークフロー内の厳格検証 (CSR は env 経由で受け、第三者アクションを使わない) + kube-apiserver の claim 照合 (ref = main、`job_workflow_ref`、`event_name` = `workflow_dispatch`、数値 ID、`github-hosted`) とする。発行は承認待ちなしで即時に完了し、write 権限保持者が単独で 7 日ごとに再発行できる。
- **Rationale**: 発行の手軽さ (承認者不在で止まらない) を優先する。承認なしの Environment は main 限定の制限しか加えず claim 照合と重複するため置かない。
- **Trade-offs**: main への悪意あるワークフロー変更がレビューを通過するか管理者 bypass でマージされると、任意の CN (SA 名や `gha:*` を含む) の証明書を最長 7 日有効で発行され得る。発行済みの不正証明書を無効にする手段は client CA の forced rotation だけである。この残存リスクを design.md の Security Considerations と steering に明記する。


### Decision: 人のユーザー名 (問題 2、design D2)
- **Context**: GitHub ユーザー名は改名・再取得できる。CN を名前だけにすると、旧名を取得した別人が binding を引き継ぐ。
- **Alternatives Considered**:
  1. `github:<login>` (要件の記載どおり) — 可読だが改名・再取得で別人に権限が移る。
  2. `github:<actor_id>` — 不変だが binding・監査で誰か読めない。
  3. `github:<login>:<actor_id>` — 両方を含む。`:` は GitHub ユーザー名に使えない文字なので区切りが曖昧にならない。
- **Selected Approach**: 3 (ユーザー決定)。発行ワークフローは CN が `github:<github.actor>:<github.actor_id>` と完全一致する場合だけ発行し、binding も同じ文字列で書く。
- **Rationale**: 改名すると binding と一致しなくなり権限が失われる (fail-closed)。再取得者は ID が異なるので一致しない。可読性も保てる。
- **Trade-offs**: 改名した利用者は binding の更新 PR が必要。Req 3.3・5.1・5.6・12.1 を `github:<GitHubユーザー名>:<数値ID>` 前提に更新済み。

### Decision: DR 直後の RBAC (問題 6)
- **Context**: DR ワークフロー用 binding が ArgoCD 同期前に無いと、recovery.sh の ArgoCD 待ちや bootstrap Secret 修復 (ArgoCD が同期できない状況を直す処理) が実行できない。
- **Alternatives Considered**:
  1. RBAC を wave -1 にして recovery.sh は 403 を許容して待つ — ArgoCD がリポジトリにアクセスできない障害 (デプロイ鍵 Secret が空) のとき修復処理自体が実行できず詰む。
  2. k3s の auto-deploying manifests (`/var/lib/rancher/k3s/server/manifests`) に置く — k3s 起動直後から存在するが、k3s の deploy controller と ArgoCD の二重管理になり、ArgoCD 管理の定義と食い違う余地がある。
  3. Ansible の ArgoCD bootstrap Play が `gitops/manifests/prod/kube-access/` 内の DR 用 binding ファイルをノード上のローカル admin で適用し、以後は ArgoCD が同じ定義を管理する (cloudflared の先行適用と同じ方式)。
- **Selected Approach**: 3。
- **Rationale**: 定義の正本は ArgoCD 管理の Git マニフェスト 1 つで、Req 2.8 の「Git 上のマニフェストで定義」を満たす。適用は bootstrap 時にローカル admin で行うので kubeconfig 不要。既存の cloudflared 先行適用と同じ例外パターンで、新しい管理経路を増やさない。
- **Trade-offs**: Git から DR binding を削除しても、次回の Ansible 実行で再作成される (Ansible は同じファイルを参照するため、ファイル自体を消せば再作成も止まる)。

### Decision: OIDC トークンの更新 (問題 4)
- **Selected Approach**: CI・DR 共通の `kube-oidc.sh` が kubeconfig を生成し、`users[].user.exec` に自身の `token` モードを登録する。kubectl は実行のたびにプラグインを呼び、プラグインは `ACTIONS_ID_TOKEN_REQUEST_URL` から都度トークンを取得して `ExecCredential` (token + expirationTimestamp) を返す。
- **Rationale**: トークン寿命 (約 5 分) とジョブ時間 (DR 最長 165 分) の差を、呼び出し側の改修なしに吸収できる。

### Decision: 認証設定の反映方式
- **Context**: 認証設定ファイルは自動再読込されるが、再読込時に不正だと黙って旧設定を維持する。
- **Selected Approach**: 認証設定ファイルの変更でも k3s を再起動する (config.yaml と同じ handler)。再起動後に `/readyz` を確認し、起動しなければ playbook を失敗させる。
- **Rationale**: Req 6.6 (不正設定の検知) を確実にする。再起動は変更時のみで、単一ノードの短時間の API 停止は許容範囲。
- **Trade-offs**: 許可ワークフローの追加でも API が短時間止まる。

### Decision: RBAC の置き場 (design D12)
- **Alternatives Considered**: 既存の `namespace-config` (wave -1、prod ns 向け LimitRange) に同居 — prune・用途の意味が混ざる。
- **Selected Approach** (ユーザー決定): 新規 Application `kube-access` (wave -1、prune・selfHeal 有効) と `gitops/manifests/prod/kube-access/`。
- **Rationale**: クラスタスコープの RBAC は既存 Application (`namespace-config` は prod ns 向け LimitRange、`argocd-config` は ArgoCD 設定) と prune の意味が異なる。binding 削除が即時剥奪になる (Req 5.3) ことと、DR bootstrap が同じディレクトリのファイルを参照することから独立させる。steering の禁止事項に対する根拠として tech.md に記録する。

### Decision: その他の決定 (design D3〜D11)

いずれもユーザーが設計の推奨案どおりに決定した。不採用案と理由を記録する。

| ID | 事項 | 採用 | 不採用案と理由 |
|----|------|------|---------------|
| D3 | server CA の入手 | `/cacerts` を tailnet 経由で取得し、同一エンドポイントの TLS 検証で整合を確認。手元は既存 CA との差分を指紋表示で検知 | CA をリポジトリに置き DR 後に更新 PR (DR 直後に PR マージまで CI・DR が接続できず Req 7.4 と衝突) / Ansible を実行する DR は SSH で CA を取得 (kubectl 用資格情報の取得に SSH を使うことになり未承認) / CA 指紋をリポジトリに置き照合 (DR 後の更新で同じ問題) |
| D4 | intrusion-response の tailnet 参加 | OAuth + `tag:ci` | 現行の API キー・タグなし参加 (Infisical 依存と `/etc/hosts` 追記が残り、タグなしノードはキー所有者の ACL を継承) |
| D5 | k3s 監査ログ | Metadata レベル、除外ルールとローテーション上限付きで有効化 | 後続に回す (識別はできても事後追跡の記録が残らない) / RequestResponse (容量が単一ノードのディスク予算に収まらない) |
| D6 | health-check のディスク使用率 | kubelet `/stats/summary` を OIDC で直接取得、`nodes/stats` の get のみ | `nodes/proxy` の get を維持 (WebSocket 経由で kubelet exec に転用可能) / kube 以外の取得元 (Netdata 等) へ移す (監視経路の作り直しが必要)。前提 (10250 への tailnet 到達と kubelet 証明書の SAN) は実機検証で成立を確認済み (「実機検証結果」の 1) |
| D7 | 旧共有 admin 証明書 | k3d で client CA のみの forced rotation を検証し、移行完了後に本番で実施 | 期限切れとクラスタ再作成を待つ (最長 365 日 system:masters が有効で漏洩の有無を否定できない) / `k3s certificate rotate` (CA が変わらず無効化できない) |
| D8 | 人の証明書の有効期限 | 7 日 + `cluster-signing-duration=168h` | 1 日 (GitHub 障害時の可用性と利便性が下がる) / 30 日 (漏洩時の残存期間が長い) |
| D9 | Environment `dr-recovery` の管理 | 手動作成 + 実行時検査 (PR #287 と同じ) | GitHub provider の Terraform 追加 (新規 provider と認証情報が必要) |
| D10 | PR #287 / #288 のマージ順序 | #288 (kubeconfig 登録 Play 削除) → #287 (`refresh_kubeconfig` を既存値の読取に縮退) → 本仕様 | 本仕様の OIDC 導入を先にして #287 を OIDC 前提に書き換える (DR 手動承認化が本仕様の進捗に待たされる) / 両 PR の Infisical 関連差分を本仕様に吸収 (PR が大きくなりレビューが遅れる) |
| D11 | DR・k3s-upgrade の権限 | DR は cluster-admin + Environment 必須、k3s-upgrade は OIDC 許可対象外 | DR 専用の最小 ClusterRole (recovery.sh の手順変更のたびに追従が要る) |

## 実機検証結果 (2026-10-04)

設計の未検証前提を実機で検証した結果。本節が design.md の根拠であり、以前の調査ログ (上の Research Log) の推測・伝聞と食い違う場合は本節が優先する。

### 検証環境と方法

| 区分 | 内容 |
|------|------|
| 本番 (読み取りのみ) | tailnet 上の `prod-node-1` に対する TLS/HTTP の読取 (`openssl s_client`、`curl`)、`make kubectl` による `get`・`get --raw` の読取。変更・SSH・Ansible・Terraform は行っていない |
| ローカル k3s | 本番と同じ `rancher/k3s:v1.36.3-k3s1` イメージを `docker run --privileged` で使い捨て起動し、`/etc/rancher/k3s` を bind mount して設定変更と再起動を再現した (k3d のノードイメージと同一。k3d クラスタ作成ではなく設定ファイルの差し替えが容易な素の起動を使った)。ホストの制約で Pod から ClusterIP へは届かないが、API サーバー・kubelet への直接アクセスは問題ない。検証後はコンテナ・一時ファイルをすべて削除 |
| OIDC 発行者のモック | GitHub の実トークンを手元に持ち出さないため、静的 JWKS を返す HTTPS の一時プロセスと自前署名 JWT で代替した。claim の構造と型は下記「GitHub OIDC トークン」で実測した実トークンに合わせている (数値 ID は文字列)。本番に入れるものではない |
| GitHub Actions | リポジトリに一時ブランチと `on: push` のワークフローを作成し、`id-token: write` で取得したトークンの claim のキー・型・形式・有効期間だけをログに出した (トークン本体・数値 ID の実値は出力せず、`::add-mask::` を併用)。tailnet 到達性の確認のため、同ワークフローで既存 CI と同じ `tag:ci` (OAuth) の一時ノードを tailnet に参加させ、資格情報なしの TLS/HTTP 読取だけを行った。確認後にブランチと run (ログ) を削除済み |

### 検証結果の一覧

| # | 項目 | 結果 |
|---|------|------|
| 1a | kubelet 10250 に手元 (tailnet) から到達 | 成功 |
| 1b | kubelet 10250 に CI ランナー (`tag:ci`) から到達 | 成功 (実測)。ACL の GET は検証不能 (下記) |
| 1c | kubelet serving 証明書の SAN と署名 CA | 成功。SAN に MagicDNS 名を含み、署名は `/cacerts` の server CA。ホスト名検証が通る |
| 1d | OIDC トークンで kubelet に認証 (webhook TokenReview) し `nodes/stats` のみで `/stats/summary` を取得、`nodes/proxy` 無しで exec 等ができない | 成功 (k3d) |
| 2a | `AuthenticationConfiguration` の API バージョン・指定方法 | 成功。`apiserver.config.k8s.io/v1` で通る (v1beta1 も通る)。`kube-apiserver-arg` の `authentication-config=<path>` で指定 |
| 2b | `anonymous.enabled: false` の要否 | 必須と確認 (設定なしでは匿名で `/version`・`/healthz` が 200) |
| 2c | 設定不正時の k3s の起動 | 失敗する (即時終了)。終了コードは 0 |
| 2d | ファイル変更時の自動再読込 | 有効だが制約あり (下記) |
| 2e | CEL (数値 claim の比較、ユーザー名マッピング、`split` 等、optional 参照、map リテラル) | 成功 |
| 2f | GitHub の issuer の JWKS を k3d から取得 | 成功 (実 issuer に対して JWKS 取得が成功することをメトリクスで確認)。発行者に到達できない状態でも起動し x509 は通る |
| 3a | CSR (`kubernetes.io/kube-apiserver-client`) が承認後に署名される | 成功。署名 CA は client CA |
| 3b | `cluster-signing-duration=168h` と `expirationSeconds` の挙動 | 成功 (未指定・超過は 168h、短い指定はそのまま、600 秒未満は作成時に拒否) |
| 3c | `O=system:masters` の拒否 | 成功 (作成時に Forbidden) |
| 3d | CN に ServiceAccount 名を入れた CSR | 通る。その SA と同一視される (設計の残存リスクの前提が実測で確認された) |
| 3e | 承認に必要な最小 RBAC・作成者本人による承認 | 成功 |
| 3f | 処理済み CSR の自動削除 (GC) | 成功 (承認後 1 時間経過後の最初の周期で削除) |
| 4 | `/cacerts` | 成功 (本番・k3d とも認証なしで server CA を返す) |
| 5 | GitHub OIDC トークンの claim・型・有効期間 | 実測済み (`environment` の存在側は未検証、下記) |
| 6 | 監査ログ | 成功 |
| 7 | client CA の forced rotation | 成功 (非 forced の cross-signed では旧証明書が無効にならないことも確認) |
| 8a | exec credential plugin による都度トークン再取得 | 成功 |
| 8b | bootstrap RBAC を Ansible が先行適用し ArgoCD が引き継ぐ (field manager の競合) | 一部成功 (kubectl で再現できる範囲。ArgoCD 本体は未検証) |
| 8c | exec・logs -f・port-forward が証明書認証で動く (Req 4.5) | 成功 |
| 8d | zitadel-bootstrap の移行方式 | kubectl の挙動のみ検証。ロール自体は検証不能 |
| 8e | gh の `return_run_details`、`gh run watch/download` | 検証不能 (後述) |
| 8f | Tailscale ACL の GET | 検証不能 (後述) |
| 8g | k3s 再起動を含む systemd の挙動 (`Restart=always`、`Type=notify`) | 検証不能 (docker 上で再現できない) |
| 8h | binding の付与・剥奪の即時反映 (Req 5.2、5.3、5.6) | 成功 (API サーバー。kubelet 直接アクセスは authz キャッシュで最大 5 分遅れる) |

### 1. kubelet 10250 の直接アクセス (D6)

- **手順 (本番、読取のみ)**: 手元から `openssl s_client -connect prod-node-1:10250` で証明書を取得。`/cacerts` で得た CA で `-verify_hostname prod-node-1` を指定して検証。認証なしの `GET /stats/summary`・`/healthz` が 401 になることだけを確認 (認証リクエストは送っていない)。`make kubectl get --raw /api/v1/nodes/prod-node-1/proxy/configz` で kubelet 設定を読取。
- **結果 (本番)**:
  - 到達可能。証明書の issuer は k3s の server CA (`k3s-server-ca@<epoch>`)。SAN は `prod-node-1` (MagicDNS 名)、`localhost`、ノードの IP 数件。`/cacerts` の CA で検証が通る (6443 の serving 証明書も同じ CA で検証できる)。
  - kubelet の設定は authentication = x509 + webhook (有効、cacheTTL 2m) + anonymous 無効、authorization = Webhook。
- **結果 (CI ランナー、`tag:ci` の一時ノード)**: `prod-node-1:6443/cacerts` が 200、6443 と 10250 の両方でホスト名検証が成功し、認証なしの `/healthz` は 401。ランナーから 10250 に届く。
- **結果 (k3d)**: kubelet の設定は本番と同じ (webhook authn、Webhook authz、anonymous 無効)。OIDC トークン (モック) で認証した `gha:infra-health-check` に `nodes/stats` の get だけを与えると:
  - 認証なし 401 / JWT のみ (RBAC なし) 403 / `nodes/stats` あり `GET /stats/summary` 200 (`.node.fs` に `usedBytes`・`capacityBytes` を含む。現行 `infra-health-check.sh` が使う項目)。
  - 同じ権限で kubelet の `/pods`・`/configz`・`/metrics`・`/healthz`・`/logs/`・`/containerLogs/…`・`/run/…` (POST)・`/exec/…`・`/portForward/…` は 403。拒否理由は `nodes/proxy` の create (`Forbidden (user=gha:infra-health-check, verb=create, resource=nodes, subresource(s)=[proxy])`)。
  - API サーバー経由の `/api/v1/nodes/<n>/proxy/stats/summary` は同じ権限では 403 (`nodes/proxy` の get が要る)。直接アクセスにする必要があるという設計の前提が確認できた。
  - kubelet の authz キャッシュ: `cacheAuthorizedTTL` 5 分、`cacheUnauthorizedTTL` 30 秒、authn の `cacheTTL` 2 分。binding を削除しても kubelet 直接アクセスの許可は最大 5 分残り、追加直後は最大 30 秒拒否される。
  - 補足: `kubectl auth can-i get nodes/stats` は `nodes` の name=`stats` として解釈され `no` を返す。サブリソースの確認は `--subresource=stats` を使う。
- **Tailscale ACL の GET (検証不能)**: 既存の OAuth client (Infisical prod) の scope は `devices:core:read`・`auth_keys` のみで、ACL の取得は 404 (`not found`) になる。ACL の中身は読めない。代わりに上記のとおり `tag:ci` の実ノードから到達できることを実測した。ACL は手動管理のため将来変えられる可能性は残る。
- **設計への影響**: D6 の前提は成立。設計から「成立しなければ実装を止めて報告する」条件を外し、ACL 変更を再検証トリガーに追加した。確認の恒常化は health-check の失敗検知に任せる (D14)。

### 2. structured authentication (k3s v1.36.3)

- **手順 (k3d)**: `AuthenticationConfiguration` を `/etc/rancher/k3s/authentication-config.yaml` に置き、`kube-apiserver-arg: authentication-config=…` を `config.yaml` に指定して起動。設計どおりのワークフロー許可リスト (CEL) を持つ設定を作り、28 通りのトークン (許可・各規則の違反・欠落) を提示して判定を確認。起動時の不正設定、起動中の設定変更を試験。
- **API バージョン・指定**: `apiserver.config.k8s.io/v1` が通る (v1beta1 も通る)。k3s は `authentication-config` 指定時に `Not setting kube-apiserver 'anonymous-auth' flag due to user-provided 'authentication-config' file.` を出し、`--anonymous-auth` を設定しない。
- **匿名認証**: `anonymous` を書かずに起動すると、匿名で `/version` と `/healthz` が 200、`/api/v1/namespaces` は 403 (`system:public-info-viewer` の範囲)。`anonymous: {enabled: false}` を書くと `/version`・`/healthz`・`/readyz` はすべて 401 になる。本番の現状 (匿名 401) を維持するには明示が必須。
- **設定が不正なときの起動**: 次の場合は kube-apiserver が起動せず k3s が約 2 秒で終了する (終了コードは 0)。いずれもエラー内容は標準出力・ログに出る。
  - CEL の構文エラー: `Error: invalid authentication configuration: jwt[0].claimValidationRules[2].expression: … compilation failed: …`
  - 未知のフィールド: `strict decoding error: unknown field "jwt[0].claimMappingsX"` (typo も致命になる)
  - ファイルがない: `failed to load authentication configuration from file …: no such file or directory`
  
  リポジトリの `k3s-server.service.j2` は `Restart=always`・`RestartSec=5s` のため、終了コード 0 でも再起動を繰り返す。systemd 上の `systemctl restart` の戻り値と待機時間は docker 上で再現できず検証不能。
- **自動再読込**:
  - 有効な変更 (許可ワークフローへのイベント追加) をアトミックな置換 (`mv`) で行うと、再起動なしで約 10 秒後に反映された (`apiserver_authentication_config_controller_automatic_reloads_total{status="success"}` が増加)。
  - 不正な変更 (CEL 構文エラー) をアトミックに置換すると、旧設定が維持され、`…automatic_reloads_total{status="failure"}` だけが増える (呼び出し側にはエラーが返らない)。研究ログの「不正なら旧設定を維持」は事実。
  - `anonymous` の値は再読込では変更できない (`anonymous: Forbidden: changed from initial configuration file` で拒否され旧設定が残る)。匿名設定の変更は再起動が必須。
  - 非アトミックな書込 (`>` によるトランケート後の書込) は途中の空ファイルを読んで一時的に `empty config data` で再読込失敗になる (旧設定は維持)。Ansible の `template` はアトミックな置換のため問題にならない。
  - 設計の「再起動で反映し `/readyz` で検知する」方針は妥当。再起動前に自動再読込が先に反映することもある (害はない)。
- **claim の CEL**:
  - `claimValidationRules` は `claims.<name>` の文字列比較・`startsWith`/`endsWith`・`split`・`replace`・map リテラル (`{'a.yml': ['x']}`)・`in`・`!` が 1.36 で使える。ユーザー名式 `"gha:" + claims.job_workflow_ref.split("@")[0].split("/")[4].replace(".yml", "")` は `gha:infra-health-check` を返し、グループは `system:authenticated` のみ。`userValidationRules` (`user.username.startsWith('gha:')`) も動く。
  - 存在しない claim を直接参照すると `no such key: <name>` の評価エラーになり、認証は拒否される (fail-closed)。`claims.?runner_environment.orValue('')` の optional 参照も動く。
  - 数値型の claim (JSON number) を文字列と比較すると偽になり拒否される (GitHub の実トークンは文字列なので正常系は通る)。
  - 28 通りのトークンで判定を確認: 許可 (infra-health-check の dispatch・schedule、intrusion-response の dispatch、dr-recovery + environment、kube-cert-issue の dispatch) は期待どおりのユーザー名。拒否: ref が main 以外・ref 欠落・pull_request/push イベント・owner ID/repository ID 不一致 (フォーク相当)・ID の欠落と数値型・許可リスト外ワークフロー・他リポジトリの同名ファイル・`@refs/heads/feat`・`workflows/sub/` 配下・許可外イベントの組み合わせ・dr-recovery の environment 欠落または別名・自己ホストランナー・`runner_environment` 欠落・audience 不一致・期限切れ・`nbf` 未来・別 issuer・`job_workflow_ref` 欠落。
  - 拒否理由は API サーバーのログに `validation expression '<式>' failed: <message>` として出る (`message` を付ければ規則名が特定できる)。ログにトークンは出ない。
  - `environment` を要求しないワークフローに `environment` claim が付いていても通る。
- **GitHub の issuer への JWKS 取得**: issuer を `https://token.actions.githubusercontent.com` にした設定で k3d を起動し、ダミーの署名のトークンを提示した。検証は署名不一致で拒否されたが、`apiserver_authentication_jwt_authenticator_jwks_fetch_last_timestamp_seconds{result="success"}` が記録され、JWKS 取得は成功した (k3d から GitHub に到達できる)。issuer に到達できない状態 (到達不能なアドレスに固定) で起動しても API サーバーは起動し、x509 の admin は通り、JWT は 401 になる (Req 11.3)。
- **設計への影響**: 設定スキーマ・CEL は設計どおり。`anonymous` は必須で再起動が必要、起動失敗時は即終了して再起動ループになる、が確定したため、AuthnConfigDistribution の復旧手順を具体化した。失敗の観測点として上記 2 つのメトリクスが使える。

### 3. CSR API

- **手順 (k3d)**: ワークフロー用ユーザー `gha:kube-cert-issue` (モックの JWT で認証) に設計の ClusterRole (CSR の create/get、`certificatesigningrequests/approval` の update、`signers` の approve) だけを与え、同ユーザーが CSR を作成・承認して証明書を取得。
- **結果**:
  - 承認後、数秒以内に `status.certificate` が入る (`Approved,Issued`)。issuer は client CA (`k3s-client-ca@<epoch>`)。KeyUsage = digital signature、EKU = client auth、CA:FALSE。`CN=github:<login>:<id>` (`:` 入り) の証明書は `kubectl auth whoami` でユーザー名がそのまま `github:<login>:<id>` になる。
  - 期限: `expirationSeconds` 未指定は 168h。1 年を指定しても 168h に切り詰められる。24h を指定すれば 24h。300 秒は `may not specify a duration less than 600 seconds` で作成時に拒否される。`notBefore` は発行時刻の 5 分前、`notAfter` は `notBefore` の 168h 後 (有効期間は発行時刻から 7 日より 5 分短い)。
  - `O=system:masters` の CSR は作成時に `use of kubernetes.io/kube-apiserver-client signer with system:masters group is not allowed` で Forbidden (k3s でも `CertificateSubjectRestriction` が有効。作成者が `gha:kube-cert-issue` でも拒否)。
  - `CN=system:serviceaccount:kube-system:foo` の CSR は作成・承認・署名でき、その証明書は `system:serviceaccount:kube-system:foo` として認証され、ServiceAccount を subject にした ClusterRoleBinding の権限を持つ。設計の残存リスク (任意の CN を発行できる) は実測で成立する。
  - 用途が `server auth` の CSR は作成・承認までは通り、署名の段階で `Approved,Failed` になる (作成時の拒否ではない)。
  - 作成者と承認者が同一 (`gha:kube-cert-issue`) でも承認できる。必要な権限は設計の 4 つだけで足りる。`delete`・`list` は与えておらず拒否される (発行ワークフローは `get` のポーリングで足りる)。
  - k3s の kubelet・コントローラの証明書は supervisor が発行し、起動直後のクラスタに CSR は存在しない (`cluster-signing-duration` の影響を受けない。設計の「k3d で確認」は実測で裏付けられた)。本番にも CSR は存在しない (`get csr` で No resources found)。
- **CSR の検証に使う openssl の注意点**: OpenSSL 3.6 の `openssl req -text` は属性・拡張がなくても `Attributes: (none)` と空の `Requested Extensions:` の見出しを出す。見出しの有無で「拡張要求あり」と判定すると、正常な CSR を誤って拒否する。`openssl asn1parse` で CertificationRequestInfo の属性 (`cont [ 0 ]`) の長さが 0 であることを見るか、拡張の中身 (見出しの次の行) の有無で判定する。
- **GC (処理済み CSR の自動削除)**: 成功。承認済み (`Approved,Issued` 4 件と `Approved,Failed` 1 件) の CSR は作成から約 69 分後、kube-controller-manager の起動から 2 回目の周期 (30 分間隔) で、5 件とも自動削除された (発行ワークフローに `delete` 権限は不要。1 時間経過後の最初の周期で消えるため、実際の消滅は承認から 1 時間〜1.5 時間後)。
- **設計への影響**: CSR 設計は成立。`notBefore` の 5 分バックデートを job summary の説明に反映。CsrValidator に openssl の出力に関する注意を追記。

### 4. `/cacerts`

- **手順**: 本番に対して認証なしで `GET https://prod-node-1:6443/cacerts` (手元と `tag:ci` の一時ノードの両方)。k3d で同じエンドポイントを取得し、`/var/lib/rancher/k3s/server/tls/server-ca.crt` と比較。
- **結果**: 認証なしで 200。証明書 1 枚 (server CA、有効期間 10 年、CN は `k3s-server-ca@<epoch>`)。k3d では `server-ca.crt` とバイト単位で同一。`authentication-config` で匿名を無効にした状態でも 200 (supervisor が提供するため)。取得した CA で、本番の 6443 と 10250 の serving 証明書 (`prod-node-1` 名) の検証が通る。
- **設計への影響**: D3 は成立。補足として、「取得した CA で同じエンドポイントの TLS 検証を通す」確認は取得内容の整合性の確認であり、経路上の攻撃者が CA と証明書の両方を差し替える場合は検出できない。認証の根拠は tailnet (MagicDNS 名・WireGuard による端末認証) と、手元での既存 CA との指紋差分検知にある。設計にその旨を明記した。

### 5. GitHub OIDC トークン

- **手順**: 一時ブランチへの push で起動したワークフロー (`id-token: write`) で、`audience=<任意文字列>` を付けてトークンを取得し、claim のキー・型・形式・`exp - iat` のみを出力した。実値は ID 類・トークンとも出していない。
- **結果**:
  - ヘッダは RS256、キー `alg`・`kid`・`typ`・`x5t`。
  - claim のキー: `actor`、`actor_id`、`aud`、`base_ref`、`check_run_id`、`event_name`、`exp`、`head_ref`、`iat`、`iss`、`job_workflow_ref`、`job_workflow_sha`、`jti`、`nbf`、`ref`、`ref_protected`、`ref_type`、`repository`、`repository_id`、`repository_owner`、`repository_owner_id`、`repository_visibility`、`run_attempt`、`run_id`、`run_number`、`runner_environment`、`sha`、`sub`、`workflow`、`workflow_ref`、`workflow_sha`。
  - 型: `repository_owner_id`・`repository_id`・`actor_id`・`run_id`・`run_attempt` はすべて JSON 文字列 (数字のみ)。設計の「文字列比較」で正しい。
  - `exp - iat` = 300 秒 (5 分)。`nbf` = `iat` - 300 秒。研究ログの「公式に明記なし・第三者情報で約 5 分」は実測で 5 分と確認。
  - `aud` は要求した `audience` と完全一致 (単一の文字列)。`iss` は `https://token.actions.githubusercontent.com`。同一ジョブ内で 2 回要求すると `jti` が異なる (毎回新規発行される)。
  - `runner_environment` = `github-hosted`、`repository_visibility` = `public`、`ref_type` = `branch`、`event_name` = `push` (この検証の起動イベント)。`ref` は `refs/heads/<branch>`。`job_workflow_ref` は `<owner>/<repo>/.github/workflows/<file>@refs/heads/<branch>`。`sub` は `repo:<owner>/<repo>:ref:refs/heads/<branch>`。
  - **`environment` は Environment を参照しないジョブでは claim ごと欠落** (`ABSENT`)。openid-configuration の `claims_supported` には `environment`・`environment_node_id` が含まれる。
- **未検証**: (1) `environment` claim が Environment 参照ジョブで付くこと (Environment を新規に作ると GitHub 側に副作用が残るため行わなかった。作成済みの `dr-recovery` は deployment branch が main のみで一時ブランチから参照できないため、同じ手順は使えない。DR の OIDC 移行のマージ後に、main の `dr-recovery` を生存確認ゲートで停止させる経路で確認する。design.md「environment claim の確認」)。(2) `event_name` の `schedule`・`workflow_dispatch` の値 (今回の起動イベントは push。公式ドキュメントの定義のみ)。
- **設計への影響**: 設計の claim 照合と型の前提が成立。ExecCredential の有効期限は JWT の `exp` (発行から 5 分) と一致させる。

### 6. 監査ログ

- **手順 (k3d)**: `kube-apiserver-arg` に `audit-policy-file`・`audit-log-path`・`audit-log-maxsize`・`audit-log-maxbackup`・`audit-log-maxage` を指定。ポリシーは Metadata レベルで、システムコンポーネント・ノード・kube-system の ServiceAccount・ServiceAccount の read・ヘルスエンドポイント・events・leases を `None` にした (design.md の「監査ポリシー」参照)。
- **結果**:
  - 各引数が効く。ログは root 0600 で出力され、1 要求 1 行の JSON。Metadata レベルで `user.username` に `gha:infra-health-check` や `system:admin` が記録される (403 の要求も記録される)。
  - 除外前は、k3s の再起動直後の約 1 分で 117 行が出た (うち 82 行が `system:k3s-supervisor`、他は `k3s-cloud-controller-manager` と kube-system の各コントローラ)。除外を `system:k3s-supervisor`・`k3s-cloud-controller-manager`・`system:serviceaccounts:kube-system` グループまで広げると、アイドル時は 60 秒間 0 行になった。
  - 容量: `maxsize=1`・`maxbackup=3` で約 37,000 要求を投入したところ、ファイルは現行 1 本 + バックアップ 3 本 (各約 1MB) で頭打ちになり合計約 3.7MB (1 行約 0.3KB)。上限は (maxbackup + 1) × maxsize。
  - ポリシーファイルは起動時にのみ読まれる (変更は再起動が必要)。
- **未検証**: 本番での実際の書込要求の量 (監査ポリシー適用後でないと計測できない。上限があるためディスクへの影響は上記の式で抑えられる)。
- **設計への影響**: D5 のポリシーを具体化し、ロール変数の上限式を明記した。

### 7. client CA の forced rotation (D7)

- **手順 (k3d、使い捨てクラスタ 2 つ)**: 1 つ目 (フルの設計構成) で、共有 admin 相当の旧クライアント証明書 (`/var/lib/rancher/k3s/server/tls/client-admin.crt`)、人の証明書 (CSR で発行)、旧 `k3s.yaml` のコピーを用意。新しい client CA (EC P-256 の自己署名ルート、`CA:TRUE`) だけを `<dir>/tls/client-ca.{crt,key}` に置き、`k3s certificate rotate-ca --path <dir> --force` を実行して k3s を再起動した。2 つ目で cross-signed (新 CA を旧 CA で署名し、新 CA + 旧ルートのバンドルを置く) を forced なしで実行して比較した。
- **結果 (forced、client CA のみ)**:
  - 旧 admin 証明書・旧 `k3s.yaml` のコピー・人の旧証明書はすべて 401。
  - 再起動後、`/etc/rancher/k3s/k3s.yaml` は新しい client CA 署名の admin 証明書で再生成され、ノード上のローカル admin はそのまま使える。kubelet・controller・scheduler などの内部クライアント証明書も新 CA で自動再発行され、ノードは Ready、kube-system の Pod (coredns) は Running、OIDC 認証も継続。
  - server CA は変わらない (`/cacerts` が回転前と同一、回転前の server CA で serving 証明書が検証できる)。人の kubeconfig の CA 部分は影響を受けない。
  - 再起動を挟む手順の所要時間は、コマンド数秒 + k3s 再起動数秒 (k3d)。
  - ディレクトリの指定は `<path>/tls/` の下にファイルを置く形式が必要。`<path>` 直下に置くと「ファイルがない」警告だけを出し、変更のないまま成功 (`certificates saved to datastore`) に見えるため、実施後に client CA の指紋で反映を確認する。
  - 置かなかったファイル (server CA 等) は「変更なし」として扱われる (`failed to stat …` の警告は無害)。
  - forced なしで新 CA が単独の証明書だと `new CA bundle contains only a single certificate but should include root or intermediate CA certificates` で検証に失敗する (forced で無視できる)。
- **結果 (cross-signed、forced なし)**: 新しい中間 CA + 旧ルートのバンドルは受理され、再起動後も **旧 admin 証明書は有効のまま** (`kubectl get ns` が通る)。D7 の目的 (旧共有 admin 証明書の無効化) には forced が必要であることが確認できた。
- **ワークロード**: k3d では Pod が rotation 後に `Unknown` になったが、これは docker の再起動が containerd 配下のコンテナも停止するためで rotation の影響ではない (rotation なしで同じ再起動をしても同じく `Unknown` になることを確認済み)。本番の `k3s.service` は `KillMode=process` でコンテナを残すため、本番相当の挙動は検証不能。ServiceAccount トークンで動く coredns は rotation 後に Running で、リポジトリの gitops に client 証明書を使う kubeconfig はない (grep で確認)。
- **設計への影響**: D7 の手順を具体化 (design.md の Migration Strategy)。Pod の継続は本番でも P7 の確認項目に残す。

### 8. その他の推測

- **exec credential plugin**: kubeconfig の `users[].user.exec` (`client.authentication.k8s.io/v1`、`interactiveMode: Never`) でトークンと `expirationTimestamp` を返すプラグインを使うと、`kubectl` の起動ごとにプラグインが 1 回呼ばれ (3 回の起動で 3 回)、1 プロセス内ではキャッシュされる。プラグインが非 0 で終了すると `getting credentials: exec: executable … failed` で kubectl が失敗する。設計の KubeOidcHelper の契約は成立。
- **bootstrap RBAC の field manager** (kubectl での再現):
  - Ansible 相当の `kubectl apply --server-side --field-manager=ansible-bootstrap` で作成した ClusterRoleBinding に、ArgoCD 相当の既定 (クライアントサイド) apply を同じ内容で当てると、競合なしで引き継げる (managedFields は `ansible-bootstrap` の Apply と `argocd-controller` の Update。`last-applied-configuration` 欠落の警告が出るが自動で補われる)。ArgoCD の server-side apply (`--force-conflicts` 付き) でも、内容が違っても成功し `Apply` 同士の共有所有になる。
  - 内容が一致していれば、Ansible の再実行は差分なし (`kubectl diff --server-side --force-conflicts` が 0)。
  - Git で内容が変わり ArgoCD が適用済みの状態で、古い内容を `--force-conflicts` なしで Ansible が server-side apply すると、フィールドの競合で失敗する。`--force-conflicts` を付けると成功するが、ArgoCD が適用した変更を古い内容で上書きする (ArgoCD の selfHeal で戻る)。PR #288 の `apply_manifests.yml` は `kubectl diff`/`apply --server-side --force-conflicts` を使っているため競合では失敗せず、Ansible は常に最新の main の同一ファイルを使う前提で運用する。
  - ArgoCD 本体 (tracking annotation、prune の挙動) は k3d で再現できず検証不能。
- **binding の付与・剥奪 (Req 5)**: CSR で発行した証明書 (`github:<login>:<id>`) は binding がないと認証されても `Forbidden`、`view` の ClusterRoleBinding を作ると直後に `get ns` が通り、binding を削除すると直後に再び `Forbidden` になる (API サーバー側に認可キャッシュの遅延はない)。ログイン名が異なる (同じ ID でも CN が違う) ユーザー名には binding が一致しない。
- **人の証明書での通常操作 (Req 4.5)**: CSR で発行した証明書 (cluster-admin の binding) で、`kubectl exec`・`logs -f`・`port-forward` が動くことを確認した (k3d)。
- **zitadel-bootstrap**: 現在は env `KUBECONFIG` の中身をファイルに書き出して `kubectl exec` に使う実装。kubectl 側の挙動 (env `KUBECONFIG` はパス、証明書認証での `exec`) は上記のとおり確認したが、ロール自体の移行は本番の Zitadel Pod が必要で検証不能。
- **gh の dispatch API (`return_run_details`)・`gh run watch`・artifact 取得 (検証不能)**: 検証には `workflow_dispatch` を持つワークフローが default ブランチに存在する必要がある (一時ブランチのワークフローは dispatch できない)。実装の P4 で `kube-cert-issue.yml` をマージした後に確認する。ローカルの gh は 2.102.0 で `gh api user` が数値の `id` と `login` を返すことは確認した。
- **GitHub リポジトリの設定 (読取)**: `main` のブランチ保護は PR レビュー 1 件必須・古いレビューの却下あり・**管理者への強制は無効** (`enforce_admins: false`)。設計の残存リスク (管理者 bypass でのマージ) の前提は実設定で成立している。Environment `dr-recovery` はユーザーが `gh api` で作成済み。`gh api repos/<repo>/environments/dr-recovery` の応答で、`protection_rules[]` に `type: required_reviewers` (reviewers は `type: Team` の `infra`、`prevent_self_review: false`) と `type: branch_policy` があり、`can_admins_bypass: false`、`deployment_branch_policy` は `custom_branch_policies: true`・`protected_branches: false`、`.../deployment-branch-policies` の `branch_policies` は `main` (`type: branch`) の 1 件であることを確認した (読取のみ)。EnvironmentGuard はこのフィールド名で検査する。`workflow_dispatch` を起動できるのが write 権限保持者に限られること、re-run で入力が変わらないこと、artifact の保持期間指定は、別アカウントでの試験が要るかドキュメントの定義のみで、実機では未検証。
- **systemd 上の再起動挙動 (検証不能)**: `Type=notify`・`TimeoutStartSec=0`・`Restart=always` のユニットで、認証設定が不正なときの `systemctl restart` (Ansible の handler) の戻り値と待機時間は docker 上で再現できない。k3s は不正設定で即時終了する (2 の結果) ため、`systemctl restart` は失敗で返るか、再起動ループになると想定するが未確認。検証には本番相当のノード (KVM の DR テスト環境) が必要。

## ロール描画物の使い捨て k3s 検証 (tasks 3.5・3.6、2026-10-04)

`scripts/verify-k3s-authn-disposable.sh` (補助 `.py`) が、k3s-server ロールのテンプレートを描画した認証設定・監査ポリシー・`config.yaml` を、本番と同じ版 (`rancher/k3s:v1.36.3-k3s1`) の使い捨てコンテナに入れて検証する。再実行できる。結果は 22 項目すべて pass、テンプレートの修正は不要だった。

| 区分 | 方法 |
|------|------|
| 描画 | ansible でロールの 3 テンプレートを描画。テスト用に差し替えたのは認証設定の issuer URL と `certificateAuthority` だけで、照合規則 (claimValidationRules・claimMappings・userValidationRules) は描画結果のまま。監査ログのローテーション上限だけ `-e` で maxsize 1MB・maxbackup 2 に上書き (総量の頭打ちを短時間で確認するため) |
| 発行者 | 自己署名 CA と自前の鍵で discovery と JWKS を返す HTTPS の一時プロセス (docker ネットワークのゲートウェイ IP で待受)。トークンは同じ鍵で署名し、数値 ID は `defaults/main.yml` から読む |
| 判定 | `POST /apis/authentication.k8s.io/v1/selfsubjectreviews` の応答で、ユーザー名 (許可) または 401 (拒否) を自動判定 |
| 起動確認 | `tasks/main.yml` と同じ判定 (ローカル admin の `/readyz` が期限内に ok、匿名 `/version` が 401) をコンテナに対して実行。systemd の再起動は `--restart always` で模した (systemd 上の挙動は 6.7) |

| 項目 | 結果 |
|------|------|
| 起動後、匿名の `/healthz`・`/readyz`・`/version`・`/api/v1/namespaces` が 401、ローカル admin (x509) が通る | pass |
| 許可 6 ケース (4 ワークフローの許可イベント全組み合わせ、環境不要のワークフローに environment claim が付く場合) が `gha:<ファイル名>` で通る | pass |
| 違反 29 ケースがすべて 401 (他組織・他リポジトリ・フォーク、ref が feature・タグ・欠落、許可リスト外、外部リポジトリの同名 reusable workflow、`job_workflow_ref` が feature ブランチ、サブディレクトリ、pull_request・pull_request_target・push、許可外イベントの組み合わせ、self-hosted・`runner_environment` 欠落、dr-recovery の environment なし・不一致・空、audience のみ一致、claim 欠落 4 種、audience 不一致、期限切れ、nbf 未来、issuer 不一致、署名鍵違い) | pass |
| 発行者に到達できない状態でも起動し、x509 が通り、JWT は 401 | pass |
| CEL の構文エラーで k3s が終了 (`invalid authentication configuration … compilation failed` が出る) し、ロールの起動確認が失敗として検知する | pass |
| 匿名無効が外れた設定 (匿名 `/version` が 200) を、起動確認の 401 判定が失敗として検知する | pass |
| CSR (`expirationSeconds` 1 年・未指定) が client CA で署名され、有効期間が 168h (604800 秒) | pass |
| organization `system:masters` の CSR が作成時に拒否される | pass |
| 監査ログに `gha:*` (create・selfsubjectreviews)、`github:<name>:<id>` 形式の x509 ユーザー (list・namespaces)、ローカル admin (list・nodes) が記録される | pass |
| システムコンポーネント・ノード・kube-system の ServiceAccount・events・leases が記録されない | pass |
| ローテーション上限: 負荷後も世代数 3・総量 約 2.3MB で、上限 (maxbackup+1) × maxsize = 3MB を超えない | pass |

- イメージの `k3s kubectl` はコンテナ内で `unknown command "kubectl"` になるため、検証スクリプトは同じバイナリへのリンク `kubectl` を使う。ロール本体 (`k3s kubectl get --raw=/readyz`) は本番ホストへ入れた k3s バイナリで動く前提で、docker 上では再現できない。この差分は 3.7 の本番適用で `/readyz` の確認タスクが通ることで確認する。
- 匿名の要求は監査ポリシーの除外対象ではなく、`/healthz*` などの nonResourceURLs 以外は `system:anonymous` として記録される (401 になる `/api/v1/namespaces` 等)。

## ユーザー決定の記録 (実機検証後、2026-10-04)

| 項目 | 決定 | 根拠となった検証結果 |
|------|------|---------------------|
| 認証設定不正時の復旧 (D13) | `block`/`rescue` で適用後の確認失敗時に旧設定へ自動ロールバック。確認は `/readyz` (ローカル admin) と匿名 `/version` = 401 の両方で、終了コード・`systemctl restart` の戻り値は使わない | k3s は不正設定で約 2 秒で exit 0 し、`Restart=always` で再起動を繰り返す (「2」)。匿名 401 は設定が読み込まれた証明になる |
| Tailscale ACL (D14) | 読取 scope は追加せず health-check の失敗検知に任せる | 既存 OAuth client では ACL を取得できず、`tag:ci` の実ノードから到達を実測した (「1」) |
| 監査ポリシー (D15) | kube-system の ServiceAccount と全 ServiceAccount の read を除外 | 除外前は再起動直後の約 1 分で 117 行、除外後はアイドル 0 行、SA の write は記録される (「6」) |
| 未検証事項 (D16) | EnvironmentGuard の API 応答は `dr-recovery` 作成後に確認済み。`environment` claim は DR の OIDC 移行のマージ後に生存確認ゲートで停止する経路、gh の dispatch は実装マージ後、systemd・rotation は DR テスト環境/P7 で確認 | 「5」「8」の未検証項目 |
| `dr-recovery` の承認者 (D9) | team `infra` を承認者とし、起動者本人の承認を認める。管理者 bypass 無効・main 限定。team のメンバーは手動で追加する | 別メンバーの承認を求めない方針 (D1) を DR にも適用 |
| rotation の戻し方 | rotation で k3s が復帰しない場合は退避した旧 client CA に戻す (API が応答すれば forced rotate-ca、起動しなければ etcd スナップショットからの復元)。SSH 例外 (D17) の範囲内で行い、本番前に DR テスト環境で試す | forced rotation は本番の単一ノードで k3s の再起動を伴う (「7」) |
| client CA rotation (D17) | 時期は任意。作業中に限り SSH でのローカル admin 使用を許可 (平常時は不可) | forced rotation で旧 `k3s.yaml` 以外の off-node 資格情報がすべて無効になり、復旧にノード上のローカル admin が要り得る (「7」) |

## Risks & Mitigations
- 発行ワークフロー侵害で cluster-admin 相当の証明書 — claim 照合 (main・ワークフロー・イベント)、ワークフロー内の CSR 検証、期限上限 7 日、第三者アクション不使用、入力の env 渡し、main ブランチ保護。別メンバー承認はユーザー決定により課さないため、main への悪意ある変更が通った場合のリスクは残る (残存リスクとして明記)。
- 認証設定の不正で kube-apiserver が起動しない (単一ノード) — 再起動後の `/readyz` 確認で即失敗、Ansible の SSH 経路でテンプレートを戻して再実行。事前に k3d で設定を起動確認する。
- 匿名認証の意図しない有効化 — `anonymous.enabled: false` を設定し、移行後に匿名要求が 401 になることを検証項目にする。
- GitHub 障害で CI・DR・新規発行が止まる — 要件上許容 (Req 11)。発行済み証明書と、ノード上のローカル admin (SSH) は使える。
- 旧共有 admin 証明書が最長 365 日有効 — 移行完了後に client CA の forced rotation で無効化 (D7)。事前に k3d で手順を検証する。
- 10250 直接アクセスの経路と kubelet 証明書の SAN — 実機検証で成立を確認済み (D6)。残るのは Tailscale ACL の将来変更で、health-check の失敗検知と再検証トリガーで扱う。

## References
- [Authenticating — Authentication configuration from a file](https://kubernetes.io/docs/reference/access-authn-authz/authentication/#using-authentication-configuration) — AuthenticationConfiguration の CEL・匿名設定・再読込
- [KEP-3331 Structured Authentication Configuration](https://github.com/kubernetes/enhancements/blob/master/keps/sig-auth/3331-structured-authentication-configuration/README.md) — 再読込時の不正設定・issuer オフライン時の挙動
- [Certificates and Certificate Signing Requests](https://kubernetes.io/docs/reference/access-authn-authz/certificate-signing-requests/) — kube-apiserver-client signer、承認権限、GC
- [Admission Controllers — CertificateSubjectRestriction](https://kubernetes.io/docs/reference/access-authn-authz/admission-controllers/#certificatesubjectrestriction) — system:masters 拒否
- [Common Expression Language in Kubernetes](https://kubernetes.io/docs/reference/using-api/cel/) — 利用可能な CEL ライブラリ (x509 なし)
- [Kubelet authentication/authorization](https://kubernetes.io/docs/reference/access-authn-authz/kubelet-authn-authz/) — `/stats/*` → `nodes/stats`、fine-grained authz
- [Kubernetes v1.36: Fine-Grained Kubelet API Authorization Graduates to GA](https://kubernetes.io/blog/2026/04/24/kubernetes-v1-36-fine-grained-kubelet-authorization-ga) — nodes/proxy GET の WebSocket exec 問題
- [GitHub OIDC reference](https://docs.github.com/en/actions/reference/security/oidc) — claim 一覧
- [Deployments and environments](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments) — required reviewers・自己承認禁止・管理者バイパス
- [Workflow dispatch API now returns run IDs](https://github.blog/changelog/2026-02-19-workflow-dispatch-api-now-returns-run-ids) — `return_run_details`、gh v2.87.0
- [K3s certificate CLI](https://docs.k3s.io/cli/certificate) — rotate / rotate-ca
- [k3s server.go (v1.36.3+k3s1)](https://github.com/k3s-io/k3s/blob/v1.36.3%2Bk3s1/pkg/daemons/control/server.go) — signer 設定・anonymous-auth の扱い
- [Spacelift: GitHub OIDC token expires after 5 minutes](https://support.spacelift.io/articles/8847614416-why-does-my-github-oidc-token-expire-after-5-minutes-when-using-spacelift-api) — トークン寿命 (第三者情報)
