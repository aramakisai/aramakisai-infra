# ============================================================
# Authentik ブランド (Brand) 設定
# ============================================================
# Brand が1件も存在しない...という状態は実際には存在せず、Authentikブループリントの
# 初期セットアップ時に自動生成される非表示のデフォルトBrand (domain=authentik-default)
# が既に存在していた (管理画面のブランド一覧には表示されない、`terraform apply` 実行時の
# "Only a single brand can be set as default" エラーで発覚。API `/api/v3/core/brands/` で
# 直接確認・import した)。
#
# この既存Brandの flow_recovery が未設定 (null) のため、Admin API `recovery_email` が
# どのフローにも遷移できず 400 "No recovery flow set." を返す
# (authentik/core/api/users.py `_create_recovery_link` 実装、2026.5.6で確認)。
# ログイン画面にパスワード忘れ導線が存在せず既存 password-recovery フローは実質未使用のため、
# flow_recovery を学生団体パスワード設定フローに割り当てても実害はない。
#
# 他の既存フィールド (flow_authentication 等) は既存の稼働中フローを維持するため、
# apply直前にAPIから取得した実値をそのまま明示指定している。
import {
  to = authentik_brand.main
  id = "0edfe1da-6fe3-4ac2-8e2a-4278a68ba87a"
}

resource "authentik_brand" "main" {
  domain  = "authentik-default"
  default = true

  branding_title                   = "authentik"
  branding_logo                    = "/static/dist/assets/icons/icon_left_brand.svg"
  branding_favicon                 = "/static/dist/assets/icons/icon.png"
  branding_default_flow_background = "/static/dist/assets/images/flow_background.jpg"

  flow_authentication = "f2c9f2c6-1d72-4741-a2a0-383e8c6eca7c" # gitleaks:allow
  flow_invalidation   = "32451bb4-f3d6-4d5e-975b-b73e6cc190ae"
  flow_user_settings  = "8d25eaef-821a-4de0-b59e-39f9ef13ad8a"

  flow_recovery = authentik_flow.student_exhibitor_password_setup.uuid
}
