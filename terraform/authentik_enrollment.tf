# ============================================================
# 招待制ユーザー登録フロー (invitation-enrollment) 定義
# ============================================================
# Authentik 管理画面で手動作成済みのフローを authentik_imports.tf
# 経由でインポートし、IaC 管理下に置く。

# Authentik デフォルトの共有プロンプトフィールド/ポリシー ID
# これらは Authentik の初期ブループリントが作成するオブジェクトで、
# default-user-settings-flow / initial-setup フローからも参照されているため、
# Terraformではインポートせず ID 直参照のみとする。
locals {
  default_locale_field_id          = "dfc629b0-8f69-420e-993b-1708cc6972cd" # default-user-settings-field-locale
  default_password_field_id        = "58ae2c86-d6c2-4618-8074-90c5c1c0246b" # initial-setup-field-password
  default_password_repeat_field_id = "f5c79017-50fc-4d97-9e4f-af57da5c2d13" # initial-setup-field-password-repeat
  default_password_policy_id       = "3d21cb87-d99b-4e66-85d9-80bdfbb96296" # default-password-change-password-policy
}

# フロー本体
resource "authentik_flow" "invitation_enrollment" {
  name               = "invitation-enrollment"
  slug               = "invitation-enrollment"
  title              = "荒牧祭実行委員会SSO登録"
  designation        = "enrollment"
  background         = ""
  compatibility_mode = false
}

# Stage 1 (order=10): 招待コード検証 (Invitation Stage)
resource "authentik_stage_invitation" "invitation_verification" {
  name                             = "invitation-verification-stage"
  continue_flow_without_invitation = false
}

# Stage 2 (order=20): プロフィール入力 (Prompt Stage) で使用するフィールド
resource "authentik_stage_prompt_field" "enrollment_username" {
  name        = "username"
  field_key   = "username"
  label       = "ユーザー名"
  type        = "username"
  required    = true
  placeholder = "メールアドレスに使用されます"
}

resource "authentik_stage_prompt_field" "enrollment_displayname" {
  name        = "displayname"
  field_key   = "name"
  label       = "表示名"
  type        = "text"
  required    = true
  placeholder = "山田太郎"
}

resource "authentik_stage_prompt_field" "enrollment_student_id" {
  name      = "student-id-prompt"
  field_key = "attributes.student_id"
  label     = "学籍番号"
  type      = "text"
  required  = true
}

resource "authentik_stage_prompt_field" "enrollment_email" {
  name        = "enrollment-email-prompt"
  field_key   = "email"
  label       = "メールアドレス"
  type        = "email"
  required    = true
  placeholder = "your@email.example" # confidential:allow
}

# Stage 2 (order=20): プロフィール入力 (Prompt Stage)
resource "authentik_stage_prompt" "enrollment_user_profile" {
  name = "user-profile-prompt-stage"

  fields = [
    local.default_locale_field_id,
    authentik_stage_prompt_field.enrollment_username.id,
    authentik_stage_prompt_field.enrollment_displayname.id,
    authentik_stage_prompt_field.enrollment_student_id.id,
    authentik_stage_prompt_field.enrollment_email.id,
  ]

  validation_policies = []
}

# Stage 3 (order=30): パスワード入力 (Prompt Stage)
resource "authentik_stage_prompt" "enrollment_user_password" {
  name = "user-password-prompt-stage"

  fields = [
    local.default_password_field_id,
    local.default_password_repeat_field_id,
  ]

  validation_policies = [
    local.default_password_policy_id,
  ]
}

# Stage 4 (order=40): ユーザー作成 (User Write Stage)
resource "authentik_stage_user_write" "enrollment_user_write" {
  name                     = "user-write-stage"
  user_creation_mode       = "create_when_required"
  create_users_as_inactive = true
  user_type                = "internal"
}

# Stage 5 (order=50): 自動ログイン (User Login Stage)
resource "authentik_stage_user_login" "enrollment_user_login" {
  name                     = "user-login-stage"
  session_duration         = "seconds=0"
  terminate_other_sessions = false
  remember_me_offset       = "seconds=0"
  network_binding          = "bind_asn"
  geoip_binding            = "bind_continent"
  remember_device          = "days=30"
}

