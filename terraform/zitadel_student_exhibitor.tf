# ============================================================
# Zitadel 出展団体アカウント (CSV駆動一括作成 + 招待コード発行, PoC)
# ============================================================
#
# 移行元: authentik_student_exhibitor_provisioning.tf(CSV駆動ユーザー作成) +
# authentik_student_exhibitor_flow.tf(招待メールのパスワード設定リンク→パスワード入力→
# 自動ログインの専用flow)。Zitadelでは招待コード(user/v2 API CreateInviteCode →
# VerifyInviteCode → SetPassword)が「パスワード未設定ユーザーへの初回認証手段発行」を
# 標準機能として持つため、authentik側の専用flow/stage一式(email/prompt/user_write/
# user_loginステージ)を再実装する必要はない(task6.1の招待移行と同一方針)。
#
# ロール/グループ: Zitadelにはauthentik相当の任意グループ機能がなく、本移行では
# project roleがグループの役割を代替する(terraform/zitadel_projects.tfの部門ロールと
# 同一パターン)。出展団体は実行委員会の部門ロールといずれとも異なる主体のため、
# 専用role_key "exhibitor" をaramakisaiプロジェクトへ追加する。
#
# 招待コード発行がTerraformネイティブでない理由:
# zitadel_human_userリソース(terraform-provider-zitadel v3系、user/v2 API)は
# initial_password / initial_hashed_password (作成時点で確定するパスワードを直接設定)
# のみを持ち、CreateInviteCode相当の属性・別リソースは存在しない
# (https://github.com/zitadel/terraform-provider-zitadel の docs/resources 配下に
# invite_code系リソースなし、2026-09-01確認)。`external` data sourceはterraform
# plan/apply のたびに複数回呼ばれうる読み取り専用の用途を想定しているが、
# CreateInviteCodeは呼ぶたびに旧コードを即時無効化する非冪等API(task6.1実機確認済み、
# docs/zitadel-invite-migration-runbook.md参照)であり相性が悪い。よって
# null_resource + provisioner "local-exec"(ユーザー作成時のみ1回発火、main.tfの
# 既存コメントアウト済みansible_bootstrapパターンを踏襲)で招待コードを発行する。
# triggersをuser_id(作成後は不変)に固定しているため、対象ユーザーが再作成されない
# 限りterraform apply再実行のたびに招待コードが再発行されることはない。

locals {
  zitadel_student_exhibitors = {
    for row in csvdecode(file("${path.module}/data/zitadel_student_exhibitors.csv")) : row.email => row
  }
}

resource "zitadel_project_role" "student_exhibitor" {
  org_id       = var.zitadel_org_id
  project_id   = zitadel_project.aramakisai.id
  role_key     = "exhibitor"
  display_name = "出展団体"
}

# zitadel_human_userはfirst_name/last_nameが必須(個人向けスキーマ)だが、出展団体は
# 団体単位のアカウントであるため、CSVのteam_nameをfirst_nameへ、団体アカウントである
# ことを示す固定値をlast_nameへ割り当てる(authentik_userのname=team_nameと同じ意図)。
resource "zitadel_human_user" "student_exhibitor" {
  for_each = local.zitadel_student_exhibitors

  org_id     = var.zitadel_org_id
  user_name  = each.key
  first_name = each.value.team_name
  last_name  = "出展団体"
  email      = each.key
  # 招待コード(VerifyInviteCode)を初回認証手段の起点とするため、招待コード発行対象の
  # 既知メールとしてisVerified=trueで作成する(task6.1 zitadel-invite-migration.pyと同一方針)。
  is_email_verified = true
  # 初回パスワードは設定しない(Requirement 13.1: 招待コードパターンで初回パスワード設定)。
}

resource "zitadel_user_grant" "student_exhibitor" {
  for_each = local.zitadel_student_exhibitors

  org_id     = var.zitadel_org_id
  project_id = zitadel_project.aramakisai.id
  user_id    = zitadel_human_user.student_exhibitor[each.key].id
  role_keys  = [zitadel_project_role.student_exhibitor.role_key]
}

resource "null_resource" "student_exhibitor_invite" {
  for_each = local.zitadel_student_exhibitors

  triggers = {
    user_id = zitadel_human_user.student_exhibitor[each.key].id
  }

  provisioner "local-exec" {
    # 機密情報(PAT)はenvironmentブロックで渡す(コマンド文字列への直接展開は
    # Terraformがsensitive値として拒否する。main.tfのansible_bootstrapパターンと同一)。
    environment = {
      ZITADEL_TOKEN = var.zitadel_token
    }

    # returnCode: k3d検証環境はSMTP未設定のため、招待コードをAPIレスポンスで直接受け取る
    # (task6.1 zitadel-invite-migration.pyと同一方針)。本番SMTP設定後はsendCodeへの
    # 切替を別タスク(本番カットオーバー)で検討する。
    command = <<-EOT
      set -e
      curl -sf -X POST \
        "${var.zitadel_insecure ? "http" : "https"}://${var.zitadel_domain}${var.zitadel_port != "" ? ":${var.zitadel_port}" : ""}/v2/users/${self.triggers.user_id}/invite_code" \
        -H "Authorization: Bearer $ZITADEL_TOKEN" \
        -H "Content-Type: application/json" \
        -d '{"returnCode":{}}' \
        -o /dev/null
    EOT
  }
}
