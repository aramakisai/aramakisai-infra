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
  # service_account はメール未設定だとログインしてAPIキー取得等ができないため必須
  email = "admin@aramakisai.com"
}

resource "authentik_token" "vaultwarden_rbac_sync" {
  identifier   = "vaultwarden-rbac-sync-api"
  user         = authentik_user.vaultwarden_rbac_sync.id
  intent       = "api"
  expiring     = false
  retrieve_key = true
  description  = "vaultwarden-rbac-sync: Authentikグループメンバーシップ取得用APIトークン"
}

# === イベント駆動トリガー: ログイン時即時同期 (task 9.1) ===
#
# discord_group_sync (authentik_discord.tf) と同じ authentik_policy_expression パターンを再利用し、
# ログイン成功時にTrigger Receiverへ非同期でPOSTする。`requests` はBaseEvaluatorの全Expression Policyで
# 標準で利用可能なグローバル (authentik/lib/expression/evaluator.py `get_http_session()`) であり、
# discord_group_syncが使う `client.do_request()`（OAuth Source専用コンテキストにのみ存在）とは異なり、
# 直接ログイン経路でも利用できる。失敗時は例外を握り潰しログイン処理自体は継続させる。
resource "authentik_policy_expression" "vaultwarden_rbac_sync_login_trigger" {
  name       = "vaultwarden-rbac-sync-login-trigger-policy"
  expression = <<-EOT
try:
    requests.post(
        "http://vaultwarden-rbac-sync.prod.svc.cluster.local/trigger",
        headers={"Authorization": "Bearer ${var.vaultwarden_rbac_sync_trigger_token}"},
        timeout=3,
    )
except Exception:
    pass

return True
EOT
}

# バインド対象のUserLoginStage binding pk (実環境API `/api/v3/flows/bindings/?target=<flow_pk>` で確認済み、2026-06-25):
#   12ed7fd6-c51d-4aa6-ba7b-c03bfd381009 = default-authentication-flow（直接ログイン）の UserLoginStage binding
#   550f392f-713e-427d-8990-0a36657808a5 = default-source-authentication の UserLoginStage binding（discord_group_syncと共用対象）
#   2ab74f9c-9e9d-46b1-bc27-406035454017 = default-source-enrollment の UserLoginStage binding（discord_group_syncと共用対象）
# 3経路全てにバインドし、直接ログイン・外部Source経由ログインのいずれでも即時トリガーされることを保証する。
resource "authentik_policy_binding" "vaultwarden_rbac_sync_login_trigger_direct" {
  target = "12ed7fd6-c51d-4aa6-ba7b-c03bfd381009"
  policy = authentik_policy_expression.vaultwarden_rbac_sync_login_trigger.id
  order  = 1
}

resource "authentik_policy_binding" "vaultwarden_rbac_sync_login_trigger_source_auth" {
  target = "550f392f-713e-427d-8990-0a36657808a5"
  policy = authentik_policy_expression.vaultwarden_rbac_sync_login_trigger.id
  order  = 1
}

resource "authentik_policy_binding" "vaultwarden_rbac_sync_login_trigger_source_enroll" {
  target = "2ab74f9c-9e9d-46b1-bc27-406035454017"
  policy = authentik_policy_expression.vaultwarden_rbac_sync_login_trigger.id
  order  = 1
}

# === イベント駆動トリガー: 管理者操作によるグループ変更即時通知 (task 9.2) ===
#
# Notification Webhook機構 (authentik_event_transport + authentik_policy_event_matcher +
# authentik_event_rule) で、管理者がAdmin UI/APIでAuthentikグループを変更した際にTrigger Receiverへ
# 即時POSTする。
resource "authentik_property_mapping_notification" "vaultwarden_rbac_sync_trigger_headers" {
  name       = "vaultwarden-rbac-sync-trigger-headers-mapping"
  expression = <<-EOT
return {"Authorization": "Bearer ${var.vaultwarden_rbac_sync_trigger_token}"}
EOT
}

resource "authentik_event_transport" "vaultwarden_rbac_sync_trigger" {
  name                    = "vaultwarden-rbac-sync-trigger-webhook"
  mode                    = "webhook"
  webhook_url             = "http://vaultwarden-rbac-sync.prod.svc.cluster.local/trigger"
  webhook_mapping_headers = authentik_property_mapping_notification.vaultwarden_rbac_sync_trigger_headers.id
}

# action値は実環境のEvents API (`/api/v3/events/events/?action=model_updated`) で実例確認済み
# (2026-06-25: Groupモデル変更がaction=model_updatedで記録される)。
# app/modelの正確な値は `terraform providers schema -json` のenum一覧から確認 (Django ContentTypeの
# app_label形式とは異なり、appはAppConfig.name形式 "authentik.core"、modelは "authentik_core.group" 形式)。
resource "authentik_policy_event_matcher" "vaultwarden_rbac_sync_group_change" {
  name   = "vaultwarden-rbac-sync-group-change-matcher"
  action = "model_updated"
  app    = "authentik.core"
  model  = "authentik_core.group"
}

resource "authentik_event_rule" "vaultwarden_rbac_sync_group_change" {
  name       = "vaultwarden-rbac-sync-group-change-rule"
  transports = [authentik_event_transport.vaultwarden_rbac_sync_trigger.id]
  # destination_group/destination_event_userのいずれも未設定だとNotificationRule.destination_users()が
  # 空になりtransport.send()が一切呼ばれない（authentik source: authentik/events/models.py
  # NotificationRule）。グループ変更操作を行った管理者自身を宛先にすることで確実にWebhookを発火させる。
  destination_event_user = true
  # 既知のproviderバグ回避（terraform-provider-authentik 2026.2.0、実機確認2026-06-25）:
  # destination_groupを未設定(null)のままにすると、レスポンスのdestination_group_objがnullになり、
  # providerのGoクライアントがネストオブジェクトのデコードに失敗し
  # `HTTP Error 'no value given for required property pk'` でcreate/read双方が失敗する。
  # destination_groupに実在のGroupを指定するとdestination_group_objが非nullになり回避できるため、
  # 既存の管理者グループを宛先に設定する（管理者への可視化という副次的利点もある）。
  destination_group = authentik_group.admin.id
}

resource "authentik_policy_binding" "vaultwarden_rbac_sync_group_change_binding" {
  target = authentik_event_rule.vaultwarden_rbac_sync_group_change.id
  policy = authentik_policy_event_matcher.vaultwarden_rbac_sync_group_change.id
  order  = 0
}
