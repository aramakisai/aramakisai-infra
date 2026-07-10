# Research & Design Decisions

## Summary
- **Feature**: `web-e2e-cloudflare-access-bypass`
- **Discovery Scope**: Extension（既存 `terraform/access.tf` の Cloudflare Access 構成への追加。Discovery Light を適用）
- **Key Findings**:
  - Cloudflare Access で Service Token を機能させるには、既存の `decision = "allow"` + `login_method` ポリシー（`allow_authentik`）とは独立した `decision = "non_identity"` ポリシーが必要。`allow` ポリシーの `include` に `service_token` を混在させても機能しない
  - `cloudflare_zero_trust_access_service_token` / `cloudflare_zero_trust_access_policy` は本リポジトリの provider pin（`cloudflare ~> 4.0`）でそのまま利用可能。既存 `access.tf` が同一 provider 世代で `cloudflare_zero_trust_access_policy` を使用済みのため互換性は自明
  - `client_secret` は Terraform state 上でのみ取得可能な作成時限定の値。既存 `outputs.tf` の `sensitive = true` パターン（`tunnel_token`, `vaultwarden_rbac_sync_authentik_token` 等）をそのまま踏襲できる

## Research Log

### Cloudflare Access Service Token + non_identity Policy の必要性
- **Context**: `aramakisai-web` の `staging-e2e-verification` spec が、Playwright（非対話クライアント）から Cloudflare Access 保護下のプレビュー URL へアクセスする手段として Service Token 方式を前提にした設計を先に完了させている（依存元 spec）
- **Sources Consulted**:
  - `aramakisai-web` リポジトリ `.kiro/specs/staging-e2e-verification/research.md`（該当リポジトリ側で Cloudflare 公式ドキュメントを基に検証済み）
  - `aramakisai-web` リポジトリ `.kiro/specs/staging-e2e-verification/design.md` の Supporting References（Terraform 例のベースライン）
  - 本リポジトリ `terraform/access.tf`（既存 `allow_authentik` ポリシーの実装パターン）
- **Findings**:
  - `cloudflare_zero_trust_access_service_token` は `account_id` + `name` のみ必須。`duration`（`8760h`/`17520h`/`43800h`/`87600h`/`forever`）と `min_days_for_renewal` でローテーション運用が可能。`min_days_for_renewal` 使用時は `lifecycle { create_before_destroy = true }` が必須（切替時の瞬断防止）
  - `cloudflare_zero_trust_access_policy` の `decision` は `allow`/`deny`/`non_identity`/`bypass` の4種。Service Token を機能させるには `non_identity` を使う必要がある
  - `include.service_token`（特定トークンID配列）と `include.any_valid_service_token`（任意の有効トークン）の二択。E2E 専用トークンのみ許可するため `service_token = [specific_id]` を採用
- **Implications**: 本リポジトリの `terraform/access.tf` に (1) Service Token リソース、(2) `non_identity` ポリシーの2点を追加すれば要件を満たす。`aramakisai-web` 側の設計はこれをそのまま前提にできる（後述 Design Decisions で再検証・確定）

### 既存 `access.tf` の for_each パターンと適用範囲
- **Context**: 既存 `allow_authentik` ポリシーは `local.access_applications`（現状 `aramakisai_web_workers_dev` のみを含む map）に対して `for_each` で全 Access Application に一律適用する設計になっている。新規 E2E ポリシーもこのパターンに乗せるべきか検討
- **Sources Consulted**: `terraform/access.tf` 該当箇所
- **Findings**: `local.access_applications` は将来他の Access Application が追加された際に自動的に人間向け Authentik ログインを有効化する意図の共通マップ。E2E Service Token は `aramakisai-web` 専用の要求（Requirement 2 の Boundary）であり、将来追加される他アプリに自動継承されるべきではない
- **Implications**: E2E 用ポリシーは `local.access_applications` の `for_each` に相乗りさせず、`cloudflare_zero_trust_access_application.aramakisai_web_workers_dev.id` を直接参照する専用リソースとして定義する（意図しない適用範囲拡大を防ぐ）

### Infisical secret 命名の整合
- **Context**: `aramakisai-web` 側の design.md は CI 実装を先行させており、`CF_ACCESS_CLIENT_ID` / `CF_ACCESS_CLIENT_SECRET` という環境変数名を Playwright の `extraHTTPHeaders` に直結する形で既に設計済み（`infisical run --env=staging` 経由で注入する想定）
- **Sources Consulted**: `aramakisai-web` `.kiro/specs/staging-e2e-verification/design.md`（Components and Interfaces / CI Workflow Integration セクション）
- **Findings**: 本リポジトリ側でこれと異なる secret 名を採用すると両リポジトリ間で契約不一致が生じる
- **Implications**: 本リポジトリの運用手順・design.md は `aramakisai-web` が既に前提とした `CF_ACCESS_CLIENT_ID` / `CF_ACCESS_CLIENT_SECRET` をそのまま Infisical `staging` 環境の secret 名として採用し、齟齬がないことを明記する（Requirement 3.4, 5.2 の充足）

