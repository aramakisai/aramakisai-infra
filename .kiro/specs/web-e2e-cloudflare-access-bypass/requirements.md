# Requirements Document

## Project Description (Input)
`aramakisai-web` リポジトリの `staging-e2e-verification` spec（Playwright による staging プレビュー環境の自動 E2E 検証）を実装するにあたり、プレビュー URL の実体 `aramakisai-web.aramakisai.workers.dev` が本リポジトリの Terraform（`terraform/access.tf`, `terraform/authentik_apps.tf`）で Cloudflare Access（Authentik OIDC, `auto_redirect_to_identity = true`）保護下にあることが判明した。Playwright のような非対話的クライアントは人間向け Authentik ログインを完走できないため、Cloudflare Access の Service Token による認証バイパスを本リポジトリ側に追加する必要がある。

## Introduction

本仕様は、`aramakisai-web` の `staging-e2e-verification` spec からの依存要求を受け、`aramakisai-infra` リポジトリの Cloudflare Access 設定に E2E テスト専用の Service Token バイパス機構を追加するものである。`aramakisai-web` 側の設計調査（同リポジトリ `.kiro/specs/staging-e2e-verification/research.md` / `design.md`）で、Cloudflare Access の Service Token を機能させるには既存の `decision = "allow"` ポリシー（`terraform/access.tf` の `allow_authentik`）とは独立した `decision = "non_identity"` ポリシーが必要であること、および `service_token` を `include` する必要があることが確認されている。本仕様ではこの知見に基づき、既存の人間向け Authentik ログインポリシーを変更せずに、E2E CI 専用の Service Token とそれを許可する非対話的ポリシーを追加する。

## Boundary Context

- **In scope**:
  - `terraform/access.tf` への `cloudflare_zero_trust_access_service_token` リソース（E2E CI 専用）の追加
  - `terraform/access.tf` への `decision = "non_identity"` の新規 Access Policy 追加（既存 `allow_authentik` ポリシーと並存させる）
  - 発行された Service Token の `client_id`/`client_secret` を Infisical `staging` 環境に投入する運用手順の確立
  - `aramakisai-web` 側 CI が参照する secret 名の契約（命名規約）の確定と、その情報を `aramakisai-web` リポジトリ側へ伝達すること
- **Out of scope**:
  - `aramakisai-web` リポジトリ側の Playwright 実装・CI ワークフロー変更（`aramakisai-web` 側 `staging-e2e-verification` spec の対象）
  - Authentik OIDC を用いた既存の人間向け Cloudflare Access ログインフロー（`allow_authentik` ポリシー）の変更
  - `aramakisai-web.aramakisai.workers.dev` 以外の Access Application（ArgoCD, Roundcube 等）の設定変更
  - Service Token の利用範囲を staging 以外（本番 `api.aramakisai.com` 等）に拡張すること
- **Adjacent expectations**:
  - `aramakisai-web` の `staging-e2e-verification` design.md（Supporting References）に記載された Terraform 例をベースラインとして扱い、大きく逸脱する場合は理由を明記する
  - 本リポジトリの Terraform 規約（`terraform/providers.tf` の `cloudflare` provider `~> 4.0`、`nonsensitive()` によるセンシティブ値の安全な比較パターン等、`access.tf`/`authentik_apps.tf` の既存記法）に従う
  - Infisical シークレット命名・投入手順は既存の `authentik_cf_client_id`/`authentik_cf_client_secret` 等の HCP Terraform 変数管理パターンに準じる

---

## Requirements

### Requirement 1: E2E 専用 Cloudflare Access Service Token の発行

**Objective:** インフラ管理者として、`aramakisai-web` の E2E テストクライアント専用の Cloudflare Access Service Token を、人間ユーザーの認証情報とは独立した形で発行したい。

#### Acceptance Criteria

1. The `terraform/access.tf` shall define a `cloudflare_zero_trust_access_service_token` resource dedicated to E2E CI usage, distinct from any human user's Authentik credentials or existing service tokens.
2. The Service Token resource shall specify an explicit `name` that identifies its purpose (e.g. referencing "aramakisai-web E2E CI") so its intent is clear in the Cloudflare dashboard and Terraform state.
3. Where token rotation is desired, the resource shall support `min_days_for_renewal` with `lifecycle { create_before_destroy = true }`, so a renewal does not cause a brief access gap for the E2E pipeline.
4. The Terraform configuration shall NOT output or log the generated `client_secret` in plaintext to any world-readable location (e.g. CI logs, VCS-tracked files); it shall remain a sensitive Terraform state value.

