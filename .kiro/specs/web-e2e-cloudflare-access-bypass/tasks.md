# Implementation Plan

- [x] 1. Cloudflare Access Service Token 発行基盤
- [x] 1.1 E2E CI 専用 Service Token リソースを定義しローテーション運用を組み込む
  - `terraform/access.tf` に `cloudflare_zero_trust_access_service_token.e2e_ci` を追加し、`name` に用途（aramakisai-web E2E CI）が識別できる値を設定する
  - `duration = "8760h"`、`min_days_for_renewal = 30` を指定し、`lifecycle { create_before_destroy = true }` を付与してローテーション時の瞬断を防ぐ
  - Observable: `terraform plan` でリソース新規作成として表示され、既存の `allow_authentik` ポリシーや `aramakisai_web_workers_dev` Application に差分が出ない
  - _Requirements: 1.1, 1.2, 1.3_

- [x] 1.2 client_id/client_secret を安全に取得できる sensitive output を追加する
  - `terraform/outputs.tf` に `e2e_service_token_client_id` / `e2e_service_token_client_secret` を `sensitive = true` で追加する
  - 両 output の `description` に Infisical `CF_ACCESS_CLIENT_ID`/`CF_ACCESS_CLIENT_SECRET` へ手動反映する運用である旨を明記する
  - Observable: `terraform plan`/`apply` の出力で両値が `(sensitive value)` としてマスクされ、`terraform output -raw <name>` を明示実行した場合のみ値が表示される
  - _Requirements: 1.4, 3.1, 3.3_

- [x] 2. 非対話アクセスを許可する Access Policy の追加
- [x] 2.1 E2E Service Token 限定の non_identity Policy を定義する
  - `terraform/access.tf` に `cloudflare_zero_trust_access_policy.allow_e2e_service_token` を追加し、`application_id` は `cloudflare_zero_trust_access_application.aramakisai_web_workers_dev.id` を直接参照する（`local.access_applications` の `for_each` には相乗りさせない）
  - `decision = "non_identity"`、`precedence = 2`、`include { service_token = [cloudflare_zero_trust_access_service_token.e2e_ci.id] }` とし `any_valid_service_token` は使用しない
  - 既存 `cloudflare_zero_trust_access_policy.allow_authentik`（`precedence = 1`）は変更しない
  - Observable: `terraform plan` で新規 Policy 追加のみが表示され、`allow_authentik` リソースの diff がゼロになる
  - _Requirements: 2.1, 2.2, 2.3_

- [x] 3. Infisicalへの認証情報投入とドキュメント同期
- [x] 3.1 terraform apply を実行し Service Token 認証情報を Infisical staging 環境へ投入する
  - `infisical run -- terraform -chdir=terraform apply` で Service Token と Policy を作成する
  - `terraform output -raw e2e_service_token_client_id` / `e2e_service_token_client_secret` の値を標準出力に平文表示させず、`infisical secrets set CF_ACCESS_CLIENT_ID`/`CF_ACCESS_CLIENT_SECRET --env=staging` へパイプし出力を `/dev/null` に抑制する
  - Observable: Infisical `staging` 環境に `CF_ACCESS_CLIENT_ID`/`CF_ACCESS_CLIENT_SECRET` が存在し、値がコミット履歴・CIログ・シェル標準出力のいずれにも平文で残っていない
  - _Requirements: 3.1, 3.3, 4.2_

- [x]* 3.2 (P) steering/tech.md のシークレット一覧を今回追加した Infisical キーで同期する
  - 「Infisical で管理するシークレット一覧」に `CF_ACCESS_CLIENT_ID` / `CF_ACCESS_CLIENT_SECRET`（用途: aramakisai-web E2E CI の Cloudflare Access Service Token）を追記する
  - `aramakisai-web` 側 `staging-e2e-verification` spec が前提とした secret 名と一致しており乖離がないことを注記する
  - Observable: `git diff .kiro/steering/tech.md` で上記追記の差分が確認できる
  - _Requirements: 3.2, 3.4, 5.1, 5.2_
  - _Boundary: .kiro/steering/tech.md_

- [x] 4. Cloudflare Access バイパスの動作検証
- [x] 4.1 Service Token 有効時に Authentik ログインを経由せずアクセス許可されることを確認する
  - `CF-Access-Client-Id`/`CF-Access-Client-Secret` ヘッダを付与した `curl` リクエストを `aramakisai-web.aramakisai.workers.dev` に送り、Authentik ログインへのリダイレクトが発生せず直接プロキシされることを確認する
  - Observable: レスポンスが 200 系でありレスポンスヘッダ/ボディに Authentik ログイン画面へのリダイレクトが含まれない
  - _Requirements: 2.4_

- [x] 4.2 Service Token 無効・失効・未提示時の fail-closed 挙動と既存ログインフローの非影響を確認する
  - ヘッダなし、または不正な値でのリクエストが引き続き Authentik ログインへリダイレクトされる（`allow_authentik` ポリシーが従来通り機能する）ことを確認する
  - Service Token を意図的に無効な値に差し替えたリクエストが deny され、そのレスポンス（HTTPステータス/リダイレクト有無）がアプリケーション層の 5xx エラーとは異なる形で識別できることを確認する
  - Observable: 有効ヘッダなしのリクエストが Authentik ログイン画面へリダイレクトされ、無効な Service Token ヘッダのリクエストは deny されアプリケーション層エラーと区別可能なレスポンスとして記録される
  - _Requirements: 2.5, 4.1, 4.3_
