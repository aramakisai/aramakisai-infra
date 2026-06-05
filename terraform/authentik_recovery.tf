# ============================================================
# パスワードリカバリーフロー定義
# ============================================================

# リカバリー用のフロー自体
resource "authentik_flow" "recovery" {
  name        = "Password Recovery Flow"
  slug        = "password-recovery"
  title       = "Password Recovery"
  designation = "recovery"
}

# Eメール送信ステージ (Email Stage)
resource "authentik_stage_email" "recovery_email" {
  name = "recovery-email-stage"

  use_global_settings = true # Authentik全体で構成されているSMTPサーバー設定を使用
  subject             = "Password Reset Request"
  template            = "email/password_reset.html"
  token_expiry        = null # プロバイダーのスキーマ(数値)とAPI(文字列)の型ミスマッチのバグを回避するため、nullを指定して除外します
}

# パスワードリセットステージ (Password Stage)
resource "authentik_stage_password" "recovery_password" {
  name = "recovery-password-stage"

  backends = [
    "authentik.core.auth.InbuiltBackend"
  ]
}

# フローとステージのバインディング
resource "authentik_flow_stage_binding" "recovery_email_bind" {
  target = authentik_flow.recovery.uuid # target には UUID が必要なため uuid を使用
  stage  = authentik_stage_email.recovery_email.id
  order  = 10
}

# パスワードリセットステージのバインディング
resource "authentik_flow_stage_binding" "recovery_password_bind" {
  target = authentik_flow.recovery.uuid # target には UUID が必要なため uuid を使用
  stage  = authentik_stage_password.recovery_password.id
  order  = 20
}
