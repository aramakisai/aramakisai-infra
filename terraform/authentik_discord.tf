# ============================================================
# Discord ソーシャルログイン連携 (アバター & ロール同期)
# ============================================================

# Discord 連携済みユーザーを識別するグループ
# このグループへの所属が他アプリへのアクセス条件になる (require-discord-link-policy)
resource "authentik_group" "discord_linked_users" {
  name = "discord-linked-users"
}

# 1. Discord Property Mapping (アバターおよび所属ロール/グループ同期)
resource "authentik_property_mapping_source_oauth" "discord_sync" {
  name       = "discord-avatar-role-mapping"
  expression = <<-EOT
import base64
from authentik.core.models import Group

ACCEPTED_GUILD_ID = "${var.discord_guild_id}"
AVATAR_SIZE = "64"

avatar_base64 = None

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
ml_groups = []

# Discord サーバーの所属ロールを検証して Authentik グループに同期
if ACCEPTED_GUILD_ID:
    try:
        guild_url = f"https://discord.com/api/v10/users/@me/guilds/{ACCEPTED_GUILD_ID}/member"
        guild_response = client.do_request("GET", guild_url, token=token)
        discord_roles = guild_response.json().get("roles", [])

        # Guild API 呼び出し成功 = Discord 連携済みとみなして連携済みグループを付与
        user_groups.append("discord-linked-users")

        # attributes.discord_role_id (単数形) に Discord ロールIDを持つグループを取得
        matched_groups = Group.objects.filter(attributes__discord_role_id__in=discord_roles)
        user_groups.extend([g.name for g in matched_groups])

        # attributes.discord_role_ids (複数形) を持つメーリングリストグループを動的スキャン
        # MLグループには {"mail": ["list@..."], "discord_role_ids": ["ID1", "ID2"]} を設定する
        for ml_group in Group.objects.filter(attributes__discord_role_ids__isnull=False):
            role_ids = ml_group.attributes.get("discord_role_ids", [])
            if any(r in role_ids for r in discord_roles):
                ml_groups.append(ml_group.name)

    except Exception:
        pass

return {
    "attributes.avatar": avatar_base64,
    "attributes.mailAlias": [],
    "groups": user_groups + list(set(ml_groups)),
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