## Architecture Pattern Evaluation

| Option | Description | Strengths | Risks / Limitations | Notes |
|--------|-------------|-----------|---------------------|-------|
| 専用ポリシー追加（採用） | 既存 `allow_authentik` を変更せず `non_identity` ポリシーを新規追加 | 既存の人間向けログインに無影響、責務分離が明確 | ポリシー数が増える | `aramakisai-web` 側設計のベースラインと一致 |
| 既存ポリシー改修 | `allow_authentik` の `include` に `service_token` を追記 | ポリシー数が増えない | Cloudflare Access の仕様上 `allow` decision では service_token を機能させられない（検証済みの既知の誤り） | 不採用 |
| `any_valid_service_token` 利用 | 特定トークンIDを指定せず任意の有効トークンを許可 | ポリシー記述がシンプル | 将来 E2E 以外の Service Token が誤って作成された場合にもバイパスが機能してしまう（最小権限違反） | 不採用、Requirement 2.2 に反する |

## Design Decisions

### Decision: E2E 専用ポリシーは `local.access_applications` の for_each に相乗りしない
- **Context**: 既存 `allow_authentik` は `for_each = local.access_applications` で全 Access Application に横展開される
- **Alternatives Considered**:
  1. 既存 for_each マップに乗せ、将来のアプリにも自動継承させる
  2. `aramakisai_web_workers_dev` を直接参照する専用リソースにする
- **Selected Approach**: 2 を採用。`cloudflare_zero_trust_access_policy.allow_e2e_service_token` は `application_id = cloudflare_zero_trust_access_application.aramakisai_web_workers_dev.id` を直接参照する単一リソースとする
- **Rationale**: E2E Service Token は `aramakisai-web` の CI 専用（Requirement の Out of Scope: 本番 `api.aramakisai.com` 等への拡張はしない）。将来他の Access Application が `local.access_applications` に追加された際、E2E バイパスが意図せず継承されるのを防ぐ
- **Trade-offs**: 複数アプリへの展開が必要になった場合は個別にポリシーを追加する必要がある（ただし要件上想定されていない）
- **Follow-up**: 将来 E2E 対象アプリが増える場合はこの決定を再評価する

### Decision: Service Token の `client_id`/`client_secret` は `sensitive` output として一度だけ露出する
- **Context**: `client_secret` は作成時にしか取得できない。CI ログや VCS に平文で残してはならない（Requirement 1.4, 3.3）
- **Alternatives Considered**:
  1. output を作らず `terraform state show` で都度取得
  2. `sensitive = true` の output を追加し、既存 `tunnel_token` 等と同じパターンで運用
- **Selected Approach**: 2。既存 `outputs.tf` のパターンに倣い `e2e_service_token_client_id` / `e2e_service_token_client_secret` を `sensitive = true` で追加し、`terraform output -raw <name>` で取得してそのまま `infisical secrets set` にパイプする運用とする
- **Rationale**: 既存の `vaultwarden_rbac_sync_authentik_token` 等と同一の運用手順に統一でき、オペレーターの学習コストがない
- **Trade-offs**: なし（既存パターンの踏襲）
- **Follow-up**: 実装後、運用手順書（design.md の Implementation Notes）にコマンド例を明記する

## Risks & Mitigations
- Service Token が失効/ローテーションされ、Infisical 側の値を更新し忘れる — `min_days_for_renewal` + `create_before_destroy` により Terraform 側は自動更新されるが、Infisical への反映は手動ステップとして残る。手順を design.md に明記し、`aramakisai-web` 側の E2E 失敗が検知トリガーになることを明記する
- `local.access_applications` に将来アプリが追加された際、E2E ポリシーの適用範囲判断を誤る — 専用リソース化（Design Decision 参照）により回避
- Infisical secret 名の两リポジトリ間齟齬 — 本 design.md で `CF_ACCESS_CLIENT_ID`/`CF_ACCESS_CLIENT_SECRET` を正本として明記し、`aramakisai-web` 側の前提と一致していることを確認済み

## References
- [aramakisai-web `.kiro/specs/staging-e2e-verification/design.md`](../../../aramakisai-web/.kiro/specs/staging-e2e-verification/design.md) — Supporting References の Terraform 例をベースラインとして採用
- [aramakisai-web `.kiro/specs/staging-e2e-verification/research.md`](../../../aramakisai-web/.kiro/specs/staging-e2e-verification/research.md) — Cloudflare Access Service Token / non_identity policy の検証ログ
- [cloudflare_zero_trust_access_service_token (Terraform Registry raw docs)](https://github.com/cloudflare/terraform-provider-cloudflare/blob/master/docs/resources/zero_trust_access_service_token.md)
- [cloudflare_zero_trust_access_policy (Terraform Registry raw docs)](https://github.com/cloudflare/terraform-provider-cloudflare/blob/master/docs/resources/zero_trust_access_policy.md)
