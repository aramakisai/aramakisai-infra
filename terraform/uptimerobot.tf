# ============================================================
# UptimeRobot — 公開エンドポイントの外形監視
# ============================================================
# Free プランは Webhook 通知非対応のため、通知はアカウント既定のメール
# alert contact に委ねる (Terraform では assigned_alert_contacts を管理しない)。
# interval はFreeプランの最小間隔 (5分=300秒) に合わせる。

locals {
  uptimerobot_monitors = {
    main_site = {
      name = "Aramakisai 本体サイト"
      type = "HTTP"
      url  = "https://aramakisai.com"
      port = null
    }
    staging = {
      name = "Staging フロントエンド"
      type = "HTTP"
      url  = "https://stg.aramakisai.com"
      port = null
    }
    argocd = {
      name = "ArgoCD 管理画面"
      type = "HTTP"
      url  = "https://argocd.aramakisai.com"
      port = null
    }
    webmail = {
      name = "Webmail (Roundcube)"
      type = "HTTP"
      url  = "https://webmail.aramakisai.com"
      port = null
    }
    idp = {
      name = "Authentik IdP"
      type = "HTTP"
      url  = "https://idp.aramakisai.com"
      port = null
    }
    autoconfig = {
      name = "メールクライアント自動設定 (autoconfig)"
      type = "HTTP"
      url  = "https://autoconfig.aramakisai.com/mail/config-v1.1.xml"
      port = null
    }
    mail_tcp = {
      name = "Stalwart メールサーバー TCP到達性 (443)"
      type = "PORT"
      url  = "mail.aramakisai.com"
      port = 443
    }
  }
}

resource "uptimerobot_monitor" "this" {
  for_each = local.uptimerobot_monitors

  name     = each.value.name
  type     = each.value.type
  url      = each.value.url
  port     = each.value.port
  interval = 300

  # staging は Cloudflare Access (Authentik OIDC) 経由で 302 が返るため、
  # リダイレクト先 (最終的に 200 を返すログイン画面) まで追従させる
  follow_redirections = true

  tags = ["observability-v2"]
}
