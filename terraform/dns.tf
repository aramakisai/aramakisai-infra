locals {
  # Cloudflare Tunnel への CNAME ターゲット
  tunnel_cname = "${cloudflare_tunnel.main.id}.cfargotunnel.com"
}

# ArgoCD UI
resource "cloudflare_record" "argocd" {
  zone_id = var.cloudflare_zone_id
  name    = "argocd"
  value   = local.tunnel_cname
  type    = "CNAME"
  proxied = true
  comment = "ArgoCD UI (Cloudflare Tunnel)"
}

# Authentik IdP
resource "cloudflare_record" "idp" {
  zone_id = var.cloudflare_zone_id
  name    = "idp"
  value   = local.tunnel_cname
  type    = "CNAME"
  proxied = true
  comment = "Authentik IdP (Cloudflare Tunnel)"
}

# Staging フロントエンド
resource "cloudflare_record" "stg" {
  zone_id = var.cloudflare_zone_id
  name    = "stg"
  value   = local.tunnel_cname
  type    = "CNAME"
  proxied = true
  comment = "Staging frontend (Cloudflare Tunnel)"
}

# Staging API
resource "cloudflare_record" "api_stg" {
  zone_id = var.cloudflare_zone_id
  name    = "api.stg"
  value   = local.tunnel_cname
  type    = "CNAME"
  proxied = true
  comment = "Staging API (Cloudflare Tunnel)"
}
