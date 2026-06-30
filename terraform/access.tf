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
#
# 保護対象:
#   (なし - stg/api_stg は廃止。staging frontend は Cloudflare Pages PR preview URL を使用。
#           stg-api.aramakisai.com は Directus 自身の admin 認証のみで保護)
#
# 非保護 (自前認証あり):
#   webmail.aramakisai.com   Roundcube が Authentik OAuth2 で保護
#                            CF Access を重ねると二重認証になるため除外
#   argocd.aramakisai.com    ArgoCD 自前認証 (admin / Authentik SSO) で保護
# ============================================================

# ============================================================
# Cloudflare Access Policies
# 保護対象 Application が存在しないため空 Map
# ============================================================

locals {
  access_applications = {}
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
