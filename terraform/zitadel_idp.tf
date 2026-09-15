# ============================================================
# Zitadel Discord ソーシャルログイン (単純OAuth2連携のみ、PoC)
# ============================================================
#
# authentik_discord.tf のアバター同期・ロール同期・動的グループ判定(discord_group_sync
# Expression Policy等)は移行対象外 (design.md Non-Goals)。ここではDiscordアカウントで
# 認可コードフローが完走しZitadelセッションが確立することのみを提供する。
#
# client_id/client_secretは既存のdiscord_client_id/discord_client_secret変数を再利用する
# (Discord Developer Portal側のアプリ登録は既存authentik用のものと共用可能。redirect_uri
# のみZitadel用に追加登録が必要)。未設定時のdummy値フォールバックはauthentik_discord.tfと
# 同じパターン。

resource "zitadel_org_idp_oauth" "discord" {
  name = "Discord"

  client_id     = var.discord_client_id != "" ? var.discord_client_id : "dummy_discord_client_id"
  client_secret = var.discord_client_secret != "" ? var.discord_client_secret : "dummy_discord_client_secret"

  authorization_endpoint = "https://discord.com/api/oauth2/authorize"
  token_endpoint         = "https://discord.com/api/oauth2/token"
  user_endpoint          = "https://discord.com/api/users/@me"
  id_attribute           = "id"
  scopes                 = ["identify", "email"]

  # 単純ログインのみ: 既存Zitadelアカウントとのリンクのみ許可し、Discord経由の
  # 自動アカウント作成・属性の自動更新(アバター等)は行わない
  is_creation_allowed = true
  is_linking_allowed  = true
  is_auto_creation    = false
  is_auto_update      = false

  use_pkce = true
}
