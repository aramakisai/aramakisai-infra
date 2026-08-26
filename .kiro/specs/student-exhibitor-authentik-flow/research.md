# Research & Design Decisions Template

## Summary
- **Feature**: `student-exhibitor-authentik-flow`
- **Discovery Scope**: Extension（既存 `invitation-enrollment` / `password-recovery` フローパターンの応用）
- **Key Findings**:
  - `authentik_user` リソースは `for_each` によるマップ駆動の一括作成に対応しており、`password` を省略すればパスワード未設定（ログイン不可）のユーザーを作成できる。既存の `terraform/main.tf`（`local.nodes`）、`access.tf`、`uptimerobot.tf` に同種の `for_each` パターンの前例がある。
  - Authentik の管理者向け「パスワード復旧メール送信」API（`core_users_recovery_email_create`、`POST /core/users/{id}/recovery_email/`）は、対象ユーザーIDと送信に使う `authentik_stage_email` インスタンスのIDを指定してトリガーする方式。ユーザー自身による識別（メールアドレス入力）ステージを経由せず、指定ユーザー宛に直接リンクを生成・送信できるため、代表者による自己識別（Requirement対象外の自己登録）を回避できる。
  - 既存 `terraform/authentik_recovery.tf` の `password-recovery` フローは「メールステージ→パスワードステージ」の2ステージのみで識別ステージを持たない。これは通常ログイン画面からの「パスワードを忘れた場合」導線用の共有フローであり、本機能専用の学生団体向けフローとして別に用意する必要がある（共有フローを流用すると、対象外ユーザーへの影響や導線混同のリスクがあるため）。
  - 既存 `terraform/authentik_enrollment.tf`（`invitation-enrollment`）に、認証済みユーザーによるフロー誤操作を防ぐ実装済みパターン（`authentik_policy_expression` で `request.user.is_authenticated` を判定し `authentik_policy_binding` でフロー本体にバインド）がある。2026-06-22 インシデントの再発防止策として確立済みであり、本機能の Requirement 5 にも同一パターンを適用する。
  - `authentik_group.student_exhibitor`（`terraform/authentik_apps.tf`）と Directus 側 `AUTH_AUTHENTIK_ROLE_MAPPING`（`gitops/manifests/prod/directus/deployment.yaml`）は既に本番設定済みであり、本機能はこのグループへユーザーを追加するのみで新規のロールマッピング変更は不要。

## Research Log

