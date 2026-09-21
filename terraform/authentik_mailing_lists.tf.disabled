# ============================================================
# ML (メーリングリスト) 共有メールボックス用 Authentik User
# ============================================================
# 各MLは専用のAuthentik Userを持ち、メールはこのUserのmail属性(=ML宛アドレス)
# 経由でMaildirへ直接配送される(fan-out廃止)。パスワードはログイン用途を持たず、
# random_passwordで生成して誰にも配布しない。
#
# 7件は同一パターンの繰り返し。pr@で1件分のパターンを確立し(タスク3.1)、
# 動作確認後に残り6件を同型で複製する(タスク6.1)。

# -----------------------------------------------------------
# 1. pr@aramakisai.com (広報) — パイロット、動作確認済み
# -----------------------------------------------------------

resource "random_password" "ml_pr_password" {
  length  = 32
  special = true
}

resource "authentik_user" "ml_pr" {
  username  = "ml-pr"
  name      = "ML: 広報 (pr@)"
  email     = "pr@aramakisai.com"
  password  = random_password.ml_pr_password.result
  is_active = true
  attributes = jsonencode({
    mailListAddress = true
  })
}

# -----------------------------------------------------------
# 2. planning@aramakisai.com (企画)
# -----------------------------------------------------------

resource "random_password" "ml_planning_password" {
  length  = 32
  special = true
}

resource "authentik_user" "ml_planning" {
  username  = "ml-planning"
  name      = "ML: 企画 (planning@)"
  email     = "planning@aramakisai.com"
  password  = random_password.ml_planning_password.result
  is_active = true
  attributes = jsonencode({
    mailListAddress = true
  })
}

# -----------------------------------------------------------
# 3. accounting@aramakisai.com (会計)
# -----------------------------------------------------------

resource "random_password" "ml_accounting_password" {
  length  = 32
  special = true
}

resource "authentik_user" "ml_accounting" {
  username  = "ml-accounting"
  name      = "ML: 会計 (accounting@)"
  email     = "accounting@aramakisai.com"
  password  = random_password.ml_accounting_password.result
  is_active = true
  attributes = jsonencode({
    mailListAddress = true
  })
}

# -----------------------------------------------------------
# 4. booth@aramakisai.com (出店)
# -----------------------------------------------------------

resource "random_password" "ml_booth_password" {
  length  = 32
  special = true
}

resource "authentik_user" "ml_booth" {
  username  = "ml-booth"
  name      = "ML: 出店 (booth@)"
  email     = "booth@aramakisai.com"
  password  = random_password.ml_booth_password.result
  is_active = true
  attributes = jsonencode({
    mailListAddress = true
  })
}

# -----------------------------------------------------------
# 5. stage@aramakisai.com (出演)
# -----------------------------------------------------------

resource "random_password" "ml_stage_password" {
  length  = 32
  special = true
}

resource "authentik_user" "ml_stage" {
  username  = "ml-stage"
  name      = "ML: 出演 (stage@)"
  email     = "stage@aramakisai.com"
  password  = random_password.ml_stage_password.result
  is_active = true
  attributes = jsonencode({
    mailListAddress = true
  })
}

# -----------------------------------------------------------
# 6. admin@aramakisai.com (管理者)
# -----------------------------------------------------------
# 管理者グループは mail 属性が 6 エイリアスの multi-value
# (postmastar@ / webmastar@ / abuse@ / admin@ / administrator@ / www@)。
# mail 属性は単一値のみ受け入れるため、
# mail=admin@aramakisai.com (単一) + mailAlias に残り5件を設定する。
# mailAlias は既存の個人メンバー向けエイリアス解決機構と同じく
# LDAP_QUERY_FILTER_ALIAS (mailAlias=%s) で解決される。

resource "random_password" "ml_admin_password" {
  length  = 32
  special = true
}

resource "authentik_user" "ml_admin" {
  username  = "ml-admin"
  name      = "ML: 管理者 (admin@ + 5 aliases)"
  email     = "admin@aramakisai.com"
  password  = random_password.ml_admin_password.result
  is_active = true
  attributes = jsonencode({
    mailListAddress = true
    mailAlias = [
      "postmastar@aramakisai.com",
      "webmastar@aramakisai.com",
      "abuse@aramakisai.com",
      "administrator@aramakisai.com",
      "www@aramakisai.com",
    ]
  })
}

# -----------------------------------------------------------
# 7. general-affairs@aramakisai.com (総務)
# -----------------------------------------------------------

resource "random_password" "ml_general_affairs_password" {
  length  = 32
  special = true
}

resource "authentik_user" "ml_general_affairs" {
  username  = "ml-general-affairs"
  name      = "ML: 総務 (general-affairs@)"
  email     = "general-affairs@aramakisai.com"
  password  = random_password.ml_general_affairs_password.result
  is_active = true
  attributes = jsonencode({
    mailListAddress = true
  })
}

# -----------------------------------------------------------
# noreply@aramakisai.com (システム送信用)
# -----------------------------------------------------------
# Authentik が DMS SMTP 経由で送信する際の From アドレス。
# DMS 共有メールボックスとして LDAP プロビジョニングされる。
# Authentik は submission ポート(587)で DMS に接続し、
# DMS が Resend 経由で外部配送する。

resource "random_password" "noreply_password" {
  length  = 32
  special = true
}

resource "authentik_user" "noreply" {
  username  = "noreply"
  name      = "noreply (system sender)"
  email     = "noreply@aramakisai.com"
  password  = random_password.noreply_password.result
  is_active = true
  attributes = jsonencode({
    mailListAddress = true
  })
}
