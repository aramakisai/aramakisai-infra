# Gap Analysis: kube-github-auth

## 1. 分析サマリ

- **共有 kubeconfig の消費者は 9 系統ある**。人 (`make kubectl`)、infra-health-check、intrusion-response (forensics/isolate の 2 ジョブ)、k3s-upgrade (Play 5 の差分判定でのみ参照)、DR (`recovery.sh`)、k3s-bootstrap Play 5 (書込)、zitadel-bootstrap (読取)、zitadel-cutover (`zitadel_bootstrap_kubeconfig` を共有)、ドキュメント類。いずれも「Infisical の `KUBECONFIG` を中身ごと環境変数で受け取りファイルに書き出す」同じ実装を各所に重複して持っている。共通の取得ヘルパーはないため、置き換えは消費者ごとの個別改修になる。
- **kube-apiserver の認証に関わる仕組みはまだ何もない**。`--authentication-config`、GitHub OIDC (`id-token: write`)、CSR を使う処理、人やワークフロー向けの RBAC (ClusterRole/Binding) は、どれもリポジトリに存在しない。既存の RBAC は ServiceAccount 向けの namespace Role (vaultwarden-rbac-sync、mailserver backup healthcheck) だけ。したがって新規に作る部分が大半を占め、既存資産の拡張だけでは要件を満たせない。
- **k3s ブートストラップは拡張点が明確**。`k3s-server` ロールは「`config.yaml` をインストール前に template 配置 → 変更があれば notify で 1 回再起動」という形になっており、認証設定ファイルの配布と `kube-apiserver-arg` の追加はこの流れにそのまま乗る (Req 6.1〜6.3)。本番は prod-node-1 の単一ノード構成 (prod-node-2/3 は削除済みで、inventory の `k3s_server_worker` と terraform の定義だけが残っている)。配布と再起動は prod-node-1 の 1 台に対して行い、再起動中は kube-apiserver が一時的に停止する。
- **DR 直後の RBAC は鶏と卵の関係になる**。RBAC を gitops (ArgoCD) で付与する設計では、新クラスタで ArgoCD が RBAC を同期するまで、DR ワークフローは認証には成功しても認可されない。一方 `recovery.sh` は ArgoCD の Healthy 待ちそのものを kubectl で行っている。DR ワークフロー用の binding だけは bootstrap で直接適用する (`infisical-auth` と同様の bootstrap 例外を設ける) か、sync-wave で最優先にするか、設計で決める必要がある。
- **最大のセキュリティ論点は証明書発行ワークフロー**。`kubernetes.io/kube-apiserver-client` signer は subject を制限しない (`O=system:masters` も署名される)。そのため承認権限を持つワークフローが侵害されると、cluster-admin 相当の証明書を作れる。この権限に対する安全策は「main へ入るコードの審査」だけになる。main の保護は有効 (PR レビュー必須) だが管理者は bypass できる。Environment は 1 つも作成されていない。

## 2. 現状調査

### 2.1 共有 kubeconfig の消費者

