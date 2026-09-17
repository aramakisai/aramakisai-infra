# ============================================================
# 学生団体担当者用 Authentik ユーザーのプロビジョニング
# ============================================================
# terraform/data/student_exhibitors.csv からデータを読み込み、
# 学生団体担当者の Authentik ユーザーを自動生成・管理する。
# パスワードは未設定状態で作成し、別途パスワード設定フローから設定する。

locals {
  student_exhibitors = {
    for row in csvdecode(file("${path.module}/data/student_exhibitors.csv")) : row.email => row
  }
}

resource "authentik_user" "student_exhibitors" {
  for_each = local.student_exhibitors

  username = each.key
  name     = each.value.team_name
  email    = each.value.email
  type     = "internal"
  groups   = [authentik_group.student_exhibitor.id]
}
