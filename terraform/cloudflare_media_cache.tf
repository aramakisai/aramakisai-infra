# メディア配信のエッジキャッシュ (旧 issue #177、Directus 撤去後は CMS 用ルールのみ)
# 用途別サイズ (サムネイル/カード/詳細) の出し分けは Cloudflare Image Transformations
# + Cache Rule のエッジ変換で行う。

# Image Transformations (Zone Settings API 上の識別子は image_resizing) は
# Cloudflare ダッシュボードで手動 ON 済み。terraform-provider-cloudflare 4.x の
# cloudflare_zone_settings_override は現在の zone plan で read-only な "mirage"
# を含む全設定を書き戻そうとして "cannot be set as it is read only" で apply
# が失敗する既知の provider 制約があり、Terraform 管理を見送った。

# zone あたり http_request_cache_settings フェーズの entrypoint ruleset は1つのみ。
# `terraform import cloudflare_ruleset.directus_assets_cache zone/<zone_id>/<ruleset_id>`
# 済みのため resource 名は当時のまま残す (rename は再作成扱いになるため)。
resource "cloudflare_ruleset" "directus_assets_cache" {
  zone_id = var.cloudflare_zone_id
  name    = "default" # zone phase entrypoint はダッシュボード作成時から name="default" 固定
  kind    = "zone"
  phase   = "http_request_cache_settings"

  # Cookie 付き要求でも .webp 等は Cloudflare 既定の拡張子ベースキャッシュに載り、
  # ログイン中の応答が匿名に配られるため、明示的に bypass する。
  rules {
    description = "CMS media Cookie 付きはキャッシュしない"
    expression  = "(http.host eq \"cms.aramakisai.com\") and starts_with(http.request.uri.path, \"/api/media/\") and (http.cookie ne \"\")"
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
  # Cookie 付き (CMS ログイン中) の応答は非公開メディアを含みうる。override_origin で
  # 30 日キャッシュされると Cookie 無しの匿名リクエストにも配られるため、Cookie 無しに限る。
  # Authorization 付きは Cloudflare 既定で BYPASS されるため条件に含めない
  rules {
    description = "CMS media delivery (id パスの 302 と実ファイル本体)"
    expression  = "(http.host eq \"cms.aramakisai.com\") and starts_with(http.request.uri.path, \"/api/media/\") and (http.cookie eq \"\")"
    action      = "set_cache_settings"
    enabled     = true

    action_parameters {
      cache = true
      edge_ttl {
        mode    = "override_origin"
        default = 2592000 # 30日。アップロード時変換済みで実体は不変、差替え時は id 自体が変わる想定
        # serve の 302 はオリジンが Cache-Control を返さず、status_code_ttl で明示しないとキャッシュされない
        status_code_ttl {
          status_code = 302
          value       = 2592000
        }
        # 4xx/5xx は一時エラーの長期固着を防ぐため短く保つ
        status_code_ttl {
          status_code_range {
            from = 400
            to   = 599
          }
          value = 60
        }
      }
    }
  }

  # CMS の公開 JSON 応答 (GET /api/*) をエッジで 60 秒キャッシュする。
  # サイネージ・駐車場は保存から数秒以内の反映が要件、announcements/topics は公開日時で
  # 保存なしに公開へ切り替わるため除外する。ただしサイトの取得には即時反映の要件が無いため、
  # 目印のクエリ __edge=1 付きに限り performance_slots / stages / sponsors / globals/site_settings
  # をキャッシュ対象に戻す。目印があっても他の除外は解除しない。
  # Cookie/Authorization 付きは認証状態で応答が変わるため対象外。
  rules {
    description = "CMS 公開 JSON 応答を 60 秒エッジキャッシュ (即時反映が要る系統と認証付きは除外)"
    expression = replace(trimspace(<<-EOT
      (http.host eq "cms.aramakisai.com")
      and (http.request.method eq "GET")
      and starts_with(http.request.uri.path, "/api/")
      and (
        not (
          starts_with(http.request.uri.path, "/api/media")
          or starts_with(http.request.uri.path, "/api/health")
          or starts_with(http.request.uri.path, "/api/users")
          or starts_with(http.request.uri.path, "/api/lost_items")
          or starts_with(http.request.uri.path, "/api/parking_statuses")
          or starts_with(http.request.uri.path, "/api/parking_lots")
          or starts_with(http.request.uri.path, "/api/parking_snapshots")
          or starts_with(http.request.uri.path, "/api/performance_slots")
          or starts_with(http.request.uri.path, "/api/stages")
          or starts_with(http.request.uri.path, "/api/telops")
          or starts_with(http.request.uri.path, "/api/sponsors")
          or starts_with(http.request.uri.path, "/api/signage_slides")
          or starts_with(http.request.uri.path, "/api/signage_groups")
          or starts_with(http.request.uri.path, "/api/announcements")
          or starts_with(http.request.uri.path, "/api/topics")
          or starts_with(http.request.uri.path, "/api/globals/signage_settings")
          or starts_with(http.request.uri.path, "/api/globals/site_settings")
        )
        or (
          http.request.uri.query contains "__edge=1"
          and (
            starts_with(http.request.uri.path, "/api/performance_slots")
            or starts_with(http.request.uri.path, "/api/stages")
            or starts_with(http.request.uri.path, "/api/sponsors")
            or starts_with(http.request.uri.path, "/api/globals/site_settings")
          )
        )
      )
      and (http.cookie eq "")
      and not has_key(http.request.headers, "authorization")
    EOT
    ), "/\\s+/", " ")
    action  = "set_cache_settings"
    enabled = true

    action_parameters {
      cache = true
      edge_ttl {
        mode    = "override_origin"
        default = 60
      }
      browser_ttl {
        mode = "bypass"
      }
    }
  }

  # 旧 2025 年版 WordPress (Worker が origin-2025 へ転送) のアーカイブは更新されないため、
  # エッジで 1 日キャッシュしてオリジンへの負荷を減らす。
  # Cookie 付き (wp-admin ログイン中・コメント投稿者など) の応答は匿名に配られないよう対象外。
  # 管理系と REST/cron は応答が動的なので /2025/ 配下でも除外する。
  # browser_ttl はオリジンの Cache-Control に従う。
  rules {
    description = "旧 2025 年版アーカイブ (/2025/) を 1 日エッジキャッシュ (Cookie 付きと管理系は除外)"
    expression = replace(trimspace(<<-EOT
      (http.host eq "aramakisai.com")
      and starts_with(http.request.uri.path, "/2025/")
      and (http.request.method eq "GET")
      and (http.cookie eq "")
      and not (
        http.request.uri.path contains "/wp-json"
        or http.request.uri.path contains "/wp-admin"
        or http.request.uri.path contains "/wp-login"
        or http.request.uri.path contains "/wp-cron"
      )
    EOT
    ), "/\\s+/", " ")
    action  = "set_cache_settings"
    enabled = true

    action_parameters {
      cache = true
      edge_ttl {
        mode    = "override_origin"
        default = 86400
      }
      browser_ttl {
        mode = "respect_origin"
      }
    }
  }
}

# Smart Tiered Cache。アップストリームのデータセンターをゾーンごとに 1 つ自動選定し、
# エッジからオリジンへの取得を集約する (Argo の有料 Tiered Caching とは別物で追加料金なし)。
resource "cloudflare_tiered_cache" "smart" {
  zone_id    = var.cloudflare_zone_id
  cache_type = "smart"
}
