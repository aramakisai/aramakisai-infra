# student-exhibitor-recovery 用 Authentik サービスアカウント + トークン定義

resource "random_password" "student_exhibitor_recovery_sa_password" {
  length  = 32
  special = true
}

resource "authentik_rbac_role" "student_exhibitor_recovery" {
  name = "student-exhibitor-recovery-role"
}

# 必要な権限 (実機検証済み, 2026-08-15):
# view_user: student_exhibitor グループ所属ユーザーの参照・一覧取得
# change_user: 対象ユーザーの属性更新（送信済みタイムスタンプ記録用）
# reset_user_password: recovery_email API (パスワード設定リンク発行) の認可に必須
resource "authentik_rbac_permission_role" "student_exhibitor_recovery_view_user" {
  role       = authentik_rbac_role.student_exhibitor_recovery.id
  permission = "authentik_core.view_user"
}

resource "authentik_rbac_permission_role" "student_exhibitor_recovery_change_user" {
  role       = authentik_rbac_role.student_exhibitor_recovery.id
  permission = "authentik_core.change_user"
}

# recovery_email API (パスワード設定リンク発行) の呼び出しには change_user では不十分で、
# 専用の reset_user_password 権限が必要 (実機検証で403を確認、2026-08-15)。
resource "authentik_rbac_permission_role" "student_exhibitor_recovery_reset_password" {
  role       = authentik_rbac_role.student_exhibitor_recovery.id
  permission = "authentik_core.reset_user_password"
}

# recovery_email API は対象ユーザーへの reset_user_password 権限に加え、リンク発行元の
# EmailStage オブジェクトへの view_emailstage 権限も内部でチェックする
# (authentik/core/api/users.py recovery_email 実装。reset_user_password 単体では403)。
resource "authentik_rbac_permission_role" "student_exhibitor_recovery_view_emailstage" {
  role       = authentik_rbac_role.student_exhibitor_recovery.id
  permission = "authentik_stages_email.view_emailstage"
}

# recovery_email / recovery アクションには authentik/core/api/users.py 側で
# permission_classes の明示オーバーライドがなく(set_password 等にはある)、DRFの
# DEFAULT_PERMISSION_CLASSES (ObjectPermissions) が素通しで効く。ObjectPermissions は
# DjangoObjectPermissions を継承しており POST メソッドには暗黙に add_user 権限を要求する
# ため、reset_user_password だけでは self.get_object() の時点で403になる
# (upstream の実装不整合、実機検証で確認済み2026-08-16)。
resource "authentik_rbac_permission_role" "student_exhibitor_recovery_add_user" {
  role       = authentik_rbac_role.student_exhibitor_recovery.id
  permission = "authentik_core.add_user"
}

resource "authentik_user" "student_exhibitor_recovery_sa" {
  username = "student-exhibitor-recovery-sa"
  name     = "Student Exhibitor Recovery Service Account"
  type     = "service_account"
  password = random_password.student_exhibitor_recovery_sa_password.result
  roles    = [authentik_rbac_role.student_exhibitor_recovery.id]
  # service_account はメール未設定だとログインしてAPIキー取得等ができないため必須
  email = "admin@aramakisai.com"
}

resource "authentik_token" "student_exhibitor_recovery_api" {
  identifier   = "student-exhibitor-recovery-api"
  user         = authentik_user.student_exhibitor_recovery_sa.id
  intent       = "api"
  expiring     = false
  retrieve_key = true
  description  = "student-exhibitor-recovery: Authentik APIアクセス用トークン"
}

output "student_exhibitor_recovery_api_token" {
  value       = authentik_token.student_exhibitor_recovery_api.key
  description = "The API token for the student-exhibitor-recovery service account."
  sensitive   = true
}

# === Infisical へのトークン登録について ===
# apply 完了後、出力されたトークンを Infisical (prod) に登録するためのコマンド例:
#
# # 1. terraform output でトークンを取得し一時ファイルに書き出す
# echo "STUDENT_EXHIBITOR_RECOVERY_AUTHENTIK_TOKEN=$(terraform output -raw student_exhibitor_recovery_api_token)" > secrets.tmp
#
# # 2. scripts/push-secrets-to-infisical.sh で登録 (標準出力を /dev/null にリダイレクトし、平文表示を防ぐ)
# ../scripts/push-secrets-to-infisical.sh secrets.tmp > /dev/null
#
# # 3. 一時ファイルを削除
# rm secrets.tmp
