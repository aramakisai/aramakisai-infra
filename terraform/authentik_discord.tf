# ============================================================
# Discord ソーシャルログイン連携 (アバター & ロール同期)
# ============================================================

# Discord 連携済みユーザーを識別するグループ
# このグループへの所属が他アプリへのアクセス条件になる (require-discord-link-policy)
resource "authentik_group" "discord_linked_users" {
  name = "discord-linked-users"
}

# 1. Discord Property Mapping
# source property mapping では user が未定義のためグループ付与は行わない。
# アバター・ロール情報を attributes に格納し、グループ付与は Expression Policy で行う。
resource "authentik_property_mapping_source_oauth" "discord_sync" {
  name       = "discord-avatar-role-mapping"
  expression = <<-EOT
import base64

ACCEPTED_GUILD_ID = "${var.discord_guild_id}"

avatar_base64 = None
discord_roles = None

if info.get("avatar"):
    avatar_url = f"https://cdn.discordapp.com/avatars/{info.get('id')}/{info.get('avatar')}.png?size=256"
    try:
        response = client.do_request("GET", avatar_url)
        encoded_image = base64.b64encode(response.content).decode('utf-8')
        avatar_base64 = f"data:image/png;base64,{encoded_image}"
    except Exception:
        avatar_base64 = None

if ACCEPTED_GUILD_ID:
    try:
        guild_url = f"https://discord.com/api/v10/users/@me/guilds/{ACCEPTED_GUILD_ID}/member"
        guild_response = client.do_request("GET", guild_url, token=token)
        if guild_response.status_code == 200:
            discord_roles = guild_response.json().get("roles", [])
    except Exception:
        pass

return {
    "attributes.avatar": avatar_base64,
    "attributes.mailAlias": [],
    "attributes.discord_roles": discord_roles,
}
EOT
}

# 2. Discord グループ同期 Expression Policy
# - prompt_data からフレッシュなロール情報を取得 (pending_user.attributes は stale)
# - ログイン時に Discord ロールと Authentik グループを差分同期
# - transaction.on_commit() でポストフロー上書きを回避して attributes を永続化
resource "authentik_policy_expression" "discord_group_sync" {
  name       = "discord-group-sync-policy"
  expression = <<-EOT
from authentik.core.models import Group, User as AkUser
from django.db import transaction

pending_user = request.context.get("pending_user")
if pending_user is None:
    return True

# プロパティマッピング結果は prompt_data に格納される (pending_user.attributes は DB の stale 値)
prompt_data = request.context.get("prompt_data", {})
discord_roles = prompt_data.get("attributes.discord_roles")
avatar = prompt_data.get("attributes.avatar")

# フォールバック: enrollment フローで UserWriteStage が保存済みの場合
if discord_roles is None:
    discord_roles = pending_user.attributes.get("discord_roles")
if avatar is None:
    avatar = pending_user.attributes.get("avatar")

# None = Guild API 失敗 → 既存グループを保持したままスキップ
if not isinstance(discord_roles, list):
    return True

# Discord ロールに対応するグループの PK セットを算出
new_pks = set()

try:
    new_pks.add(Group.objects.get(name="discord-linked-users").pk)
except Group.DoesNotExist:
    pass

for g in Group.objects.filter(attributes__discord_role_id__in=discord_roles):
    new_pks.add(g.pk)

for g in Group.objects.filter(attributes__discord_role_ids__isnull=False):
    if any(r in g.attributes.get("discord_role_ids", []) for r in discord_roles):
        new_pks.add(g.pk)

# Discord 管理グループ (discord_role_id / discord_role_ids 属性 + discord-linked-users)
managed_pks = set(
    list(Group.objects.filter(attributes__discord_role_id__isnull=False).values_list('pk', flat=True)) +
    list(Group.objects.filter(attributes__discord_role_ids__isnull=False).values_list('pk', flat=True))
)
try:
    managed_pks.add(Group.objects.get(name="discord-linked-users").pk)
except Group.DoesNotExist:
    pass

current_pks = set(pending_user.ak_groups.filter(pk__in=managed_pks).values_list('pk', flat=True))

for pk in current_pks - new_pks:
    pending_user.ak_groups.remove(Group.objects.get(pk=pk))

for pk in new_pks - current_pks:
    pending_user.ak_groups.add(Group.objects.get(pk=pk))

# フロー完了後に discord_roles と avatar を DB に永続化
user_pk = pending_user.pk
_discord_roles = discord_roles
_avatar = avatar

def _save_attrs():
    u = AkUser.objects.get(pk=user_pk)
    attrs = dict(u.attributes or {})
    attrs["discord_roles"] = _discord_roles
    if _avatar:
        attrs["avatar"] = _avatar
    AkUser.objects.filter(pk=user_pk).update(attributes=attrs)

transaction.on_commit(_save_attrs)

return True
EOT
}

# 3. デフォルトフローの UserLoginStage binding にポリシーをバインド
# UserWriteStage は username/email を上書きするため auth フローには追加しない。
# UUIDs はデフォルトフローのため固定値:
#   550f392f-... = default-source-authentication の UserLoginStage binding
#   2ab74f9c-... = default-source-enrollment の UserLoginStage binding
resource "authentik_policy_binding" "discord_group_sync_auth_policy" {
  target = "550f392f-713e-427d-8990-0a36657808a5"
  policy = authentik_policy_expression.discord_group_sync.id
  order  = 0
}

resource "authentik_policy_binding" "discord_group_sync_enroll_policy" {
  target = "2ab74f9c-9e9d-46b1-bc27-406035454017"
  policy = authentik_policy_expression.discord_group_sync.id
  order  = 0
}

# 4. Discord OAuth2 Source
resource "authentik_source_oauth" "discord" {
  name          = "Discord"
  slug          = "discord"
  provider_type = "discord"

  consumer_key    = var.discord_client_id != "" ? var.discord_client_id : "dummy_discord_client_id"
  consumer_secret = var.discord_client_secret != "" ? var.discord_client_secret : "dummy_discord_client_secret"

  authentication_flow = data.authentik_flow.default_source_authentication.id
  enrollment_flow     = data.authentik_flow.default_source_enrollment.id

  additional_scopes = "guilds guilds.members.read"

  property_mappings = [
    authentik_property_mapping_source_oauth.discord_sync.id
  ]

  user_matching_mode = "email_link"
}
