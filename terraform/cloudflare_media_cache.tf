# メディア配信のエッジキャッシュ (旧 issue #177、Directus 撤去後は CMS 用ルールのみ)
# 用途別サイズ (サムネイル/カード/詳細) の出し分けは Cloudflare Image Transformations
# + Cache Rule のエッジ変換で行う。

# Image Transformations (Zone Settings API 上の識別子は image_resizing) は
# Cloudflare ダッシュボードで手動 ON 済み。terraform-provider-cloudflare 4.x の
# cloudflare_zone_settings_override は現在の zone plan で read-only な "mirage"
# を含む全設定を書き戻そうとして "cannot be set as it is read only" で apply
# が失敗する既知の provider 制約があり、Terraform 管理を見送った。

# zone あたり http_request_cache_settings フェーズの entrypoint ruleset は1つのみ
# (ダッシュボードで作成済みの "Bypass AppFlowy APIs" ルールが既存)。
# `terraform import cloudflare_ruleset.directus_assets_cache zone/<zone_id>/<ruleset_id>`
# 済みのため resource 名は当時のまま残す (rename は再作成扱いになるため)。
resource "cloudflare_ruleset" "directus_assets_cache" {
  zone_id = var.cloudflare_zone_id
  name    = "default" # zone phase entrypoint はダッシュボード作成時から name="default" 固定
  kind    = "zone"
  phase   = "http_request_cache_settings"

  rules {
    description = "Bypass AppFlowy APIs"
    expression  = "true"
    action      = "set_cache_settings"
    enabled     = true

    action_parameters {
      cache = false
    }
  }

  # CMS (Payload) のメディア配信。/api/media/serve/:id/:size は id パスの 302 で、
  # /api/media/file/:filename が実バイトを返す本体。両方 /api/media/ 配下で拡張子の
  # 有無が一定しないため、既定の拡張子ベースキャッシュに載らない。実バイトの方を
  # キャッシュしないと切り替え直後に懸念した「全画像が単一 pod を通る」が解消しない
  rules {
    description = "CMS media delivery (id パスの 302 と実ファイル本体)"
    expression  = "(http.host eq \"cms.aramakisai.com\") and starts_with(http.request.uri.path, \"/api/media/\")"
    action      = "set_cache_settings"
    enabled     = true

    action_parameters {
      cache = true
      edge_ttl {
        mode    = "override_origin"
        default = 2592000 # 30日。アップロード時変換済みで実体は不変、差替え時は id 自体が変わる想定
      }
    }
  }
}
