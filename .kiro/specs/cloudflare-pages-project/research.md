# Research & Design Decisions

---
**Purpose**: Discovery findings for the `cloudflare-pages-project` spec.

---

## Summary
- **Feature**: `cloudflare-pages-project`
- **Discovery Scope**: Extension（既存 cloudflare ~>4.0 プロバイダーへの追加）
- **Key Findings**:
  - `cloudflare_pages_project` は cloudflare provider v4 で `build_config` + `deployment_configs` ブロックを持つ。`deployment_configs.production.environment_variables` で key-value を管理。
  - apex ドメイン (`aramakisai.com`) への CNAME は Cloudflare が CNAME Flattening を行うため `proxied = true` のまま設定可能。既存の MX/TXT レコードと共存できる。
  - `cloudflare_zero_trust_access_policy.allow_authentik` は `for_each = local.access_applications` で定義されているため、`stg` / `api_stg` を Map から除去するだけでポリシーも自動削除される。別途 `cloudflare_zero_trust_access_policy` リソースを個別削除する必要はない。

## Research Log

### cloudflare_pages_project リソーススキーマ (provider ~>4.0)

- **Context**: Terraform で Pages プロジェクトを IaC 管理するリソース名とブロック構造を確認。
- **Sources Consulted**: Terraform Registry cloudflare provider v4 ドキュメント, 既存 `terraform/access.tf` / `providers.tf` パターン
- **Findings**:
  - リソース: `cloudflare_pages_project`
  - 必須: `account_id`, `name`, `production_branch`
  - オプション: `build_config { build_command, destination_dir, root_dir }`, `deployment_configs { production { environment_variables }, preview { environment_variables } }`
  - Computed 出力: `subdomain` (auto-assigned `<project>.pages.dev`)
  - GitHub 連携は Pages native build を使う場合のみ `source` ブロックが必要。今回は GHA + `wrangler pages deploy` を使うため `source` ブロック不要。
- **Implications**: `build_config` は Pages ダッシュボード上に表示されるが、GHA が `wrangler pages deploy` で静的ファイルを直接アップロードするため実際のビルドには使われない。`NODE_VERSION` 環境変数は Pages ビルドシステムのランタイムピン用として設定する（GHA ビルドには影響しない）。

### apex ドメイン DNS 設計

- **Context**: `aramakisai.com` (apex) を Cloudflare Pages にポイントする DNS 設計。
- **Sources Consulted**: 既存 `terraform/dns.tf`（`@` ネームで MX / SPF TXT が存在）、Cloudflare CNAME Flattening ドキュメント
- **Findings**:
  - Cloudflare は apex の CNAME を CNAME Flattening で処理する（RFC に違反しない）。
  - `proxied = true` かつ `type = "CNAME"`, `name = "@"`, `value = "aramakisai-web.pages.dev"` で動作。
  - 既存の MX / TXT (`@`) レコードは CNAME と共存可能（Cloudflare が処理）。
  - `cloudflare_pages_domain` リソースで Pages プロジェクトとドメインを紐付けることで、TLS 証明書も自動発行される。
- **Implications**: apex DNS レコード追加 + `cloudflare_pages_domain` の2リソースが必要。DNS レコード単体では Pages にトラフィックが届かない（Pages domain verification が必要なため）。

### Access アプリケーション削除の影響範囲

- **Context**: `stg` / `api_stg` Access Application 削除時に関連ポリシーがどう扱われるか。
- **Sources Consulted**: 既存 `terraform/access.tf`
- **Findings**:
  - `cloudflare_zero_trust_access_policy.allow_authentik` は `for_each = local.access_applications` で定義。
  - `local.access_applications` は `authentik_configured` が true のとき `{ stg = ..., api_stg = ... }` を返す。
  - Application リソースを削除し Map から `stg` / `api_stg` を除去すれば、for_each により対応するポリシーも自動削除。
  - `authentik_configured` が true の状態で apply した場合、Terraform が削除順序を適切に解決する（Application 削除前にポリシー削除）。
  - `argocd.aramakisai.com` は Access Application が存在しないため（非保護）、影響なし。
- **Implications**: `local.access_applications` の Map から `stg` / `api_stg` を削除するだけでポリシーも連動削除。個別のポリシーリソース操作は不要。

### Directus CORS 設定

