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
    # 注意: authorize/token は slug 非スコープの共通エンドポイント (Authentik側の仕様)。
    #       jwks のみ slug スコープ。/.well-known/openid-configuration で要確認。
    auth_url  = "https://idp.aramakisai.com/application/o/authorize/"
    token_url = "https://idp.aramakisai.com/application/o/token/"
    certs_url = "https://idp.aramakisai.com/application/o/cloudflare/jwks/"

    scopes = ["openid", "email", "profile"]
  }
}

# ============================================================
# Cloudflare Access Applications
#
# 保護対象:
#   aramakisai-web.aramakisai.workers.dev   Workers.dev 既定URL (本番は aramakisai.com 経由)
#                                            誤って外部に晒さないよう Authentik OIDC で保護
#
# 非保護 (自前認証あり):
#   webmail.aramakisai.com   Roundcube が Authentik OAuth2 で保護
#                            CF Access を重ねると二重認証になるため除外
#   argocd.aramakisai.com    ArgoCD 自前認証 (admin / Authentik SSO) で保護
# ============================================================

resource "cloudflare_zero_trust_access_application" "aramakisai_web_workers_dev" {
  account_id       = var.cloudflare_account_id
  name             = "aramakisai-web (workers.dev)"
  domain           = "aramakisai-web.aramakisai.workers.dev"
  type             = "self_hosted"
  session_duration = "24h"

  # auto_redirect_to_identity requires allowed_idps with exactly one IdP
  auto_redirect_to_identity = local.authentik_configured
  allowed_idps              = local.authentik_configured ? [cloudflare_zero_trust_access_identity_provider.authentik[0].id] : []
}

# ============================================================
# Cloudflare Access Policies
# ============================================================

locals {
  access_applications = {
    aramakisai_web_workers_dev = cloudflare_zero_trust_access_application.aramakisai_web_workers_dev.id
  }
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
