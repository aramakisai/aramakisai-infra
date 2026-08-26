# ============================================================
# Authentik Expression Policies & Application Bindings
# ============================================================

# Discord 連携を必須とするポリシー
# 未連携ユーザーがアプリにアクセスしようとした際にブロックし、
# 連携手順を案内するメッセージを表示する
resource "authentik_policy_expression" "require_discord_link" {
  name       = "require-discord-link-policy"
  expression = <<-EOT
is_linked = ak_is_group_member(request.user, name="executive")

if not is_linked:
    ak_message("このアプリケーションを利用するにはDiscord連携が必要です。画面右上の歯車マーク（設定）から「Connected Services」を開き、Discordを連携してください。")
    return False

return True
EOT
}

# Roundcube (Webmail) へのポリシーバインド
resource "authentik_policy_binding" "roundcube_require_discord" {
  target = authentik_application.roundcube.uuid
  policy = authentik_policy_expression.require_discord_link.id
  order  = 10
}

# ArgoCD へのポリシーバインド
resource "authentik_policy_binding" "argocd_require_discord" {
  target = authentik_application.argocd.uuid
  policy = authentik_policy_expression.require_discord_link.id
  order  = 10
}

# Cloudflare Access (staging 保護) へのポリシーバインド
resource "authentik_policy_binding" "cloudflare_require_discord" {
  target = authentik_application.cloudflare.uuid
  policy = authentik_policy_expression.require_discord_link.id
  order  = 10
}

# Vaultwarden へのポリシーバインド
resource "authentik_policy_binding" "vaultwarden_require_discord" {
  target = authentik_application.vaultwarden.uuid
  policy = authentik_policy_expression.require_discord_link.id
  order  = 10
}

# Room Presence Tracker へのポリシーバインド
resource "authentik_policy_binding" "room_presence_require_discord" {
  target = authentik_application.room_presence.uuid
  policy = authentik_policy_expression.require_discord_link.id
  order  = 10
}

# Directus (prod) へのポリシーバインド
resource "authentik_policy_binding" "directus_prod_require_discord" {
  target = authentik_application.directus_prod.uuid
  policy = authentik_policy_expression.require_discord_link.id
  order  = 10
}

# Directus (stg) へのポリシーバインド
resource "authentik_policy_binding" "directus_stg_require_discord" {
  target = authentik_application.directus_stg.uuid
  policy = authentik_policy_expression.require_discord_link.id
  order  = 10
}

# student_exhibitor グループは実行委員(executive)ではなくDiscord連携を持たないため、
# 上記require_discordポリシーだけではDirectus (prod)に一切ログインできない
# (実機検証で発覚、2026-08-26)。ANY判定(デフォルト)のためこのグループ直接許可を
# 追加すればrequire_discordを満たさなくてもアクセス可能になる。
resource "authentik_policy_binding" "directus_prod_allow_student_exhibitor" {
  target = authentik_application.directus_prod.uuid
  group  = authentik_group.student_exhibitor.id
  order  = 5
}
