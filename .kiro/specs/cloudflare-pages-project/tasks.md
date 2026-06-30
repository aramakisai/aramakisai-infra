# Implementation Plan

- [x] 1. pages.tf 作成 — Pages プロジェクト・カスタムドメイン IaC 定義
- [x] 1.1 `cloudflare_pages_project.aramakisai_web` リソースを定義する
  - `terraform/pages.tf` を新規作成し、`cloudflare_pages_project` リソースブロックを追加する
  - `name = "aramakisai-web"`、`account_id = var.cloudflare_account_id`、`production_branch = "main"` を設定する
  - `build_config` ブロックに `build_command = "pnpm run build"`、`destination_dir = ".vercel/output/static"`、`root_dir = "frontend"` を設定する
  - `deployment_configs.production` および `.preview` に `NODE_VERSION = "22"` のみを環境変数として設定し、`NEXT_PUBLIC_*` 変数は含めない
  - `source` ブロックは追加しない（GitHub App 連携は不要。GHA + `wrangler pages deploy` がデプロイを担う）
  - 既存 Pages プロジェクトが Cloudflare 上に存在する場合のインポートコマンド `terraform import cloudflare_pages_project.aramakisai_web <account_id>/aramakisai-web` をコメントとして記載しておく
  - `terraform validate` が構文エラーなしで通過する
  - _Requirements: 1.1, 1.2, 1.4, 1.5, 2.1, 2.2, 2.3, 7.1, 7.2_

- [x] 1.2 `cloudflare_pages_domain.aramakisai_web_prod` リソースを定義する
  - `pages.tf` に `cloudflare_pages_domain` リソースブロックを追加する
  - `account_id = var.cloudflare_account_id`、`project_name = cloudflare_pages_project.aramakisai_web.name`、`domain = "aramakisai.com"` を設定する
  - `terraform validate` で参照エラーなく、Pages プロジェクトへの依存関係が正しく設定されている
  - _Requirements: 3.1, 3.3_

- [x] 2. 既存ファイル変更・CORS 設定（並列実行グループ）
- [x] 2.1 (P) `dns.tf` に apex CNAME を追加し、stg レコードを削除する
  - `cloudflare_record.apex` リソースを追加する: `name = "@"`、`type = "CNAME"`、`value = "aramakisai-web.pages.dev"`、`proxied = true`、`comment = "Production frontend (Cloudflare Pages)"`
  - `cloudflare_record.stg` リソースブロックを削除する（staging frontend は Cloudflare Pages PR preview URL を使用するため廃止）
  - 既存の MX・SPF・DKIM・DMARC・mail・webmail・argocd・idp 等の DNS レコードは変更しない
  - `terraform plan` 実行時に `cloudflare_record.apex` の追加と `cloudflare_record.stg` の削除のみが差分として表示される
  - _Requirements: 3.2, 3.4_
  - _Boundary: DNS apex レコード, DNS stg レコード削除_

- [x] 2.2 (P) `access.tf` から stg・api_stg Access Application とポリシーを削除する
  - `cloudflare_zero_trust_access_application.stg` リソースブロックを削除する
  - `cloudflare_zero_trust_access_application.api_stg` リソースブロックを削除する
  - `local.access_applications` の Map から `stg` / `api_stg` キーを除去する（`for_each` によりポリシーが自動削除される）
  - `cloudflare_zero_trust_access_identity_provider.authentik` リソースおよびファイル冒頭の注意コメントは残す
  - `terraform plan` 実行時に 2 件の Application と対応ポリシーの削除のみが表示され、`cloudflare_zero_trust_access_identity_provider.authentik` や他 Access リソースへの影響がない
  - _Requirements: 4.1, 4.2, 4.3, 4.5_
  - _Boundary: Access Application 削除_

- [x] 2.3 (P) `outputs.tf` に pages_project_subdomain 出力を追加する
  - 既存の output ブロックのフォーマット（description + value）に従い `output "pages_project_subdomain"` を追加する
  - `value = cloudflare_pages_project.aramakisai_web.subdomain` で Pages 自動割り当てサブドメイン（`aramakisai-web.pages.dev`）を参照する
  - `description = "Cloudflare Pages 自動割り当てサブドメイン (<project>.pages.dev)"` を設定する
  - `terraform validate` で参照エラーなく、output が定義されている
  - _Requirements: 6.1, 6.2, 6.3_
  - _Depends: 1.1_
  - _Boundary: pages_project_subdomain output_

