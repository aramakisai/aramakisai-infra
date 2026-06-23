# vaultwarden-rbac-sync 専用 Authentik サービスアカウント
#
# グループメンバーシップ取得専用の非対話ユーザー。ログインフロー/イベント駆動トリガー
# (terraform-provider-authentik の authentik_policy_expression / event_rule 等) は
# 別タスクで本ファイルに追記する。
resource "random_password" "vaultwarden_rbac_sync_password" {
  length  = 32
  special = true
}

resource "authentik_user" "vaultwarden_rbac_sync" {
  username = "vaultwarden-rbac-sync"
  name     = "Vaultwarden RBAC Sync Service Account"
  type     = "service_account"
  password = random_password.vaultwarden_rbac_sync_password.result
}

resource "authentik_token" "vaultwarden_rbac_sync" {
  identifier   = "vaultwarden-rbac-sync-api"
  user         = authentik_user.vaultwarden_rbac_sync.id
  intent       = "api"
  expiring     = false
  retrieve_key = true
  description  = "vaultwarden-rbac-sync: Authentikグループメンバーシップ取得用APIトークン"
}
