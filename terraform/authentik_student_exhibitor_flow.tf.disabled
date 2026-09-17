# ============================================================
# 学生団体（出展団体）パスワード設定フロー
# ============================================================

resource "authentik_flow" "student_exhibitor_password_setup" {
  name        = "Student Exhibitor Password Setup"
  slug        = "student-exhibitor-password-setup"
  title       = "パスワード設定"
  designation = "recovery"
}

# Stage 1 (order=05): Eメール送信ステージ
resource "authentik_stage_email" "student_exhibitor_email" {
  name                = "student-exhibitor-email-stage"
  use_global_settings = true
  subject             = "学生団体アカウント パスワード設定"
  # password_reset.html は「パスワード変更をリクエストした」文言のため、
  # 初回パスワード登録である本フローでは文脈が合わない(実機検証で確認、2026-08-26)。
  # account_confirmation.html は url/user/expires/token のみ参照し (invitation.html
  # は本テンプレートにない host 変数を参照し空欄崩れする)、
  # invitation-enrollment の enrollment_email_verification と同一テンプレートを踏襲する。
  template = "email/account_confirmation.html"
  # provider の schema が string 型になっており、APIとも一致しているため値を設定
  token_expiry = "hours=24"
}

# Stage 2 (order=10): パスワード入力ステージ
# authentik_stage_password (既存パスワードの検証専用ステージ) は不採用。
# PLAN_CONTEXT_PENDING_USER を前提に既存パスワードを照合するだけで、
# 新規パスワードの入力フォームを持たないため、フォーム非表示のまま
# スキップされパスワード未設定で自動ログインへ抜ける不具合を確認済み
# (実機検証、2026-08-26)。invitation-enrollment の
# authentik_stage_prompt.enrollment_user_password と同型のパターンを踏襲する。
resource "authentik_stage_prompt" "student_exhibitor_password" {
  name = "student-exhibitor-password-prompt-stage"

  fields = [
    local.default_password_field_id,
    local.default_password_repeat_field_id,
  ]

  validation_policies = [
    local.default_password_policy_id,
  ]
}

# Stage 3 (order=15): ユーザー書き込みステージ
# user_creation_mode = never_create: 既存ユーザー (Provisioningで作成済み) の
# prompt_data.password を書き込むのみで、新規ユーザー作成は行わない。
resource "authentik_stage_user_write" "student_exhibitor_user_write" {
  name               = "student-exhibitor-user-write-stage"
  user_creation_mode = "never_create"
}

# Stage 4 (order=20): 自動ログインステージ
resource "authentik_stage_user_login" "student_exhibitor_login" {
  name                     = "student-exhibitor-login-stage"
  session_duration         = "seconds=0"
  terminate_other_sessions = false
  remember_me_offset       = "seconds=0"
  network_binding          = "bind_asn"
  geoip_binding            = "bind_continent"
  remember_device          = "days=30"
}

# フローとステージのバインディング
# evaluate_on_plan = false: パスワードステージにバインドしたパスワードポリシーが
# フロープランニング時点(パスワード未入力)で評価され FlowNonApplicableException に
# なるのを防ぐ (authentik_enrollment.tf の既存パターンを踏襲、実機検証で確認・2026-08-16)。
resource "authentik_flow_stage_binding" "student_exhibitor_email_bind" {
  target               = authentik_flow.student_exhibitor_password_setup.uuid
  stage                = authentik_stage_email.student_exhibitor_email.id
  order                = 5
  evaluate_on_plan     = false
  re_evaluate_policies = true
}

resource "authentik_flow_stage_binding" "student_exhibitor_password_bind" {
  target               = authentik_flow.student_exhibitor_password_setup.uuid
  stage                = authentik_stage_prompt.student_exhibitor_password.id
  order                = 10
  evaluate_on_plan     = false
  re_evaluate_policies = true
}

resource "authentik_flow_stage_binding" "student_exhibitor_user_write_bind" {
  target               = authentik_flow.student_exhibitor_password_setup.uuid
  stage                = authentik_stage_user_write.student_exhibitor_user_write.id
  order                = 15
  evaluate_on_plan     = false
  re_evaluate_policies = true
}

resource "authentik_flow_stage_binding" "student_exhibitor_login_bind" {
  target               = authentik_flow.student_exhibitor_password_setup.uuid
  stage                = authentik_stage_user_login.student_exhibitor_login.id
  order                = 20
  evaluate_on_plan     = false
  re_evaluate_policies = true
}

output "student_exhibitor_email_stage_id" {
  value       = authentik_stage_email.student_exhibitor_email.id
  description = "The UUID of the student exhibitor email stage, used as the email_stage parameter for the recovery email sender script."
  sensitive   = false
}

# === Infisical へのステージID登録について ===
# apply 完了後、出力されたステージIDを Infisical (prod) に登録するためのコマンド例:
#
# # 1. terraform output でステージIDを取得し一時ファイルに書き出す
# echo "STUDENT_EXHIBITOR_EMAIL_STAGE_ID=$(terraform output -raw student_exhibitor_email_stage_id)" > secrets.tmp
#
# # 2. scripts/push-secrets-to-infisical.sh で登録
# ../scripts/push-secrets-to-infisical.sh secrets.tmp > /dev/null
#
# # 3. 一時ファイルを削除
# rm secrets.tmp

# ============================================================
# ログイン中ユーザーによるフロー再入場の拒否ポリシー
# ============================================================
# 既存 deny_enrollment_if_authenticated (terraform/authentik_enrollment.tf) は
# request.user.is_authenticated のみを見る式のため、本フローでは使えない。
# recovery_email API (scripts/send-student-exhibitor-recovery-emails.py) が
# リンク発行時に呼ぶ FlowPlanner.plan() は PLAN_CONTEXT_PENDING_USER (= 操作対象ユーザー)
# を PolicyEngine の評価主体 request.user に使う (authentik/flows/planner.py,
# authentik/policies/engine.py で確認、実機検証で403/400連鎖の末に特定・2026-08-16)。
# 対象ユーザーは実在する内部ユーザーであり is_authenticated は常に True を返すため、
# 既存ポリシーをそのまま適用するとリンク発行そのものが毎回 FlowNonApplicableException で
# 失敗する。事前planning (pending_user がコンテキストに存在する) はスキップし、
# 実際にユーザーがブラウザでリンクを開いた際 (pending_user なし) にのみ
# request.user.is_authenticated を判定する専用ポリシーとして分離する。
resource "authentik_policy_expression" "student_exhibitor_deny_if_authenticated" {
  name       = "student-exhibitor-deny-if-authenticated"
  expression = <<-EOT
if "pending_user" in request.context:
    return True
if request.user.is_authenticated:
    ak_message("既にログイン中のため、このリンクは使用できません。一度ログアウトしてから開いてください。")
    return False
return True
EOT
}

resource "authentik_policy_binding" "student_exhibitor_deny_if_authenticated_bind" {
  target  = authentik_flow.student_exhibitor_password_setup.uuid
  policy  = authentik_policy_expression.student_exhibitor_deny_if_authenticated.id
  order   = 0
  enabled = true
}
