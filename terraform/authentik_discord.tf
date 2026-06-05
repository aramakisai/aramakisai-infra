# ============================================================
# Discord ソーシャルログイン連携 (アバター & ロール同期)
# ============================================================

# 1. Discord Property Mapping (アバターおよび所属ロール/グループ同期)
resource "authentik_property_mapping_source_oauth" "discord_sync" {
  name       = "discord-avatar-role-mapping"
  expression = <<-EOT
import base64
from authentik.core.models import Group

ACCEPTED_GUILD_ID = "${var.discord_guild_id}"
AVATAR_SIZE = "64"

avatar_base64 = None
avatar_url = None

# Discord アバター画像の取得・Base64化
if info.get("avatar"):
    avatar_url = f"https://cdn.discordapp.com/avatars/{info.get('id')}/{info.get('avatar')}.png?size={AVATAR_SIZE}"
    try:
        response = client.do_request("GET", avatar_url)
        encoded_image = base64.b64encode(response.content).decode('utf-8')
        avatar_base64 = f"data:image/png;base64,{encoded_image}"
    except Exception:
        avatar_base64 = None

user_groups = []
# Discord サーバーの所属ロールを検証して Authentik グループに同期
if ACCEPTED_GUILD_ID:
    try:
        guild_url = f"https://discord.com/api/v10/users/@me/guilds/{ACCEPTED_GUILD_ID}/member"
        guild_response = client.do_request("GET", guild_url, token=token)
        guild_data = guild_response.json()
        discord_roles = guild_data.get("roles", [])

        # attributes.discord_role_id に Discord ロールIDを持つグループを取得
        matched_groups = Group.objects.filter(attributes__discord_role_id__in=discord_roles)
        user_groups = [group.name for group in matched_groups]
    except Exception:
        pass

return {
    "attributes.avatar": avatar_base64,
    "attributes.mailAlias": [],
    "groups": user_groups
}
EOT
}

# 2. Discord OAuth2 Source
resource "authentik_source_oauth" "discord" {
  name          = "Discord"
  slug          = "discord"
  provider_type = "discord"

  consumer_key    = var.discord_client_id != "" ? var.discord_client_id : "dummy_discord_client_id"
  consumer_secret = var.discord_client_secret != "" ? var.discord_client_secret : "dummy_discord_client_secret"

  authentication_flow = data.authentik_flow.default_source_authentication.id
  enrollment_flow     = data.authentik_flow.default_source_enrollment.id

  # ギルド情報およびギルドメンバー情報を取得するためのスコープを追加
  additional_scopes = "guilds guilds.members.read"

  property_mappings = [
    authentik_property_mapping_source_oauth.discord_sync.id
  ]

  user_matching_mode = "email_link"
}
