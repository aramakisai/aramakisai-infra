# www.aramakisai.com -> https://aramakisai.com (301, パスとクエリを保持)
# zone 単体の Redirect Rules は Free プランの API トークンでは作れないため、
# cloudflare_cms_media_redirects.tf と同じ account 単位の Bulk Redirects を使う。
# root ruleset は phase ごとに 1 つなので、規則は cloudflare_ruleset.cms_media_legacy_redirects に追加している。

# 既存の手動作成レコード (A 118.20.155.69, proxied=false) を取り込む。
# proxied にして Cloudflare のエッジでリダイレクトを返すため、宛先 IP は到達しない予約アドレスにする。
import {
  to = cloudflare_record.www
  id = "${var.cloudflare_zone_id}/4b07c415b6ed7074a8333020a9cdbf95"
}

resource "cloudflare_record" "www" {
  zone_id = var.cloudflare_zone_id
  name    = "www"
  value   = "192.0.2.1"
  type    = "A"
  proxied = true
  comment = "www -> apex リダイレクト用 (Bulk Redirects、宛先 IP は使われない)"
}

resource "cloudflare_list" "www_redirect" {
  account_id  = var.cloudflare_account_id
  name        = "www_redirect"
  kind        = "redirect"
  description = "www.aramakisai.com -> https://aramakisai.com"

  item {
    value {
      redirect {
        source_url            = "www.aramakisai.com"
        target_url            = "https://aramakisai.com"
        status_code           = 301
        preserve_query_string = "enabled"
        subpath_matching      = "enabled"
        preserve_path_suffix  = "enabled"
        include_subdomains    = "disabled"
      }
    }
  }
}