- [x] 2.4 (P) prod Directus `deployment.yaml` に CORS 環境変数を追加する
  - `gitops/manifests/prod/directus/deployment.yaml` の `spec.containers[0].env` 末尾に以下を追加する:
    `- name: CORS_ENABLED` / `  value: "true"` および `- name: CORS_ORIGIN` / `  value: "https://aramakisai.com"`
  - 既存の env エントリ（DB_CLIENT、PUBLIC_URL、STORAGE_* 等）は変更しない
  - CORS 変数は Secret 経由ではなく env 直書きで設定する（非機密情報のため）
  - YAML インデントが既存エントリと一致しており、`kubectl apply` でエラーが出ない形式になっている
  - _Requirements: 5.2, 5.4_
  - _Boundary: Directus prod deployment_

- [x] 2.5 (P) staging Directus `deployment.yaml` に CORS 環境変数を追加する
  - `gitops/manifests/staging/directus/deployment.yaml` の `spec.containers[0].env` 末尾に以下を追加する:
    `- name: CORS_ENABLED` / `  value: "true"` および `- name: CORS_ORIGIN` / `  value: "*"`
  - staging は Cloudflare Pages PR preview URL（`*.pages.dev`）の動的サブドメインからのリクエストを受け付けるためワイルドカード `"*"` を使用する（prod の `"https://aramakisai.com"` は使用しない）
  - YAML インデントが既存エントリと一致しており、`kubectl apply` でエラーが出ない形式になっている
  - _Requirements: 5.1, 5.3_
  - _Boundary: Directus staging deployment_

- [x] 3. Terraform 検証・動作確認
- [x] 3.1 `terraform validate` と `terraform plan` で変更差分を検証する
  - `infisical run --env=prod -- terraform -chdir=terraform validate` がエラーなしで完了することを確認する
  - `infisical run --env=prod -- terraform -chdir=terraform plan` を実行し、以下の差分が表示されることを確認する:
    - `pages.tf`: `cloudflare_pages_project.aramakisai_web`（create）、`cloudflare_pages_domain.aramakisai_web_prod`（create）
    - `dns.tf`: `cloudflare_record.apex`（create）、`cloudflare_record.stg`（destroy）
    - `access.tf`: `cloudflare_zero_trust_access_application.stg`（destroy）、`cloudflare_zero_trust_access_application.api_stg`（destroy）、対応ポリシー（destroy）
    - `outputs.tf`: `pages_project_subdomain`（output 追加）
  - `cloudflare_zero_trust_access_identity_provider.authentik` や argocd 等の既存 Access リソースが plan の変更対象に含まれていないことを確認する
  - plan の差分が想定外のリソースを含まず、apply 前に安全を確認できる状態になっている
  - _Requirements: 1.3, 4.5, 7.1, 7.3_

- [x] 3.2 `terraform apply` 後の E2E 動作を確認する
  - `https://aramakisai.com` がブラウザで Cloudflare Pages Production デプロイに到達することを確認する（`cloudflare_pages_domain` の TLS 証明書発行後。apply 直後は数分かかる場合がある）
  - `https://stg-api.aramakisai.com` が Cloudflare Access 認証リダイレクトなしに Directus 画面（またはヘルスエンドポイント）に到達することを確認する
  - ArgoCD sync 完了後に prod Directus の CORS 設定が Pod に反映されていることを `make kubectl ARGS="exec -n prod deploy/directus -- printenv CORS_ORIGIN"` 等で確認する
  - staging Directus の CORS 設定反映後、`*.pages.dev` の preview URL から `fetch('https://stg-api.aramakisai.com/server/health')` を実行し、レスポンスヘッダーに `Access-Control-Allow-Origin: *` が含まれることを確認する
  - Cloudflare アカウントに Pages プロジェクトが既に存在していた場合は `terraform import` → apply 後に plan 差分ゼロであることを確認する（冪等性）
  - _Requirements: 3.3, 4.4, 5.3, 7.2_
