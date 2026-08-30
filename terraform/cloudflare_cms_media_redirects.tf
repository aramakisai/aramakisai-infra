# 移行前 (Directus) の画像 URL から移行後 (Payload CMS) の URL への恒久リダイレクト
# (aramakisai-web spec `payload-cms-migration` task 5.4)
#
# 対応表は投入時 (task 8.1) の `cms/seed/id-map.json` (git 管理外) から得た。
# 対象はファイル 9 件のみ。旧 URL はクエリの有無・値を問わず新 URL の原本へ送る。
#
# zone 単体の Redirect Rules (http_request_dynamic_redirect フェーズ) は
# Free プランの API トークンでは "request is not authorized" になり作成できなかった
# (ダッシュボードにも Bulk Redirects しか出ない)。Free プランでも使える account 単位の
# Bulk Redirects (list + http_request_redirect フェーズの root ruleset) を使う。
locals {
  # 旧 Directus files.id (UUID) => 新 Payload media.id
  cms_media_legacy_redirect_map = {
    "079cb354-ad68-4384-9107-b08f719e7dd7" = 1
    "6fc63f5a-1058-4441-a2e0-f4d3b91afb57" = 2
    "7bc0ee32-05b5-4cf6-80ba-a5a8d97b2da4" = 3
    "7dcb31b7-1d26-459b-a4d2-dc387c66a314" = 4
    "87420f27-3979-430e-a53e-c745ebece696" = 5
    "8910f907-7ed0-48c1-a528-37b192c7bf81" = 6
    "9a879200-cf00-4614-8a59-a29c56dcbd79" = 7
    "a2edf49b-ac83-4d21-b47f-07b4907bd42d" = 8
    "f8dac7bd-32eb-4df3-845d-a296ca1730a7" = 9
  }
}

resource "cloudflare_list" "cms_media_legacy_redirects" {
  account_id  = var.cloudflare_account_id
  name        = "cms_media_legacy_redirects"
  kind        = "redirect"
  description = "移行前 Directus asset URL -> 移行後 Payload CMS media URL (payload-cms-migration task 5.4)"

  dynamic "item" {
    for_each = local.cms_media_legacy_redirect_map
    content {
      value {
        redirect {
          source_url            = "api.aramakisai.com/assets/${item.key}"
          target_url            = "https://cms.aramakisai.com/api/media/serve/${item.value}/original"
          status_code           = 301
          preserve_query_string = "disabled"
          subpath_matching      = "disabled"
          preserve_path_suffix  = "disabled"
          include_subdomains    = "disabled"
        }
      }
    }
  }
}

resource "cloudflare_ruleset" "cms_media_legacy_redirects" {
  account_id = var.cloudflare_account_id
  name       = "CMS media legacy URL bulk redirects"
  kind       = "root"
  phase      = "http_request_redirect"

  rules {
    description = "旧 Directus asset URL を新 CMS media URL へ一括リダイレクト"
    expression  = "true"
    action      = "redirect"
    enabled     = true

    action_parameters {
      from_list {
        name = cloudflare_list.cms_media_legacy_redirects.name
        key  = "http.request.full_uri"
      }
    }
  }
}