- **Context**: `CORS_ENABLED` / `CORS_ORIGIN` の Directus 公式設定方法確認。
- **Sources Consulted**: 既存 `gitops/manifests/prod/directus/deployment.yaml`, Directus 環境変数ドキュメント
- **Findings**:
  - Directus は `CORS_ENABLED=true` + `CORS_ORIGIN` 環境変数で CORS を制御。
  - `CORS_ORIGIN=*` はワイルドカード（全オリジン許可）、特定ドメインは `https://example.com` 形式。
  - 既存 deployment.yaml は env ブロックに平文変数を追加する形式（`secretRef` は envFrom で別途注入）。CORS 値は機密ではないため Secret 経由不要。
- **Implications**: 各 deployment.yaml の `spec.containers[0].env` に2行追加するだけ。GitOps PR → ArgoCD sync で反映。

## Architecture Pattern Evaluation

| Option | Description | Strengths | Risks / Limitations | Notes |
|--------|-------------|-----------|---------------------|-------|
| 新規 pages.tf + 既存ファイル編集 | リソース種別ごとの既存ファイル粒度に従い pages.tf を新規作成、dns.tf / access.tf / outputs.tf を編集 | 既存パターンと完全一致、変更範囲が明確 | なし | 採用 |
| access.tf に pages domain 設定を追加 | pages.tf を作らず access.tf に統合 | ファイル数削減 | 責務混在、既存構造のファイル粒度ルールに違反 | 不採用 |

## Design Decisions

### Decision: `source` ブロックなしで Pages プロジェクトを作成

- **Context**: `cloudflare_pages_project` の `source` ブロック（GitHub 連携）を含めるか否か。
- **Alternatives Considered**:
  1. `source` ブロック込みで GitHub リポジトリ / ブランチを Terraform から指定
  2. `source` ブロックなし（GHA + `wrangler pages deploy` でデプロイ）
- **Selected Approach**: `source` ブロックなし
- **Rationale**: cicd-pipeline spec が GHA + `wrangler pages deploy` を管理する設計。Pages native build は使わない。`source` ブロックがあると GitHub App インストール（Terraform 外の手動作業）が必須になり、apply の前提条件が増える。
- **Trade-offs**: ビルド設定は `build_config` に記載するが実際には使われない（GHA が静的ファイルを直接 deploy）。ダッシュボード上の表示と実挙動が乖離するが、許容範囲。
- **Follow-up**: `build_config` に記載する値は wrangler.toml と整合していること（frontend-scaffold spec 完了後に確認）。

### Decision: `NEXT_PUBLIC_*` 変数を deployment_configs に含めない

- **Context**: `NEXT_PUBLIC_DIRECTUS_URL` / `NEXT_PUBLIC_SITE_URL` を Pages に設定するか。
- **Selected Approach**: deployment_configs に含めない
- **Rationale**: Next.js の `NEXT_PUBLIC_*` はビルド時にバンドルにインライン化される。GHA ビルドステップで注入しなければ undefined になる。Pages 側で設定しても GHA ビルドには影響しない。
- **Trade-offs**: GHA ワークフロー（cicd-pipeline spec）で環境変数を管理する必要があるが、それは別 spec の責務。

## Risks & Mitigations

- **apex CNAME と既存 MX の共存**: Cloudflare が CNAME Flattening で対処するため問題なし。apply 前に `terraform plan` で既存 MX/SPF への影響がないことを確認する。
- **Access Application 削除後の `stg-api.aramakisai.com` へのアクセス**: Directus 自身の認証（admin password）は残る。意図的な設定解除であることを PR に明記する。
- **Pages プロジェクト名の重複**: Cloudflare アカウント内でプロジェクト名はユニーク。すでに手動作成済みの場合は `terraform import` が必要（要件 7.2）。
- **`cloudflare_pages_domain` の TLS 証明書発行遅延**: Pages がドメイン検証を完了するまで数分かかる場合がある。DNS 伝播後に自動で完了するため apply 後に待機が必要。

## References
- Cloudflare Terraform Provider v4 `cloudflare_pages_project`: https://registry.terraform.io/providers/cloudflare/cloudflare/latest/docs/resources/pages_project
- Cloudflare Terraform Provider v4 `cloudflare_pages_domain`: https://registry.terraform.io/providers/cloudflare/cloudflare/latest/docs/resources/pages_domain
- Cloudflare CNAME Flattening: https://developers.cloudflare.com/dns/cname-flattening/
- Directus 環境変数リファレンス (CORS): https://docs.directus.io/self-hosted/config-options.html#cors