| 消費者 | ファイル | 現状の取得方法 | kube に対する操作 | 移行時の注意 |
|---|---|---|---|---|
| 人 | `Makefile` (`kubectl` ターゲット) | `infisical run` で `$KUBECONFIG` (中身) を `/tmp/kubeconfig-aramakisai` に書出 | 任意 (cluster-admin) | 新しい入口は手元のコンテキストを使う (Req 4.6)。`make kubectl ARGS=...` は CLAUDE.md・steering・runbook で多数参照されているため、入口の名前を残すかどうかがドキュメント改修量に効く |
| infra-health-check | `.github/workflows/infra-health-check.yml`, `.github/scripts/infra-health-check.sh` | Infisical → `$KUBECONFIG` | `get --raw /api/v1/nodes/<node>/proxy/stats/summary`、`get clusters.postgresql.cnpg.io -A` | `nodes/proxy` の get が必要。下の「リスク R5」参照 |
| intrusion-response | `.github/workflows/intrusion-response.yml` | Infisical REST API で `KUBECONFIG` と `TF_VAR_tailscale_api_key` を取得。Tailscale API キーで ephemeral・タグなしの auth key を作って tailnet に参加し、`/etc/hosts` に Tailscale IP を追記 | `logs`、`get events/networkpolicy/pods`、`apply` NetworkPolicy | 2 ジョブとも workflow レベルの `permissions` に `id-token: write` がない。`kubectl apply` には get/create/patch が必要で、Req 2.4 の「作成のみ」とずれる。`kubectl logs -n <ns> --all-containers` は Pod 名もセレクタも指定しておらず、既存実装の時点で失敗している可能性がある (Req 8.2 の「従来どおり」の基準に影響) |
| k3s-upgrade | `.github/workflows/k3s-upgrade.yml` | ランナー自身は kubectl を使わない (インストールだけしている)。playbook Play 5 が `$KUBECONFIG` と取得値を比較する | ノード上の `/etc/rancher/k3s/k3s.yaml` (ローカル admin) で kubectl を実行 | Play 5 を削除すれば kube 資格情報は不要になる。Req 8.3 の「アップグレード前後の状態確認」を runner 側で新たに行うかどうかで、OIDC が必要かが決まる |
| DR | `.github/workflows/dr-recovery.yml`, `.github/scripts/recovery.sh` | main は `repository_dispatch`。PR #287 で `workflow_dispatch` + `environment: dr-recovery` + `if: github.ref == 'refs/heads/main'` に変わる。bootstrap 後に Infisical から `KUBECONFIG` を取り直す (`refresh_kubeconfig`) | get (nodes/applications/clusters/pods/secret)、`create secret` + `apply` (argocd ns)、`annotate externalsecret -A`、`apply -f` (mailserver 関連)。旧クラスタへの `get nodes` (生存確認ゲート) と `delete jobs` もある | 実行時間は最大 165 分と長く、OIDC トークン (後述: 短命) を 1 回取得するだけでは足りない。生存確認ゲートの `kubectl=alive` は旧クラスタへの認証も前提にしている |
| k3s-bootstrap (書込) | `ansible/playbooks/k3s-bootstrap.yml` Play 5 (PR #288 では Play 6 へ移動し `OPS_INFISICAL_*` を使用) | `/etc/rancher/k3s/k3s.yaml` を取得して Infisical に登録。手元の `kubeconfig` をスタブで上書き | — | 削除対象 (Req 8.5, 10.3)。Play 0〜4/6 の kubectl はノード上のローカル admin を使うため影響しない |
| zitadel-bootstrap | `ansible/roles/zitadel-bootstrap/{defaults,tasks}/main.yml`, `_api_call.yml` | env `KUBECONFIG` を **中身として** ファイルに書き出す。`ZITADEL_POC_KUBECONFIG` で上書きできる | `kubectl exec` (zitadel/login コンテナ) ほか | 標準の `KUBECONFIG` (パス) と意味が衝突する。移行後は env にパスが入るため、パス文字列がそのまま kubeconfig の中身として書き出される壊れ方をする。人の手元コンテキストを使う形への改修が必要 |
| zitadel-cutover | `ansible/roles/zitadel-cutover/tasks/*.yml` | `zitadel_bootstrap_kubeconfig` を共有 | prod に対しては kubectl/argocd を実行しない設計 (k3d 検証のみ) | 変数の定義元 (zitadel-bootstrap) を直せば追従する |
| ドキュメント | `CLAUDE.md`, `README.md`, `.kiro/steering/{tech,dr,vaultwarden-rbac}.md`, `docs/dr-runbook.md`, `docs/zitadel-*-runbook.md`, 手元用スタブ `kubeconfig` (Git 管理下) | — | — | Req 8.7・14 の対象。`.gitignore` の `ansible/kubeconfig` と `.gitleaks.toml` の `kubeconfig$` も整理の候補 |

DR テスト用スクリプト (`dr-k3d-*.sh`、`dr-local-test.sh`、`dr-kvm-create.sh`) は k3d や KVM のローカル kubeconfig を使っており、共有 kubeconfig には依存しない。ただし `recovery.sh` の `KUBECONFIG_FILE` 前提を変える場合は追従が要る。

### 2.2 ネットワーク経路と tailnet 参加

- 6443 は Hetzner firewall で公開されておらず (`terraform/firewall.tf`)、tailnet 経由でしか到達できない。
- `tls-san` は `ansible_host` (MagicDNS の短縮名) と private IP。クライアントはこの名前で接続しないと TLS 検証に失敗する。
- infra-health-check・k3s-upgrade・dr-recovery は `tailscale/github-action@v3` + `TS_OAUTH_CLIENT_ID/SECRET` + `tags: tag:ci` で参加している。intrusion-response だけが API キーを使い、タグなしで参加している (未決事項 3)。
- Tailscale ACL (tagOwners や 6443 への到達許可) は Terraform 管理外。人の端末と `tag:ci` から 6443 へ到達できることは、現状の運用実績 (infra-health-check が kubectl を使えている) でしか裏付けがない。

### 2.3 RBAC と ArgoCD の構成

- `gitops/root.yaml` (App of Apps、`gitops/apps/` を再帰監視) → `apps/prod/*.yaml` → `manifests/prod/<svc>/`。
- クラスタスコープの RBAC を置く Application はない。置き場の候補は、新規 Application (例: `cluster-rbac`)、既存の `namespace-config` (wave -1、destination は prod ns) への同居、`argocd-config` への同居。steering には「Application を不用意に増やさない」という禁止事項がある。一方 RBAC は sync-wave と prune の挙動 (binding 削除が即時剥奪につながる。Req 5.3) を独立に制御したい資源なので、設計判断が要る。
- 既存の RBAC マニフェストは日本語コメントで用途と最小権限の根拠を書く慣習 (`vaultwarden-rbac-sync/rbac.yaml`)。

### 2.4 進行中 PR との関係

| PR | 本仕様に関わる差分 | 必要な調整 (Req 10) |
|---|---|---|
| #288 (`fix/k3s-bootstrap-idempotency`) | Play 6 (旧 Play 5) の kubeconfig 登録が `OPS_INFISICAL_*` 前提。`infisical-auth` を `ESO_INFISICAL_*` から作成。k3s-upgrade.yml を `OPS_INFISICAL_*` に変更。k3s-server ロールに etcd 健全性待ちとノード Ready 待ちを追加。`.infisical.json` をコミット | Play 6 を削除。`infisical-auth` の入力を `INFISICAL_CLIENT_*` に戻す。k3s-upgrade.yml を `INFISICAL_CLIENT_*` に戻す。CLAUDE.md への追記 (OPS_* の記述) を削除。**ロールの再起動・待機の改善は、認証設定の配布 (Req 6.3) が前提にできる資産** |
| #287 (`fix/dr-manual-approval`) | dr-recovery.yml の `OPS_INFISICAL_*`、`refresh_kubeconfig`、`ESO_INFISICAL_*` による `infisical-auth` 修復。Environment `dr-recovery` (required reviewers の有無を検査) | Infisical 認証を `INFISICAL_CLIENT_*` に戻す。`refresh_kubeconfig` を OIDC ベースの kubeconfig 生成に置き換える。Environment の承認は Req 1.6 の `environment` claim 照合とそのまま整合する |

両 PR とも本仕様の決定に依存するため、マージ順序は「本仕様の方針確定 → 両 PR から OPS_* と ESO_* を外す → 本仕様の実装」か、「両 PR を INFISICAL_CLIENT_* に戻してマージし、Play 6 / refresh_kubeconfig は本仕様の移行完了まで現行 (書込失敗時は既存値一致でスキップ) のまま残す」のどちらかになる。後者では Req 9.1 (接続できない期間を作らない) を満たしやすい。ただしクラスタ再作成時は Infisical へ書き込めないため、旧方式の DR は事実上壊れたままになる点に注意。

## 3. 外部技術の調査結果

| 項目 | 結果 | 確度 |
|---|---|---|
| Structured Authentication Config | `apiserver.config.k8s.io/v1` `AuthenticationConfiguration`。v1.34 で GA なので、k3s v1.36 で使える。`--authentication-config` は `--oidc-*` フラグと排他。issuer URL は authenticator ごとに一意 | 高 (KEP-3331・公式ドキュメント) |
| ファイルの再読込 | API server がファイルを監視し、約 1 分ごとにハッシュを比較して再読込する。再読込時に設定が不正なら旧設定を維持する (`apiserver_authentication_config_controller_automatic_reload_*` メトリクス)。**起動時に不正な場合の挙動 (起動失敗かどうか) は k3s 上で実測が必要** | 中 |
| issuer 到達不能 | 起動時に issuer がオフラインでも API server は起動できる (自己ホスト型 IdP のための仕様)。JWT 認証だけが失敗し、x509 認証は影響を受けない (Req 11.3 と整合) | 中〜高 |
| CEL | `claimValidationRules[].expression` が false を返すと 401。`claimMappings.username.expression` の結果には暗黙の prefix が付かない (`system:` を避け、人の `github:` と衝突しない prefix を式の中に明示する必要がある)。文字列関数 (split など) が使える範囲は v1.36 の CEL 環境で確認する | 中 |
| GitHub OIDC claim | `repository_id`、`repository_owner_id`、`job_workflow_ref`、`workflow_ref`、`event_name`、`environment` (job が environment を参照した場合のみ)、`actor`、`actor_id`、`ref`。`aud` の既定値はオーナーの URL で、要求側が任意に指定できる (Req 1.7)。environment を使うと `sub` が `repo:...:environment:<name>` に変わる。本リポジトリは不変 ID 入りの新しい `sub` 形式の適用日より前に作成されているため、数値 ID は個別 claim で照合する | 高 (GitHub 公式) |
| OIDC トークンの有効期限 | 公式ドキュメントに明記はない。第三者情報では約 5 分。**長時間ジョブ (DR、Ansible 実行中の kubectl) は、kubeconfig の `exec` 資格情報プラグインで実行のたびに `ACTIONS_ID_TOKEN_REQUEST_URL` から取得し直す方式が必要** | 中 (実測要) |
| CSR API | `kubernetes.io/kube-apiserver-client` は **subject を制限しない (system:masters も可)**。`expirationSeconds` の最小は 600 秒で、実際の期間は signer 側の上限 (`--cluster-signing-duration`、既定 1 年) と CA の有効期限で切り詰められる。承認に必要な権限は `certificatesigningrequests` の create/get、`certificatesigningrequests/approval` の update、`signers` (resourceNames: `kubernetes.io/kube-apiserver-client`) の approve。承認・発行済みの CSR は 1 時間後に GC で自動削除される (Req 3.9 は既定挙動で満たせる。明示 delete も可) | 高 |
| k3s の CSR 署名 | k3s が kube-controller-manager に client-ca で kube-apiserver-client を署名させる設定 (`--cluster-signing-kube-apiserver-client-*`) と期間の上限は、公式ドキュメントで確認できなかった。**実機確認が必要** | 低 |
| k3s `/cacerts` | 6443 (supervisor) で認証なしに配布される。ノード参加時の TLS ブートストラップ用で、server CA (PEM) を返す。取得時の TLS 検証は行わない前提の仕組みで、真正性は token の `K10<sha256>` (CA の SHA256) で検証する | 中〜高 |
| k3s 証明書ローテーション | `k3s certificate rotate` はリーフ証明書だけを更新し、**CA が変わらないため旧証明書は有効期限まで有効**。admin 証明書は 365 日で、起動時に期限まで 120 日を切っていれば自動更新される (鍵は再利用)。旧証明書を無効にするには `k3s certificate rotate-ca` (cross-signed 既定 / `--force`) が要る。これは人の証明書・全コンポーネントにも影響する | 高 |
| 監査ログ | k3s は `kube-apiserver-arg` で `audit-log-path` / `audit-policy-file` などを渡す (CIS hardening ガイドと同じ方式)。ポリシーファイルの配布は認証設定と同じ k3s-server ロールの経路で行える | 中 |
| `nodes/proxy` | 2026 年に、`nodes/proxy` の GET だけで WebSocket 経由で kubelet `/exec` を叩けることが報告された (upstream は仕様どおりとして CVE なし)。v1.36 で fine-grained kubelet authz が GA になり、`nodes/stats` などの細粒度サブリソースがある | 中〜高 |

## 4. 要件と資産の対応表

凡例: **Missing** = 実装がない / **Unknown** = 要調査 / **Constraint** = 既存の制約 / **Reuse** = 既存資産を流用できる

| Req | 必要なもの | 既存資産 | ギャップ |
|---|---|---|---|
| 1 OIDC 直接検証 | AuthenticationConfiguration (issuer、audiences、claimValidationRules)、各ワークフローの `id-token: write` | なし | **Missing**: 設定ファイル・CEL・権限。**Unknown**: v1.36 の CEL 文字列関数、トークンの有効期限。**Constraint**: `environment` claim は job が environment を参照したときしか出ない (Req 1.6)。Environment はまだ 1 つもない |
| 2 ワークフロー単位の最小権限 | username の CEL マッピング (ワークフローファイル名から導く)、ClusterRole/Binding (またはワークフローごとの Role) | ServiceAccount 向け Role の書き方 (`vaultwarden-rbac-sync/rbac.yaml`) | **Missing**: 人とワークフロー向け RBAC とその置き場の Application。**Constraint**: intrusion-response は `apply` を使うため patch が要る (2.4 の「作成のみ」と矛盾。`kubectl create` への変更で解消できる)。health-check は `nodes/proxy` が要る (R5)。DR は secret の create、ExternalSecret の annotate、mailserver 関連の apply が要るため、範囲を絞り込みにくい (未決事項 6) |
| 3 証明書発行ワークフロー | workflow_dispatch (CSR を input で受ける)、openssl による subject・鍵用途・形式の検証、CSR 作成・承認・取得、結果 (証明書) の返却 | なし | **Missing**: 全部。**Constraint**: 公開リポジトリなので run ログや artifact は外部から見える (証明書と CSR は公開してよいが、トークンのマスクが必須)。input を `${{ }}` で shell に展開するとインジェクションになるので env 経由で渡す。`actor` と `triggering_actor` の違い (再実行時) の扱いを決める必要がある。**Unknown**: k3s の署名期間の上限 |
| 4 手元コンテキスト作成 | 1 コマンドで鍵生成 → `gh workflow run` → run の特定・待機 → 証明書取得 → `kubectl config set-*` まで行うスクリプト | `gh` CLI (運用で使用中)。`Makefile` の入口 | **Missing**: スクリプト本体。**Unknown**: `gh workflow run` で起動した run を確実に特定する方法 (dispatch API が run ID を返すかどうか、相関 ID の input 方式)。Windows や WSL の新入生環境での openssl・gh の前提 (`make setup` の案内に追記が要る) |
| 5 付与・剥奪 | `User github:<name>` への binding だけ | ArgoCD の prune・selfHeal | **Missing**: binding の置き場。**Constraint**: 剥奪の即時性は ArgoCD の同期間隔に依存する。GitHub ユーザー名は改名や再取得ができるため、名前だけを基準にすると別人が同じ名前を取った場合に権限を引き継ぐ (R4) |
| 6 認証設定の配布・DR 直後 | k3s-server ロールでのファイル配布、`kube-apiserver-arg`、handler 経由の再起動 | `config.yaml.j2` + `notify: Restart k3s` + `flush_handlers`、PR #288 の etcd・Ready 待ち | **Reuse**: 配布と再起動の流れ。**Missing**: 認証設定ファイルの template、起動失敗の検知 (`wait_for 6443` だけでは認証設定の不正を検知できない可能性がある)。**Constraint**: 本番は prod-node-1 の単一ノードで、反映時の再起動中は API が停止する (冗長な server はない。inventory に残る prod-node-2/3 は削除済みノードの定義)。DR 直後に RBAC が未同期という鶏と卵の問題 (R2) |
| 7 server CA の入手 | `/cacerts` 取得、リポジトリへの配置、Ansible の SSH 経由取得のいずれか | Ansible は SSH でノードに入れる (DR と k3s-upgrade は Ansible を実行している) | **Missing**: 取得手段。**Unknown**: `/cacerts` を tailnet 経由で TOFU 取得する場合の信頼性の評価 (未決事項 2) |
| 8 消費者の移行 | 2.1 の各消費者の改修 | — | **Missing**: 全消費者の改修。**Constraint**: zitadel-bootstrap の `KUBECONFIG` (中身) の意味が衝突する。DR は長時間ジョブ |
| 9 段階的移行 | 併用期間の手順 (新旧の認証が並存できる) | x509 と JWT は並存できる (authenticator は複数可) | **Missing**: 手順書。**Constraint**: 共有 kubeconfig の admin 証明書は `system:masters` で RBAC では剥奪できない。CA を変えない限り最長 365 日有効 (R6) |
| 10 PR との整合 | #288/#287 の OPS_* と ESO_* の除去 | 両 PR の差分 | **Missing**: 両 PR 側の修正。**Constraint**: 現在 `INFISICAL_CLIENT_*` は Viewer なので、DR 時の旧方式 Play 6 は書込 403 で落ちる |
| 11 GitHub 障害時 | 人の証明書は GitHub に依存しない | — | **Reuse**: 仕組み上自然に満たされる。**Missing**: 運用ドキュメント |
| 12 識別と監査 | username 設計、発行記録 (run ログ・job summary)、監査ログ (任意) | — | **Missing**: 監査ポリシー (未決事項 4)。**Unknown**: ログの保存先と容量 (単一ノードのディスク。過去に WAL 起因のディスクフル障害あり) |
| 13 制約 | 新規アプリなし・SSH 経路なし・Zitadel なし・identity 追加なし | — | **Constraint**: 本方式は kube-apiserver の組み込み機能、Actions、gh だけで成立し、制約内に収まる。数値 ID は公開情報として扱える |
| 14 ドキュメント同期 | 2.1 のドキュメント群 | — | **Missing**: 改修。`make kubectl` の参照箇所が多い |

## 5. 実装アプローチの選択肢

### Option A: 既存の入口と構造を拡張する

- `Makefile` の `kubectl` ターゲットを「手元の専用コンテキスト (例: `aramakisai-prod`) で kubectl を実行する」形に置き換え、証明書を取得する `make kube-login` を追加する。ドキュメント中の `make kubectl ARGS=...` はそのまま使える。
- 認証設定は `config.yaml.j2` の `kube-apiserver-arg` と、同じロールの新規 template 1 枚で配る。
- CI 側は各ワークフローに `id-token: write` と、OIDC トークンを kubeconfig に入れるステップを inline で追加する。
- RBAC は既存の `namespace-config` か `argocd-config` の Application に同居させる。
- ✅ 新規ファイルが少なく、ドキュメントの差分も小さい。Application を増やさない。
- ❌ kubeconfig を組み立てるロジックが 4〜5 ワークフローに重複する。現状の Infisical 取得ロジックの重複と同じ問題を再生産する。
- ❌ RBAC を既存 Application に同居させると、prune・sync-wave の意味が混ざる (`namespace-config` は prod ns 向けの LimitRange 用)。

### Option B: 認証まわりを独立した部品として新設する

- `.github/scripts/kube-oidc-kubeconfig.sh` (または composite action) を新設する。CA の取得と、`exec` 資格情報プラグインで都度トークンを取得する kubeconfig の生成を担い、全 CI と DR が共有する。
- `scripts/kube-login.sh` (人向け) と `.github/workflows/kube-cert-issue.yml` (発行) を新設する。
- RBAC は専用 Application (`cluster-rbac` など、wave -1) と `gitops/manifests/prod/cluster-rbac/` に集約し、ワークフロー用と人用のファイルを分ける。
- ✅ 責務が明確。長時間ジョブのトークン更新を 1 箇所で解決できる。剥奪の prune 挙動を独立に管理できる。
- ❌ Application が 1 つ増える (steering の禁止事項に照らして根拠の記録が要る)。ファイル数が増える。
- ❌ `make kubectl` を廃止または改名するとドキュメントの改修量が大きい。

### Option C: ハイブリッド (推奨候補)

- **拡張する部分**: k3s-server ロール (認証設定と、必要なら監査ポリシー)、`Makefile` の `kubectl` ターゲット (入口の名前を維持して中身を差し替える)、既存ワークフロー・`recovery.sh`・zitadel-bootstrap の kubeconfig 取得部分。
- **新設する部分**: OIDC kubeconfig 生成スクリプト (CI 共通)、証明書発行ワークフロー、人向け login スクリプト、RBAC マニフェスト一式。
- **段階的な進め方**:
  1. 認証設定の配布 (動作は変えず JWT authenticator を追加するだけ) と RBAC (binding のみ) を導入する。
  2. infra-health-check (読取のみで影響が最小) を移行して実地検証する。
  3. 証明書発行と人の入口を導入し、新旧を並存させる。
  4. intrusion-response・DR・k3s-upgrade・zitadel-bootstrap を移行する。
  5. Play 5/6 とスタブを削除し、Infisical から `KUBECONFIG` を削除する。
  6. ローテーションを判断する。
- **ロールバック**: 共有 kubeconfig を削除するまでは、各消費者を旧取得方式に戻すだけで済む。認証設定はファイルから JWT 部分を外せば再起動なしで反映される (再読込)。
- ✅ Req 9.1 / 9.5 の「どの時点でも接続できる」を満たしやすい。重複は CI 共通スクリプトに集約できる。
- ❌ 段階数が多く、PR #287/#288 とのマージ順序の調整が要る。

## 6. 工数とリスク

- **工数: L (1〜2 週間)**。新規部品 (発行ワークフロー、login スクリプト、OIDC kubeconfig 生成、RBAC 一式) と 9 系統の消費者移行、DR 経路の再検証、ドキュメント同期が重なる。実機検証 (k3s の再起動を伴う) は本番 1 クラスタでしか行えない。
- **リスク: High**。kube-apiserver の認証設定ミスは全経路の停止につながりうる。DR 直後の RBAC 鶏と卵、証明書発行ワークフローの権限の大きさ、OIDC トークン寿命と長時間ジョブ、k3s 固有の挙動 (CSR 署名の上限、起動時の不正設定の扱い) に実測が要る。

### 主要リスク

| ID | 内容 | 影響 | 緩和の方向 |
|---|---|---|---|
| R1 | 発行ワークフローの承認権限は、subject を制限しない signer で任意の証明書 (system:masters を含む) を作れる | ワークフローが侵害されれば cluster-admin 相当 | 安全策は main の保護 (管理者 bypass の扱いを見直す) と、ワークフロー内の厳格な検証。CSR input を env で渡しインジェクションを防ぐ。発行ワークフローに Environment を付けるかを検討する (承認者なし・main 限定の deployment branch policy なら利便性を損なわない) |
| R2 | DR 直後に RBAC (gitops) が同期されるまで DR ワークフローが認可されない | DR の停止、または ArgoCD 待ちのループの誤判定 | DR 用 binding だけを bootstrap で直接適用し ArgoCD に adopt させる (`infisical-auth` と同種の例外として steering に記録)、または RBAC Application を wave -1 にして `recovery.sh` 側で 403 を許容して待つ |
| R3 | OIDC トークン寿命 (約 5 分の見込み) < ジョブ時間 | DR と長時間処理の途中で 401 | `exec` 資格情報プラグインで都度取得する (ExecCredential の `expirationTimestamp`) |
| R4 | GitHub ユーザー名は改名や再取得ができる | 旧名の binding を別人が引き継ぐ | CN に数値 `actor_id` を含める案の検討 (Req 3.3 の `github:<ユーザー名>` との調整が要る)。または退任・改名時に binding を見直す手順を作る |
| R5 | health-check に要る `nodes/proxy` の get は、kubelet exec に転用できる | 「読取専用」ユーザーがコマンド実行権を持つ | fine-grained kubelet authz (v1.36 GA) で `nodes/stats` に絞れる経路を調べる。代替指標 (ディスク使用率の取得元の変更) を検討する。tag:ci から kubelet 10250 に到達できない ACL を確認する |
| R6 | 共有 kubeconfig の admin 証明書は `system:masters` (RBAC で剥奪できない) で、CA を変えない限り最長 365 日有効 | Infisical から削除しても漏洩済みなら残存リスクがある | 未決事項 5 参照。6443 が tailnet 限定である点が緩和要因 |
| R7 | 認証設定の不正による起動失敗が `wait_for 6443` で検知できない。単一ノードのため反映は唯一の server に直接及ぶ | クラスタ全停止 | PR #288 の etcd 健全性待ち・Ready 待ちに乗せる。反映後に JWT 認証の疎通確認 (`/readyz` に加え、テスト用の検証ステップ) を加える |
| R8 | Tailscale ACL が Terraform 管理外 | 6443 への到達可否がコードから追えない。intrusion-response の参加方式を変えると到達性が変わる | 現行 ACL の確認を設計前の調査項目にする (Terraform 化は本仕様の範囲外) |

### 要調査事項 (設計フェーズへ持ち越し)

1. k3s v1.36 で `kube-apiserver-arg: authentication-config=...` を渡したときの起動時の不正設定の挙動と、k3s が既定で渡す認証系フラグ (`--anonymous-auth` など) との衝突の有無。
2. k3s の kube-controller-manager が kube-apiserver-client CSR を client-ca で署名するか、およびその期間の上限 (`cluster-signing-duration` 相当)。
3. v1.36 の CEL 環境で使える文字列関数 (`job_workflow_ref` からワークフローファイル名を取り出す式)。
4. GitHub OIDC トークンの実際の `exp`。`exec` プラグインで再取得するときの `ACTIONS_ID_TOKEN_REQUEST_TOKEN` の有効範囲。
5. `gh workflow run` から run を特定する方法 (dispatch API の run 情報の返却可否、相関 ID の input)。
6. `/cacerts` の応答内容 (server CA のみか、中間 CA を含むか) と、`rotate-ca` を行った後の内容。
7. Tailscale ACL における 6443 と 10250 の到達許可 (人の端末、tag:ci、タグなしノード)。
8. infra-health-check のディスク使用率を `nodes/proxy` なしで取得する経路。

## 7. 未決事項ごとの判断材料

### 1. 人向けクライアント証明書の有効期限

- **選択肢**: 1 日 / 7 日 (想定) / 30 日。
- **判断材料**: 失効できないため、残存リスクの上限は有効期限になる (Req 5.5)。ただし binding を削除すれば即時に剥奪できるので、期限が抑えるのは「binding が残ったまま秘密鍵が漏洩した」場合に限られる。CSR の下限は 600 秒、上限は signer 設定 (要調査 2) による。再発行は 1 コマンドで済み (Req 4.3)、GitHub 障害時は発行済み証明書だけが頼りになる (Req 11.1)。
- **推奨**: 7 日を既定とし、ワークフロー側で上限を強制する (入力で短くするのは許可する)。GitHub 障害が数日続く可能性は低く、週 1 回の再発行は許容範囲。1 日にすると障害時の可用性と利便性が落ちる。

### 2. server CA 証明書の入手方法

- **選択肢**:
  - (a) `/cacerts` を tailnet 経由で取得する (TOFU)。
  - (b) リポジトリに配置し、DR 後に PR で更新する。
  - (c) Ansible を実行するワークフロー (DR、k3s-upgrade) は SSH で `server-ca.crt` を取得し、それ以外は (a)。
  - (d) CA のハッシュをリポジトリに置き、(a) で取得した値を照合する。
- **判断材料**: (a) は追加管理がなく DR 後も自動で追従する (Req 7.2)。経路は WireGuard でノード鍵が認証された tailnet なので、中間者になれるのは tailnet 内の攻撃者か Tailscale 自身に限られる。(b) と (d) は真正性が Git で担保されるが、DR 直後に更新 PR が通るまで CI・DR が接続できない (Req 7.4 と衝突)。また公開リポジトリに CA を置くこと自体は問題ない (公開情報)。(c) は DR の最初の接続を SSH (既に信頼している経路) で賄える。
- **推奨**: (c)。DR と k3s-upgrade は Ansible の SSH 経路で CA を取得し、人・health-check・intrusion-response は `/cacerts` を tailnet 経由で取得する。人のスクリプトは取得した CA の指紋を表示し、既存のコンテキストと異なる場合は警告する (TOFU の変化検知)。

### 3. intrusion-response の tailnet 参加方式

- **選択肢**: (a) 現行 (API キーで ephemeral・タグなし、`/etc/hosts` に追記) / (b) 他の CI と同じ `tailscale/github-action` + OAuth + `tag:ci`。
- **判断材料**: (a) は Infisical の `TF_VAR_tailscale_api_key` を読む必要があり、Req 1.9 (保存した kube 資格情報を使わない) には抵触しないが、Infisical への依存が残る。タグなしノードはキー所有者の ACL を継承するため、権限が過大になりうる。`/etc/hosts` の追記はプロジェクトの方針 (hosts の編集を避ける) とも相性が悪い。(b) は MagicDNS でそのまま名前解決でき、ACL を `tag:ci` に一本化できる。侵害対応時に Tailscale OAuth が使えないという特殊な事情は見当たらない。
- **推奨**: (b)。KUBECONFIG も Tailscale API キーも Infisical から取得しなくなり、ワークフローから Infisical 依存が消える。

### 4. k3s 監査ログの有効化

- **選択肢**: 有効化しない / Metadata レベルで有効化 (ローテーション設定付き) / RequestResponse で有効化。
- **判断材料**: 人とワークフローの username が分かれることで、識別そのものは監査ログなしでも Req 12.1 を満たす。ただし事後に追跡するには記録が要る。単一ノードのディスク予算 (WAL によるディスクフルの前例) を考えると、容量の上限 (`audit-log-maxsize` / `maxbackup` / `maxage`) が必須。外部へ転送する基盤はない (監視スタックの方針)。
- **推奨**: Metadata レベルで、system コンポーネントと read の大量アクセスを除外したポリシーで有効化し、ローテーションで上限を固定する。配布は認証設定と同じロールの経路。本仕様の範囲で行うか後続にするかは、工数との兼ね合いで決める (後続にしても他の要件には影響しない)。

### 5. 共有 kubeconfig 削除後のクライアント証明書ローテーション

- **選択肢**: (a) 実施しない (期限切れを待つ) / (b) `k3s certificate rotate` (効果なし) / (c) `k3s certificate rotate-ca` / (d) 次回の DR やクラスタ再作成に委ねる。
- **判断材料**: (b) は CA が変わらないため旧証明書を無効にできない (公式ドキュメントで確認)。(c) が唯一の即時無効化手段だが、全コンポーネント、人の証明書、場合によっては token の再構成に影響し、単一ノード・単一クラスタでの実施はリスクが高い。旧証明書は system:masters で RBAC では止められない。一方 6443 は tailnet 限定で、漏洩した証明書を使うには tailnet への参加も必要。admin 証明書の残存期間はノード上の更新日時で確認できる。
- **推奨**: 原則は (a)+(d) とし、根拠 (tailnet 限定・漏洩の兆候なし) を記録する (Req 9.3)。漏洩の疑いがある場合や Tailscale 側の侵害時だけ (c) を手順化しておく。(c) を採る場合は、cross-signed モードの手順を k3d で事前に検証する。

### 6. DR 復旧・k3s アップグレードのワークフローに与えるクラスタ管理権限の範囲

- **判断材料**:
  - DR (`recovery.sh`、PR #287 版) は argocd ns の Secret の作成・更新、全 namespace の ExternalSecret の annotate、mailserver 関連リソースの apply、各種 get を行う。手順は今後も変わりやすく (メールリストアの手動化など)、最小権限を維持し続けるコストが高い。
  - Environment `dr-recovery` の required reviewers で、実行には人の承認を挟む。
  - k3s-upgrade はランナーから kubectl を使わない (Play 5 の差分判定のみ)。Play 5 を削除すれば **kube 権限は不要**。Req 8.3 の「前後の状態確認」を追加しない限り、OIDC の受け入れ対象に含めないのが最小になる。
- **推奨**:
  - **DR**: `cluster-admin` に binding し、`environment == 'dr-recovery'`、`event_name == 'workflow_dispatch'`、`ref` が main であることを CEL で必須にする。承認ゲートを主な安全策とする。
  - **k3s-upgrade**: 当面は OIDC を許可せず、状態確認を追加するなら読取専用の ClusterRole にする。クラスタへの変更は Ansible の SSH 経路 (ノード上の admin) で行われるので、kube 側の高権限は不要。

## 8. 設計フェーズへの推奨

- Option C (ハイブリッド) を軸に設計する。
- 設計で決めること:
  1. RBAC の置き場 (新規 Application かどうか) と、DR 用 binding の bootstrap 例外。
  2. username 体系 (例: ワークフローは `gha:<file>`、人は `github:<login>`)。数値 ID を併用するかどうか (R4)。
  3. CI 共通の OIDC kubeconfig 生成 (exec プラグイン) の形態 (スクリプトか composite action か)。
  4. `make kubectl` の入口を維持するかどうか。
  5. PR #287/#288 とのマージ順序。
  6. 発行ワークフローに Environment を付けるか、main の保護における管理者 bypass の扱い (R1)。
- 第 6 章の要調査事項 1〜8 を design の research で解消する。特に 1・2・4 は k3d で再現して確かめられる。