---

### Requirement 2: 非対話的アクセスを許可する Access Policy の追加

**Objective:** インフラ管理者として、E2E Service Token を提示したリクエストが Cloudflare Access を通過できるようにしたい。ただし既存の人間向け Authentik ログインポリシーは変更しない。

#### Acceptance Criteria

1. The `terraform/access.tf` shall define a new `cloudflare_zero_trust_access_policy` for the `aramakisai_web_workers_dev` application with `decision = "non_identity"`, since Cloudflare Access requires this decision type (not `allow`) for service token-based include rules to take effect.
2. The new policy's `include` block shall reference the specific Service Token resource from Requirement 1 (`service_token = [...]`) rather than `any_valid_service_token`, so that only the designated E2E token — not any arbitrary valid token — can bypass Access on this application.
3. The new policy shall be assigned a `precedence` value distinct from the existing `allow_authentik` policy's `precedence = 1`, and the existing `allow_authentik` policy shall remain unmodified, so human users continue to authenticate via Authentik OIDC exactly as before.
4. When a request presents valid Service Token headers (`CF-Access-Client-Id`, `CF-Access-Client-Secret`) matching Requirement 1's token, Cloudflare Access shall grant access to `aramakisai-web.aramakisai.workers.dev` without redirecting to the Authentik login flow.
5. If a request presents invalid, expired, or no Service Token headers and is not an authenticated human session, then Cloudflare Access shall continue to deny or redirect to Authentik login, consistent with current behavior.

---

### Requirement 3: Infisical への Secret 投入と命名契約

**Objective:** インフラ管理者として、発行した Service Token の認証情報を安全に `aramakisai-web` の CI から参照可能にしたい。

#### Acceptance Criteria

1. After `terraform apply` creates the Service Token, the operator shall record the resulting `client_id`/`client_secret` into the shared Infisical project's `staging` environment (the same Infisical project referenced by both repositories' `.infisical.json`), since `client_secret` is only retrievable from Terraform state at creation time.
2. The Infisical secret names used for the Service Token credentials shall be explicitly documented (e.g. in this spec's design or steering) so `aramakisai-web`'s CI workflow can reference them by an agreed-upon name without ambiguity.
3. The Service Token credentials shall NOT be committed to either repository's Git history in plaintext.
4. Where the Infisical secret names differ from what `aramakisai-web`'s `staging-e2e-verification` design assumed, this spec's design shall record the final agreed names so both repositories stay in sync.

---

### Requirement 4: ローテーション・失効時の可観測性

**Objective:** セキュリティ担当者として、Service Token が失効・ローテーションされた場合に、E2E パイプラインの失敗として気づける状態にしたい（サイレントな検証スキップを避けたい）。

#### Acceptance Criteria

1. If the Service Token is revoked or expired, then Cloudflare Access shall deny requests presenting it (fail closed), rather than silently falling back to an unauthenticated or degraded access mode.
2. The design shall document the operational procedure for rotating the Service Token (re-running `terraform apply` with `min_days_for_renewal` triggering renewal, or manual replacement), including the follow-up step of updating the Infisical secret value.
3. Where the Service Token is intentionally revoked (e.g. security incident), the resulting Cloudflare Access denial shall be distinguishable (via HTTP status behavior) from an application-level failure on the `aramakisai-web` side, consistent with the E2E job's error-handling design in `staging-e2e-verification`.

---

### Requirement 5: aramakisai-web リポジトリへの情報連携

**Objective:** `aramakisai-web` の CI 実装者として、本リポジトリ側の変更内容（Service Token の Infisical secret 名、Access Policy の挙動）を過不足なく把握したい。

#### Acceptance Criteria

1. When this spec's implementation is complete, the design/tasks documentation shall summarize the concrete Infisical secret names and any operational prerequisites needed by `aramakisai-web`'s `e2e` CI job, in a form referenceable from the other repository.
2. The `aramakisai-web` repository's `staging-e2e-verification` spec's Out of Boundary / Allowed Dependencies section shall remain accurate after this implementation; if actual secret names or policy behavior deviate from what that spec assumed, this spec shall note the deviation explicitly so the other repository's spec can be revised.
