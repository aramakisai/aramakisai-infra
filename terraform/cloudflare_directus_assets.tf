# Directus 画像配信 (issue #177)
# 用途別サイズ (サムネイル/カード/詳細) の出し分けは Cloudflare Image Transformations
# + Cache Rule のエッジ変換で行い、Directus (単一 pod, 512Mi) 側の配信時変換は使わない。

# Image Transformations 有効化 (Zone Settings API 上の識別子は image_resizing のまま)
resource "cloudflare_zone_settings_override" "main" {
  zone_id = var.cloudflare_zone_id
  settings {
    image_resizing = "on"
  }
}

# Directus の asset URL は /assets/<uuid> で拡張子を持たず、既定の拡張子ベース
# キャッシュ対象に載らないため明示的な Cache Rule が必要。
resource "cloudflare_ruleset" "directus_assets_cache" {
  zone_id     = var.cloudflare_zone_id
  name        = "Directus assets cache"
  description = "api.aramakisai.com / stg-api.aramakisai.com の /assets/* をエッジキャッシュ対象にする"
  kind        = "zone"
  phase       = "http_request_cache_settings"

  rules {
    description = "Directus asset delivery (拡張子なし UUID パス)"
    expression  = "(http.host eq \"api.aramakisai.com\" or http.host eq \"stg-api.aramakisai.com\") and starts_with(http.request.uri.path, \"/assets/\")"
    action      = "set_cache_settings"
    enabled     = true

    action_parameters {
      cache = true
      edge_ttl {
        mode    = "override_origin"
        default = 2592000 # 30日。アップロード時変換済みで実体は不変、差替え時はUUID自体が変わる想定
      }
    }
  }
}
