# cms のログイン系エンドポイントへの総当たりを抑える。
#
# Free プランの制約: http_ratelimit のルールは 1 件、period と mitigation_timeout は 10 秒のみ、
# 集計単位は IP。パスは 1 ルールに束ねて枠を節約する。
# zone あたり http_ratelimit フェーズの entrypoint ruleset は 1 つのみ。
resource "cloudflare_ruleset" "cms_login_ratelimit" {
  zone_id = var.cloudflare_zone_id
  name    = "default" # zone phase entrypoint の name は固定
  kind    = "zone"
  phase   = "http_ratelimit"

  rules {
    description = "cms のログイン・パスワード再設定系 POST を IP ごとに制限"
    action      = "block"
    enabled     = true
    expression = replace(trimspace(<<-EOT
      (http.host eq "cms.aramakisai.com") and (http.request.method eq "POST") and (
        http.request.uri.path in {"/api/users/login" "/api/users/forgot-password" "/api/users/reset-password" "/api/users/unlock"}
      )
    EOT
    ), "/\\s+/", " ")

    ratelimit {
      characteristics     = ["cf.colo.id", "ip.src"]
      period              = 10
      requests_per_period = 5
      mitigation_timeout  = 10
    }
  }
}
