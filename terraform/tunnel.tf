resource "cloudflare_zero_trust_tunnel_cloudflared" "main" {
  account_id = var.cloudflare_account_id
  name       = "aramakisai-k3s"
  secret     = base64encode(var.cf_tunnel_secret)
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "main" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.main.id

  config {
    # Stalwart Web Admin UI
    # proxied = true (Cloudflare CDN) → Tunnel → stalwart-admin ClusterIP:443
    # CF Access は設定しない。Stalwart 独自のログインで保護される。
    # no_tls_verify = true: Stalwart の TLS 証明書は Let's Encrypt (ACME DNS-01) だが、
    #                        内部 ClusterIP 経由のため SNI が一致しない場合があるため
    ingress_rule {
      hostname = "mail-admin.aramakisai.com"
      service  = "https://stalwart-admin.prod.svc.cluster.local:443"
      origin_request {
        no_tls_verify = true
      }
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
