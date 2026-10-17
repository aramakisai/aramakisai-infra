# 旧 2025 年版サイトの origin 証明書は期限切れで Full (strict) の検証に通らない。
# resolveOverride 時にどちらの hostname で規則が評価されるか公式 docs に記載がないため、
# 両方 (override 先ホスト / apex の /2025/ 配下) にマッチさせる。
# zone phase ごとに entrypoint ruleset は 1 つのため、他の http_config_settings 規則を足す場合はここに追加する。
resource "cloudflare_ruleset" "origin_2025_ssl" {
  zone_id = var.cloudflare_zone_id
  name    = "default"
  kind    = "zone"
  phase   = "http_config_settings"

  rules {
    description = "旧2025年版 origin は期限切れ証明書のため SSL を full にする"
    expression  = "(http.host eq \"origin-2025.aramakisai.com\") or (http.host eq \"aramakisai.com\" and starts_with(http.request.uri.path, \"/2025/\"))"
    action      = "set_config"
    enabled     = true

    action_parameters {
      ssl = "full"
    }
  }
}
