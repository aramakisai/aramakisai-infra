# Requirements Document

## Project Description (Input)
aramakisai-web (Next.js フロントエンド) の Cloudflare Pages プロジェクトを Terraform で管理する。
既存の Cloudflare プロバイダー設定 (cloudflare ~> 4.0、cloudflare_account_id / cloudflare_zone_id 変数) を流用し、
`terraform/pages.tf` に `cloudflare_pages_project` リソースを追加する。
環境変数・カスタムドメイン・GitHub 連携を IaC で管理し、ダッシュボード上の手動操作を排除する。

## Introduction

本仕様は `aramakisai-infra` の Terraform に Cloudflare Pages プロジェクト管理を追加するものである。
`aramakisai-web` の `frontend-scaffold` spec が完了し `wrangler.toml` が存在することを前提とする。
`cicd-pipeline` spec (aramakisai-web 側) はこのプロジェクトが Terraform で作成済みであることを前提とする。

## Boundary Context

- **In scope**:
  - `terraform/pages.tf` — `cloudflare_pages_project` リソース定義
  - GitHub リポジトリ連携設定 (deployment_configs)
  - 環境変数設定 (production / preview 別)
  - カスタムドメイン (`aramakisai.com` → Production)
  - `terraform/variables.tf` / `terraform/outputs.tf` への変数・出力追加
- **Out of scope**:
  - Next.js アプリケーションコード
  - GitHub Actions ワークフロー (cicd-pipeline spec で管理)
  - `wrangler.toml` の内容 (frontend-scaffold spec で管理)
  - Cloudflare Access によるアクセス制御 (別途 access.tf で管理)
  - staging フロントエンド用固定サブドメイン (`stg.aramakisai.com` → **廃止決定**: staging 確認は Cloudflare Pages PR preview URL を使用する)
- **Adjacent expectations**:
  - `CLOUDFLARE_API_TOKEN` は既存の Infisical 管理変数を使用
  - `var.cloudflare_account_id` / `var.cloudflare_zone_id` は既存変数を流用
  - GitHub 連携には Cloudflare Pages 用の GitHub App インストールが別途必要 (手動 or Terraform 外)
  - `cloudflare_record.stg` (既存 `terraform/dns.tf`) は本 spec の実装時に削除する

---

## Requirements

### Requirement 1: Cloudflare Pages プロジェクト作成

**Objective:** インフラ管理者として、`cloudflare_pages_project` が Terraform で定義され、`terraform apply` 一発でプロジェクトが作成・再現できることを望む。

#### Acceptance Criteria

1. The Terraform configuration shall define a `cloudflare_pages_project` resource in `terraform/pages.tf` named `aramakisai-web` under `var.cloudflare_account_id`.
2. The Pages project shall set `production_branch = "main"` so that pushes to `main` trigger production deployments.
3. When `terraform plan` is run against a fresh state, the plan shall show exactly one resource to create: `cloudflare_pages_project.aramakisai_web`.
4. The Pages project build configuration shall specify: build command (`pnpm run build`), destination directory (`.vercel/output/static`), and root directory (`frontend`).
5. The resource shall use `depends_on` or data source references to ensure `var.cloudflare_account_id` is resolved before apply.

---

### Requirement 2: 環境変数管理 (Production / Preview)

**Objective:** インフラ管理者として、production と preview で異なる Directus エンドポイントが自動的に設定されることを望む。手動でダッシュボードを操作せず Terraform apply のみで完結すること。

#### Acceptance Criteria

1. The `cloudflare_pages_project` deployment_configs shall set `NODE_VERSION = "22"` for both production and preview environments to pin the Node.js runtime used by Cloudflare's internal build system.
2. `NEXT_PUBLIC_DIRECTUS_URL` and `NEXT_PUBLIC_SITE_URL` shall NOT be set in the Cloudflare Pages deployment_configs; because the project uses GHA + `wrangler pages deploy` (not Pages native build), these values must be injected as environment variables during the GHA build step to be inlined by Next.js at compile time.
3. If non-`NEXT_PUBLIC_*` runtime environment variables are required in future, they shall be added to `terraform/pages.tf` deployment_configs and applied via `terraform apply`; direct dashboard edits shall not be used.

---

### Requirement 3: カスタムドメイン設定

**Objective:** インフラ管理者として、`aramakisai.com` が Cloudflare Pages の Production デプロイに向くよう DNS および Pages ドメイン設定が Terraform で管理されることを望む。

#### Acceptance Criteria