# Stage 4.5 (order=45): メールアドレス検証 (Email Verification Stage)
# ユーザーの個人メールに確認コードを送信し、リンククリックでユーザーを有効化する。
# User Write Stage が create_users_as_inactive=true でユーザーを作成し、
# このステージの activate_user_on_success=true で有効化される。
resource "authentik_stage_email" "enrollment_email_verification" {
  name                     = "enrollment-email-verification-stage"
  use_global_settings      = true
  subject                  = "荒牧祭実行委員会SSO メールアドレス確認"
  template                 = "email/password_reset.html"
  token_expiry             = null
  activate_user_on_success = true
}

# フローとステージのバインディング
resource "authentik_flow_stage_binding" "enrollment_invitation_bind" {
  target               = authentik_flow.invitation_enrollment.uuid
  stage                = authentik_stage_invitation.invitation_verification.id
  order                = 10
  evaluate_on_plan     = false
  re_evaluate_policies = true
}

resource "authentik_flow_stage_binding" "enrollment_user_profile_bind" {
  target               = authentik_flow.invitation_enrollment.uuid
  stage                = authentik_stage_prompt.enrollment_user_profile.id
  order                = 20
  evaluate_on_plan     = false
  re_evaluate_policies = true
}

resource "authentik_flow_stage_binding" "enrollment_user_password_bind" {
  target               = authentik_flow.invitation_enrollment.uuid
  stage                = authentik_stage_prompt.enrollment_user_password.id
  order                = 30
  evaluate_on_plan     = false
  re_evaluate_policies = true
}

resource "authentik_flow_stage_binding" "enrollment_user_write_bind" {
  target               = authentik_flow.invitation_enrollment.uuid
  stage                = authentik_stage_user_write.enrollment_user_write.id
  order                = 40
  evaluate_on_plan     = false
  re_evaluate_policies = true
}

resource "authentik_flow_stage_binding" "enrollment_email_verification_bind" {
  target               = authentik_flow.invitation_enrollment.uuid
  stage                = authentik_stage_email.enrollment_email_verification.id
  order                = 45
  evaluate_on_plan     = false
  re_evaluate_policies = true
}

resource "authentik_flow_stage_binding" "enrollment_user_login_bind" {
  target               = authentik_flow.invitation_enrollment.uuid
  stage                = authentik_stage_user_login.enrollment_user_login.id
  order                = 50
  evaluate_on_plan     = false
  re_evaluate_policies = true
}

# ============================================================
# ログイン中ユーザーによるフロー再入場の拒否ポリシー
# ============================================================
# インシデント (2026-06-22): ログイン中の状態で招待リンクを開くと、
# User Write Stage (create_when_required) が新規ユーザーを作らず、
# ログイン中の既存ユーザー (今回は superuser) の email/password を
# 検証なしに上書きしてしまった。再発防止のため、認証済みユーザーは
# このフローに入れずログアウトを促すポリシーをフロー本体にバインドする。
resource "authentik_policy_expression" "deny_enrollment_if_authenticated" {
  name       = "deny-enrollment-if-authenticated"
  expression = <<-EOT
if request.user.is_authenticated:
    ak_message("既にログイン中のため、このリンクは使用できません。一度ログアウトしてから開いてください。")
    return False
return True
EOT
}

resource "authentik_policy_binding" "enrollment_deny_if_authenticated_bind" {
  target  = authentik_flow.invitation_enrollment.uuid
  policy  = authentik_policy_expression.deny_enrollment_if_authenticated.id
  order   = 0
  enabled = true
}

# 2026-06-22 にTerraform実行不可 (HCP Terraform org認証エラー) のため
# 上記2リソースはAuthentik APIで直接作成済み。復旧後に import すること:
#   terraform import authentik_policy_expression.deny_enrollment_if_authenticated 2d9f400a-cec2-408e-a6f7-005b416dcc44
#   terraform import authentik_policy_binding.enrollment_deny_if_authenticated_bind ac5acf28-2721-4b3d-ad28-a06c5bbceebe
