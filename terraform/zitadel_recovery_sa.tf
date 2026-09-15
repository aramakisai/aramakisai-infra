# ============================================================
# 招待発行用(旧authentik_student_exhibitor_recovery_sa.tf後継) Zitadel machine user
# ============================================================
#
# 移行元authentik_student_exhibitor_recovery_sa.tfは view_user/change_user/
# reset_user_password/view_emailstage/add_user の5権限を独自rbac_roleで細粒度に
# 組み合わせていた(Non-Goals: authentik相当の細粒度permission管理は再現しない)。
# Zitadelには同等の細粒度permission機能がないが、組み込みのorg-scoped managerロール
# `ORG_USER_MANAGER`(ユーザーの作成・閲覧・更新・パスワードリセット・招待コード発行を
# 包含)がinstance全体の管理権限(IAM_OWNER)なしで要件を満たすため、この1件のみ
# 例外的に移行対象とする(design.md Non-Goals, Requirement 14)。
resource "zitadel_machine_user" "recovery_sa" {
  org_id      = var.zitadel_org_id
  user_name   = "invite-recovery-sa"
  name        = "Invite Recovery Service Account"
  description = "招待発行用SA: ユーザー作成・閲覧・更新・パスワードリセット・招待コード発行 (ORG_USER_MANAGERのみ、IAM_OWNER不要)"
  with_secret = false
}

resource "zitadel_org_member" "recovery_sa" {
  org_id  = var.zitadel_org_id
  user_id = zitadel_machine_user.recovery_sa.id
  roles   = ["ORG_USER_MANAGER"]
}

resource "zitadel_personal_access_token" "recovery_sa" {
  org_id          = var.zitadel_org_id
  user_id         = zitadel_machine_user.recovery_sa.id
  expiration_date = "2029-01-01T00:00:00Z"
}

# 発行したPATはInfisicalへ手動登録する想定(本番カットオーバー時に実施、PoCではk3d実機検証のみ)。
# infisical-auth/dovecot_lua_authと同型の例外ブートストラップのため、terraform outputには含めない。
