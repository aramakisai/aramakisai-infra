# vaultwarden-rbac-sync 専用 Authentik サービスアカウント + グループ定義
#
# グループメンバーシップ取得専用の非対話ユーザー。ログインフロー/イベント駆動トリガー
# (terraform-provider-authentik の authentik_policy_expression / event_rule 等) は
# 別タスクで本ファイルに追記する。
#
# mapping-configmap.yaml で参照される全Authentikグループを定義。
# "管理者" は既存手動グループ → terraform import 要。

# === 部門グループ (既存 → import) ===
import {
  to = authentik_group.planning
  id = "00486db1-59c6-4ab8-beec-cdd45caadcb1"
}
import {
  to = authentik_group.accounting
  id = "c5d9866c-550b-4db3-88e2-bfc03a9c1378"
}
import {
  to = authentik_group.vendors
  id = "273f83b3-3ebc-4d9e-a9ac-3e85894dda57"
}
import {
  to = authentik_group.performers
  id = "cdb6a105-6e16-472a-8d34-95b1a0ca9bd3"
}
import {
  to = authentik_group.pr
  id = "d2382993-61c7-47d2-ac1d-55864c757e46"
}
import {
  to = authentik_group.general_affairs
  id = "f53fff56-9948-49a5-85a3-fb7cf8412b8c"
}

resource "authentik_group" "planning" {
  name = "企画"
  lifecycle { ignore_changes = [attributes] }
}
resource "authentik_group" "accounting" {
  name = "会計"
  lifecycle { ignore_changes = [attributes] }
}
resource "authentik_group" "vendors" {
  name = "出店"
  lifecycle { ignore_changes = [attributes] }
}
resource "authentik_group" "performers" {
  name = "出演"
  lifecycle { ignore_changes = [attributes] }
}
resource "authentik_group" "pr" {
  name = "広報"
  lifecycle { ignore_changes = [attributes] }
}
resource "authentik_group" "general_affairs" {
  name = "総務"
  lifecycle { ignore_changes = [attributes] }
}

# === 権限グループ ===
import {
  to = authentik_group.admin
  id = "e3ccdce4-f130-4bac-8932-70a963554d4a"
}

resource "authentik_group" "admin" {
  name         = "管理者"
  is_superuser = true
  lifecycle { ignore_changes = [attributes] }
}

resource "authentik_group" "leader" {
  name = "リーダー"
  attributes = jsonencode({
    discord_id  = "1437996367896252436"
    mailAclSlug = "leader"
  })
}

# === 資格情報専用グループ ===
resource "authentik_group" "google_account" {
  name = "Googleアカウント"
}
resource "random_password" "vaultwarden_rbac_sync_password" {
  length  = 32
  special = true
}

# サービスアカウントにグループ一覧取得権限を付与
# (tasks.md task 1.5: 管理用APIトークンで確認済み、view_group権限がないと403)
resource "authentik_rbac_role" "vaultwarden_rbac_sync_group_reader" {
  name = "vaultwarden-rbac-sync-group-reader"
}

resource "authentik_rbac_permission_role" "vaultwarden_rbac_sync_group_reader_perm" {
  role       = authentik_rbac_role.vaultwarden_rbac_sync_group_reader.id
  permission = "authentik_core.view_group"
}

resource "authentik_user" "vaultwarden_rbac_sync" {
  username = "vaultwarden-rbac-sync"
  name     = "Vaultwarden RBAC Sync Service Account"
  type     = "service_account"
  password = random_password.vaultwarden_rbac_sync_password.result
  roles    = [authentik_rbac_role.vaultwarden_rbac_sync_group_reader.id]
}

resource "authentik_token" "vaultwarden_rbac_sync" {
  identifier   = "vaultwarden-rbac-sync-api"
  user         = authentik_user.vaultwarden_rbac_sync.id
  intent       = "api"
  expiring     = false
  retrieve_key = true
  description  = "vaultwarden-rbac-sync: Authentikグループメンバーシップ取得用APIトークン"
}