### 学生団体アカウントの一括事前作成手段
- **Context**: Requirement 1（CSV等の団体一覧からのアカウント一括作成、冪等性、部分失敗耐性）をどう実現するか。
- **Sources Consulted**: 社内既存実装（`terraform/main.tf`, `terraform/access.tf`, `terraform/uptimerobot.tf`, `terraform/authentik_mailing_lists.tf`）、[goauthentik/terraform-provider-authentik: authentik_user resource docs](https://github.com/goauthentik/terraform-provider-authentik/blob/main/docs/resources/user.md)
- **Findings**:
  - `authentik_user` は `username` / `name` / `email` / `is_active` / `password` / `attributes` / `groups` / `type` を持つ。`password` を指定しなければパスワード未設定状態で作成される。
  - リポジトリには `for_each = local.xxx`（マップ）で複数リソースを生成する確立済みパターンが複数存在する（`nodes`, `access_applications`, `uptimerobot_monitors`）。`authentik_mailing_lists.tf` は同型リソースをコピペで7件手書きしているが、件数が可変・増減する学生団体一覧には向かない。
  - Terraform の `for_each` は入力マップのキー（本機能ではメールアドレス）が変わらない限り既存リソースへの差分を生まないため、同一CSVでの再実行時に重複作成されない（Requirement 1.6 を自然に満たす）。
  - Terraform apply はリソース間の依存関係がない限り、1リソースのエラーが他リソードの作成をブロックしない（`-target` 不要で部分失敗耐性を確保できる、CLAUDE.md記載の `tailscale_tailnet_key.k3s_nodes` の既知差分運用と同様の考え方）。
- **Implications**: CSV（`terraform/data/student_exhibitors.csv` 等）を `csvdecode(file(...))` で読み込み、メールアドレスをキーとする `local` マップを経由して `authentik_user` を `for_each` 生成する設計を採用する。Terraform state がそのまま「作成済みアカウント一覧」の正本になり、CSVへの行追加・削除がそのまま差分として可視化される（steering記載の「コードベース外への知識分散を避ける」方針にも合致）。

### パスワード設定用リンクの送信方式
- **Context**: Requirement 3（一括作成完了後、各アカウントへパスワード設定用メールを送信）をどう実現するか。
- **Sources Consulted**: [core_users_recovery_email_create API reference](https://api.goauthentik.io/reference/core-users-recovery-email-create), [Email stage | authentik docs](https://docs.goauthentik.io/add-secure-apps/flows-stages/stages/email/), 既存 `terraform/authentik_recovery.tf`
- **Findings**:
  - `POST /core/users/{id}/recovery_email/` は管理者権限で任意ユーザーに対しパスワード復旧メールを送信できる。送信に使用する `authentik_stage_email` インスタンスを指定し、そのステージがバインドされているフローへ続くトークン付きリンクが生成される。
  - レスポンスは成功時 204。400/403 はリクエスト不正・権限不足。
  - このAPIはユーザーによる自己識別（メールアドレス入力）を経由しないため、代表者の自己登録・自己入力なしに直接リンクを発行できる（Boundary Context の Out of scope 要件と整合）。
  - 有効期限は `authentik_stage_email` の `token_expiry` 設定に依存する（既存 `authentik_recovery.tf` は provider の型不一致バグ回避のため `token_expiry = null` を指定し、Authentik側デフォルト値を採用している）。
- **Implications**: 本機能専用の `authentik_stage_email` インスタンスを新規フローにバインドし、実行委員会担当者が一括作成後に実行するスクリプトから対象ユーザー全件へ `recovery_email` API を呼び出す方式とする。既存の共有 `password-recovery` フローとは独立させ、影響範囲を分離する。

### パスワード設定フローの構成
- **Context**: Requirement 4/5/6（パスワードのみ入力、認証済みユーザーの拒否、設定後自動ログイン）をどう実現するか。
- **Sources Consulted**: 既存 `terraform/authentik_enrollment.tf`, `terraform/authentik_recovery.tf`
- **Findings**:
  - `authentik_stage_password` はパスワード＋パスワード確認のみを要求するステージであり、`invitation-enrollment` のプロフィール入力ステージのような追加フィールドを含まない。
  - `authentik_stage_user_login` は既存2フローともにログイン完了ステージとして使われており、Requirement 6（設定後自動ログイン）にそのまま流用できる。
  - 認証済みユーザーの誤操作防止は、`invitation-enrollment` に実装済みの `authentik_policy_expression`（`request.user.is_authenticated` 判定）＋ `authentik_policy_binding`（フロー本体、order=0）パターンをそのまま再利用できる。同一の `authentik_policy_expression` リソースを複数フローの `authentik_policy_binding` から参照可能。
- **Implications**: 新規フロー `student-exhibitor-password-setup` を作成し、「メールステージ（送信専用・識別ステージなし）→パスワードステージ→ログインステージ」の3ステージ構成とする。既存の `deny_enrollment_if_authenticated` ポリシーを本フローにも束縛する。

## Architecture Pattern Evaluation

| Option | Description | Strengths | Risks / Limitations | Notes |
|--------|-------------|-----------|---------------------|-------|
| Terraform for_each（CSV駆動） | CSVをTerraformが読み込み `authentik_user` を生成 | 冪等性・部分失敗耐性がTerraform標準機能で担保される。既存 `for_each` パターンと一貫性あり。Stateが正本になり監査しやすい | CSV変更のたびに `terraform apply` 実行が必要（手動トリガー）。大量削除時の誤operation対策が別途必要 | **採用** |
| 外部スクリプトでAuthentik API直接一括作成 | PythonスクリプトがCSVを読みAPIを直接叩く | Terraform apply不要で即時実行可 | 冪等性・部分失敗耐性を自前実装する必要がある。Stateが二重管理になりIaC原則から逸脱 | 不採用（steering「コードベース外への知識分散を避ける」に反する） |
| 既存 `invitation-enrollment` フローの拡張利用 | 招待コード方式を流用 | 実装済みフローの再利用 | 代表者の自己登録前提（プロフィール入力あり）で本要件のOut of scopeと矛盾。招待コード発行・配布の運用が別途必要 | 不採用（Boundary Contextで明示的に除外） |

## Design Decisions

### Decision: アカウント作成はTerraform管理、パスワード設定用メール送信は補助スクリプトで分離
- **Context**: Requirement 1（一括作成）と Requirement 3（メール送信）は異なる冪等性要件を持つ（前者はリソース存在ベース、後者は送信済み状態ベース）。
- **Alternatives Considered**:
  1. 単一スクリプトでユーザー作成とメール送信を両方実施
  2. Terraformでユーザー作成、外部スクリプトでメール送信（採用）
- **Selected Approach**: `authentik_user` の作成・グループ付与・`user_type` 設定はTerraformが担当する。パスワード設定用メール送信は、Terraform apply後に実行委員会担当者が実行する冪等スクリプト（`scripts/send-student-exhibitor-recovery-emails.py` 相当）が、対象グループ内ユーザーを走査し `recovery_email` APIを呼び出す。
- **Rationale**: Terraform stateとAuthentikユーザーオブジェクトのメール送信状態（一時的なトークン発行イベント）は性質が異なり、無理にTerraformで表現すると `terraform apply` のたびに再送信が走るリスクがある。送信済み管理をスクリプト側の責務に分離することで、それぞれの冪等性ロジックをシンプルに保てる。
- **Trade-offs**: 実行委員会担当者が2手順（`terraform apply` → スクリプト実行）を踏む必要がある。ただし既存の `k3s-bootstrap.yml` 等も複数手順の運用が前提であり、リポジトリの運用パターンと一貫する。
- **Follow-up**: スクリプトの送信済み管理に使うユーザー属性キー名（例: `attributes.exhibitor_recovery_sent_at`）をタスク実装時に確定する。

### Decision: パスワード設定フローは独立フローとして新規作成し、共有 `password-recovery` フローは流用しない
- **Context**: 学生団体向けのパスワード設定リンクを、実行委員・一般ユーザー向けの既存 `password-recovery` フローと共有すべきか。
- **Alternatives Considered**:
  1. 既存 `password-recovery` フローを流用
  2. 専用フロー `student-exhibitor-password-setup` を新規作成（採用）
- **Selected Approach**: 専用フローを新規作成し、専用の `authentik_stage_email` / `authentik_stage_password` / `authentik_stage_user_login` を束縛する。
- **Rationale**: 既存フローは自己識別ステージを持つ自己申告型の「パスワードを忘れた」導線であり、Requirement 5（認証済みユーザー拒否）のような学生団体固有のポリシーを追加すると、実行委員・一般ユーザー向けの既存導線に意図しない影響が及ぶ。フローを分離することで責任境界を明確にし、将来の変更が互いに影響しない。
- **Trade-offs**: ステージ定義がわずかに重複する（パスワードステージ等）。ただし既存 `invitation-enrollment` と `password-recovery` も既に別フローとして独立しており、リポジトリの既存方針と一貫する。
- **Follow-up**: なし。

## Risks & Mitigations
- CSVへの学生団体一覧記載ミス（メールアドレス誤り等）による誤送信 — 一括作成前にレビュー（PRベースのCSV変更）を必須とする運用でカバーする。
- `recovery_email` API のトークン有効期限切れ後、代表者からの問い合わせに対する再送手順が未定義 — 実装タスクで再実行可能なスクリプト設計とすることで、担当者が同スクリプトを個別ユーザー指定で再実行できるようにする。
- 一括作成対象人数が多い場合の `recovery_email` API 呼び出し頻度（レートリミット）— Authentik公式のレート制限値は本調査で確認できていない。実装時に送信間隔（簡易スロットリング）を設けることを検討する。

## References
- [authentik_user | Terraform Registry](https://github.com/goauthentik/terraform-provider-authentik/blob/main/docs/resources/user.md) — ユーザーリソースの全属性
- [core_users_recovery_email_create | authentik API reference](https://api.goauthentik.io/reference/core-users-recovery-email-create) — 復旧メール送信API
- [Email stage | authentik docs](https://docs.goauthentik.io/add-secure-apps/flows-stages/stages/email/) — メールステージの設定項目
