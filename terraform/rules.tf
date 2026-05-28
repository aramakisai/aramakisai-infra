# ============================================================
# Cloudflare Transform Rules (Response Header Rewrite)
# ============================================================

# mail-admin.aramakisai.com: Stalwart の Location ヘッダー書き換え
#
# 問題:
#   Stalwart は HTTPS リスナーの defaultHostname (mail.aramakisai.com) を使って
#   リダイレクト先 URL を組み立てる。そのため mail-admin.aramakisai.com 経由で
#   アクセスすると Location: https://mail.aramakisai.com/... にリダイレクトされ、
#   ファイアウォールでポート 443 が閉鎖されているため到達不能になる。
#
# 修正:
#   Cloudflare で Location ヘッダーの mail.aramakisai.com を
#   mail-admin.aramakisai.com に置換する。
resource "cloudflare_ruleset" "response_header_rewrite" {
  zone_id     = var.cloudflare_zone_id
  name        = "Response Header Rewrites"
  description = "Fix internal redirects"
  kind        = "zone"
  phase       = "http_response_headers_transform"

  rules {
    action      = "rewrite"
    description = "mail-admin: Stalwart Location ヘッダーを mail → mail-admin に書き換え"
    enabled     = true
    expression  = "(http.host eq \"mail-admin.aramakisai.com\")"

    action_parameters {
      headers {
        name       = "Location"
        operation  = "set"
        expression = "regex_replace(http.response.headers[\"location\"][0], \"https://mail\\.aramakisai\\.com\", \"https://mail-admin.aramakisai.com\")"
      }
    }
  }
}
