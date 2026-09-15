# ============================================================
# Zitadel Project / Role (フラットロール、PoC)
# ============================================================
#
# authentikのrbac_role/permission_role相当の細粒度権限管理(view_group等)は移行対象外。
# キー・表示名のみのフラットロールとして定義し、project_role_assertionで
# OIDC ID Token/UserinfoクレームへProject Roleを配布する (task 2.1)。
#
# ロール一覧は既存authentik_vaultwarden_rbac_sync.tf / authentik_discord.tfの
# グループ構成 (部門6グループ + executive + admin + leader) と対応させ、
# 移行時にRP側のgroups/rolesクレーム処理を再利用しやすくする。

resource "zitadel_project" "aramakisai" {
  name = "aramakisai"

  # OIDC ID Token/Userinfoクレームへロールを配布する (Requirement 2.2, 8.1, 8.2)
  project_role_assertion = true

  # ロール未付与ユーザーのログイン自体は妨げない(authentikの現行挙動を踏襲)
  project_role_check = false
  has_project_check  = false
}

locals {
  # role_key => display_name (フラット、permission行列なし)
  aramakisai_project_roles = {
    admin           = "管理者"
    executive       = "実行委員"
    leader          = "リーダー"
    planning        = "企画"
    accounting      = "会計"
    vendors         = "出店"
    performers      = "出演"
    pr              = "広報"
    general_affairs = "総務"
  }
}

resource "zitadel_project_role" "aramakisai" {
  for_each = local.aramakisai_project_roles

  # zitadel_projectと異なりzitadel_project_roleはorg_idが必須引数 (provider実装上の非対称)
  org_id       = var.zitadel_org_id
  project_id   = zitadel_project.aramakisai.id
  role_key     = each.key
  display_name = each.value
}
