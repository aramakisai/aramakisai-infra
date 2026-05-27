# ============================================================
# Cloudflare Access: Authentik OIDC IdP 登録
#
# 【注意】Authentik はクラスター上で動くため、初回 terraform apply 時は
#         authentik_cf_client_id / authentik_cf_client_secret が空で構わない。
#         Authentik セットアップ後に HCP Terraform ワークスペースで変数を設定し、
#         再度 terraform apply することで IdP が登録される。
# ============================================================

locals {
  # nonsensitive(): 比較結果は true/false のみで秘密値を露出しないため安全
  authentik_configured = nonsensitive(
    var.authentik_cf_client_id != "" &&
    var.authentik_cf_client_secret != ""
  )
}

resource "cloudflare_zero_trust_access_identity_provider" "authentik" {
  count = local.authentik_configured ? 1 : 0

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

resource "cloudflare_zero_trust_access_application" "stg" {
  account_id                = var.cloudflare_account_id
  name                      = "Staging Frontend"
  domain                    = "stg.aramakisai.com"
  type                      = "self_hosted"
  session_duration          = "24h"
  auto_redirect_to_identity = true
}

resource "cloudflare_zero_trust_access_application" "api_stg" {
  account_id                = var.cloudflare_account_id
  name                      = "Staging API"
  domain                    = "api.stg.aramakisai.com"
  type                      = "self_hosted"
  session_duration          = "24h"
  auto_redirect_to_identity = true
}

# ============================================================
# Cloudflare Access Policies
# Authentik IdP が登録済みの場合のみ作成する
# ArgoCD は自前の認証 (admin / SSO) があるため CF Access 保護対象外
# ============================================================

locals {
  access_applications = local.authentik_configured ? {
    stg     = cloudflare_zero_trust_access_application.stg.id
    api_stg = cloudflare_zero_trust_access_application.api_stg.id
  } : {}
}

resource "cloudflare_zero_trust_access_policy" "allow_authentik" {
  for_each = local.access_applications

  account_id     = var.cloudflare_account_id
  application_id = each.value
  name           = "Allow via Authentik"
  precedence     = 1
  decision       = "allow"

  include {
    login_method = [cloudflare_zero_trust_access_identity_provider.authentik[0].id]
  }
}
