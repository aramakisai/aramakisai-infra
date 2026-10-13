# Zitadel はルートを v1 の /ui/login 経由で Console に送るため、v2 の DEFAULT_REDIRECT_URI が効かない。
# ルート完全一致だけ Login v2 へ寄せ、ログイン後に dash へ遷移させる。
# 規則は cloudflare_ruleset.cms_media_legacy_redirects に追加している (www_redirect.tf と同じ理由)。
resource "cloudflare_list" "idp_root_redirect" {
  account_id  = var.cloudflare_account_id
  name        = "idp_root_redirect"
  kind        = "redirect"
  description = "idp.aramakisai.com/ -> Login v2"

  item {
    value {
      redirect {
        source_url            = "idp.aramakisai.com/"
        target_url            = "https://idp.aramakisai.com/ui/v2/login/"
        status_code           = 302
        preserve_query_string = "disabled"
        subpath_matching      = "disabled"
        preserve_path_suffix  = "disabled"
        include_subdomains    = "disabled"
      }
    }
  }
}
