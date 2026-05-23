# ============================================================
# Cloudflare Access: Authentik OIDC IdP 登録
# ============================================================

resource "cloudflare_access_identity_provider" "authentik" {
  account_id = var.cloudflare_account_id
  name       = "Authentik"
  type       = "oidc"

  config {
    client_id     = var.authentik_cf_client_id
    client_secret = var.authentik_cf_client_secret

    # Authentik 標準エンドポイント
    # Provider slug は Authentik 管理画面で "cloudflare" に設定すること
    auth_url  = "https://idp.aramakisai.com/application/o/cloudflare/authorize/"
    token_url = "https://idp.aramakisai.com/application/o/cloudflare/token/"
    certs_url = "https://idp.aramakisai.com/application/o/cloudflare/jwks/"

    scopes = ["openid", "email", "profile"]
  }
}

# ============================================================
# Cloudflare Access Applications
# ============================================================

resource "cloudflare_access_application" "stg" {
  account_id                = var.cloudflare_account_id
  name                      = "Staging Frontend"
  domain                    = "stg.aramakisai.com"
  type                      = "self_hosted"
  session_duration          = "24h"
  auto_redirect_to_identity = true
}

resource "cloudflare_access_application" "api_stg" {
  account_id                = var.cloudflare_account_id
  name                      = "Staging API"
  domain                    = "api.stg.aramakisai.com"
  type                      = "self_hosted"
  session_duration          = "24h"
  auto_redirect_to_identity = true
}

resource "cloudflare_access_application" "argocd" {
  account_id                = var.cloudflare_account_id
  name                      = "ArgoCD"
  domain                    = "argocd.aramakisai.com"
  type                      = "self_hosted"
  session_duration          = "8h"   # 管理画面はセキュリティ強化のため短め
  auto_redirect_to_identity = true
}

# ============================================================
# Cloudflare Access Policies
# ============================================================

locals {
  access_applications = {
    stg     = cloudflare_access_application.stg.id
    api_stg = cloudflare_access_application.api_stg.id
    argocd  = cloudflare_access_application.argocd.id
  }
}

resource "cloudflare_access_policy" "allow_authentik" {
  for_each = local.access_applications

  account_id     = var.cloudflare_account_id
  application_id = each.value
  name           = "Allow via Authentik"
  precedence     = 1
  decision       = "allow"

  include {
    login_method = [cloudflare_access_identity_provider.authentik.id]
  }
}