1. The Terraform configuration shall define a `cloudflare_pages_domain` resource associating `aramakisai.com` with the `aramakisai-web` Pages project as the production custom domain.
2. The existing `cloudflare_record` for the apex domain (`aramakisai.com`) shall be updated or created in `terraform/dns.tf` to point to the Cloudflare Pages project (CNAME to `aramakisai-web.pages.dev` or equivalent).
3. When `terraform apply` completes, `https://aramakisai.com` shall resolve to the production Pages deployment without manual DNS changes.
4. The existing `cloudflare_record.stg` resource (currently pointing to the Cloudflare Tunnel as "Staging frontend") shall be removed from `terraform/dns.tf` as part of this spec; staging frontend verification shall use the dynamic Cloudflare Pages PR preview URL instead of a fixed subdomain.

---

### Requirement 4: Cloudflare Access クリーンアップ

**Objective:** インフラ管理者として、廃止した staging フロントエンドドメインおよび誤って設定された stg-api Access 保護を Terraform から削除し、Pages preview が stg-api に直接アクセスできることを望む。

#### Acceptance Criteria

1. The `cloudflare_zero_trust_access_application.stg` resource (protecting `stg.aramakisai.com`) shall be removed from `terraform/access.tf` as the domain is being deprecated.
2. The `cloudflare_zero_trust_access_application.api_stg` resource (protecting `stg-api.aramakisai.com`) shall be removed from `terraform/access.tf`; the Cloudflare Access on staging API was a configuration mistake and staging Directus shall be accessible without Access authentication.
3. All `cloudflare_zero_trust_access_policy` resources referencing the removed applications shall be removed simultaneously to avoid orphaned state.
4. After `terraform apply`, `https://stg-api.aramakisai.com` shall be reachable without Cloudflare Access authentication; Directus's own admin credentials remain the only authentication layer.
5. When `terraform plan` is run after the removal, the plan shall show destruction of the two Access applications and their policies with no unintended side effects on other Access resources (e.g., `argocd.aramakisai.com` policy shall remain untouched).

---

### Requirement 5: Directus CORS 設定

**Objective:** 開発者として、Cloudflare Pages preview URL (`*.pages.dev`) および本番ドメイン (`aramakisai.com`) からのブラウザリクエストが Directus に到達できることを望む。CORS 未設定だと全 API コールがブロックされる。

#### Acceptance Criteria

1. The staging Directus `deployment.yaml` (`gitops/manifests/staging/directus/deployment.yaml`) shall add environment variables `CORS_ENABLED=true` and `CORS_ORIGIN=*` to allow requests from any Cloudflare Pages preview domain.
2. The production Directus `deployment.yaml` (`gitops/manifests/prod/directus/deployment.yaml`) shall add `CORS_ENABLED=true` and `CORS_ORIGIN=https://aramakisai.com` to restrict CORS to the production domain only.
3. When a Cloudflare Pages preview deployment makes a `fetch` request to `https://stg-api.aramakisai.com`, the response shall include `Access-Control-Allow-Origin` headers allowing the request to succeed.
4. If `CORS_ORIGIN` for production is extended in future (e.g., for additional domains), it shall be updated in `gitops/manifests/prod/directus/deployment.yaml` via a GitOps PR, not via the Directus admin UI.

---

### Requirement 6: Terraform 変数・出力

**Objective:** インフラ管理者として、Pages プロジェクトに関連する値が outputs として参照可能であることを望む。

#### Acceptance Criteria

1. The Terraform configuration shall add an output `pages_project_subdomain` exporting the auto-assigned `<project>.pages.dev` subdomain of the Pages project.
2. If a new variable is required (e.g., GitHub owner/repo), it shall be declared in `terraform/variables.tf` with a description and added to the Infisical secrets list in `.kiro/steering/tech.md`.
3. The `terraform/outputs.tf` addition shall follow the existing output formatting conventions in the file.

---

### Requirement 7: ステート管理・冪等性

**Objective:** インフラ管理者として、Terraform apply を複数回実行しても Pages プロジェクトが重複作成されないことを望む。

#### Acceptance Criteria

1. The `cloudflare_pages_project` resource shall be idempotent — repeated `terraform apply` runs with no config changes shall produce a plan with zero changes.
2. If the Pages project already exists in Cloudflare but not in Terraform state, the resource shall be importable via `terraform import cloudflare_pages_project.aramakisai_web <account_id>/<project_name>` without requiring destroy/recreate.
3. The Terraform state for the Pages project shall be stored in the existing HCP Terraform workspace (`aramakisai-infra`) alongside all other resources.
