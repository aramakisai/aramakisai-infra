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
        no_tls_verify = true  # ArgoCD は内部で自己署名証明書を使用
      }
    }

    # Authentik IdP
    ingress_rule {
      hostname = "idp.aramakisai.com"
      service  = "http://authentik-server.prod.svc.cluster.local:80"
    }

    # Staging フロントエンド
    ingress_rule {
      hostname = "stg.aramakisai.com"
      service  = "http://frontend.staging.svc.cluster.local:80"
    }

    # Staging API
    ingress_rule {
      hostname = "api.stg.aramakisai.com"
      service  = "http://api.staging.svc.cluster.local:80"
    }

    # フォールバック (いずれのホスト名にもマッチしない場合)
    ingress_rule {
      service = "http_status:404"
    }
  }
}
