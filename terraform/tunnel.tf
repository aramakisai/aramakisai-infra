resource "cloudflare_zero_trust_tunnel_cloudflared" "main" {
  account_id = var.cloudflare_account_id
  name       = "aramakisai-k3s"
  secret     = base64encode(var.cf_tunnel_secret)
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "main" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.main.id

  config {
    # Roundcube Webmail
    # Roundcube 自身が Authentik OAuth2 (oauth_login_redirect) で認証するため CF Access は不要
    # port 443 をファイアウォールで開放せずに webmail を提供するための経路
    ingress_rule {
      hostname = "webmail.aramakisai.com"
      service  = "http://roundcube.prod.svc.cluster.local:80"
    }

    # ArgoCD UI
    ingress_rule {
      hostname = "argocd.aramakisai.com"
      service  = "https://argocd-server.argocd.svc.cluster.local:443"
      origin_request {
        no_tls_verify = true # ArgoCD は内部で自己署名証明書を使用
      }
    }

    # Zitadel IdP: login v2 UIはZitadel公式ドキュメント(reverse proxy設定例)通り
    # /ui/v2/login配下のみ別コンテナ(login, port 3000)、それ以外は全てAPI(port 8080)。
    # path指定ルールは先勝ちのため、より具体的なpath指定ルールを先に、無指定の
    # フォールバック(8080)を最後に置く必要がある
    ingress_rule {
      hostname = "idp.aramakisai.com"
      path     = "^/ui/v2/login.*"
      service  = "http://zitadel.zitadel.svc.cluster.local:3000"
    }

    # vaultwarden-rbac-sync: Zitadel Actions v2 webhook受信用。ZitadelのHTTPClient.DenyList
    # (SSRF対策)がRFC1918/cluster-local宛先へのtarget作成を拒否するため外部公開が必須だが、
    # 新規サブドメインは増やさず(サブドメインを冗長に増やさない方針)、既にZitadel自身の
    # 外部到達に使っている idp.aramakisai.com にpathで相乗りさせる。ブラウザアクセスは
    # 想定しないためCloudflare Accessは付与しない(認証はwebhook側の署名検証に委ねる)。
    ingress_rule {
      hostname = "idp.aramakisai.com"
      path     = "^/webhook/rbac-sync.*"
      service  = "http://vaultwarden-rbac-sync.prod.svc.cluster.local:80"
    }

    ingress_rule {
      hostname = "idp.aramakisai.com"
      service  = "http://zitadel.zitadel.svc.cluster.local:8080"
    }

    # Staging フロントエンド
    ingress_rule {
      hostname = "stg.aramakisai.com"
      service  = "http://frontend.staging.svc.cluster.local:80"
    }

    # api.aramakisai.com への ingress rule は持たない。5.4 の旧 URL リダイレクトで
    # カバーされないパスは fallback (404) に落ちる想定 (Directus 本体は撤去済み)

    # Production CMS (Payload)
    ingress_rule {
      hostname = "cms.aramakisai.com"
      service  = "http://cms.prod.svc.cluster.local:80"
    }

    # Room Presence Tracker (実行委員室 在室管理)
    ingress_rule {
      hostname = "presence.aramakisai.com"
      service  = "http://room-presence.prod.svc.cluster.local:3000"
    }

    # Vaultwarden (Password Manager)
    ingress_rule {
      hostname = "vault.aramakisai.com"
      service  = "http://vaultwarden.prod.svc.cluster.local:80"
    }

    # フォールバック (いずれのホスト名にもマッチしない場合)
    ingress_rule {
      service = "http_status:404"
    }
  }
}
