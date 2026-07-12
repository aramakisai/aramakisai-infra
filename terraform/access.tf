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
#   aramakisai-web.aramakisai.workers.dev       Workers.dev 既定URL (本番は aramakisai.com 経由)
#                                                誤って外部に晒さないよう Authentik OIDC で保護
#   aramakisai-web-dev.aramakisai.workers.dev   env.dev worker (aramakisai-web-dev) の
#                                                Workers.dev 既定URL。dev.aramakisai.com custom
#                                                domain とは別に自動生成されるため個別に保護要
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

resource "cloudflare_zero_trust_access_application" "aramakisai_web_dev" {
  account_id       = var.cloudflare_account_id
  name             = "aramakisai-web (dev)"
  domain           = "dev.aramakisai.com"
  type             = "self_hosted"
  session_duration = "24h"

  auto_redirect_to_identity = local.authentik_configured
  allowed_idps              = local.authentik_configured ? [cloudflare_zero_trust_access_identity_provider.authentik[0].id] : []
}

resource "cloudflare_zero_trust_access_application" "aramakisai_web_dev_workers_dev" {
  account_id       = var.cloudflare_account_id
  name             = "aramakisai-web-dev (workers.dev)"
  domain           = "aramakisai-web-dev.aramakisai.workers.dev"
  type             = "self_hosted"
  session_duration = "24h"

  auto_redirect_to_identity = local.authentik_configured
  allowed_idps              = local.authentik_configured ? [cloudflare_zero_trust_access_identity_provider.authentik[0].id] : []
}

# ============================================================
# Cloudflare Access Policies
# ============================================================

locals {
  access_applications = {
    aramakisai_web_workers_dev     = cloudflare_zero_trust_access_application.aramakisai_web_workers_dev.id
    aramakisai_web_dev             = cloudflare_zero_trust_access_application.aramakisai_web_dev.id
    aramakisai_web_dev_workers_dev = cloudflare_zero_trust_access_application.aramakisai_web_dev_workers_dev.id
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

# ============================================================
# Cloudflare Access: E2E CI 専用 Service Token
#
# aramakisai-web リポジトリの Playwright E2E テストが
# Authentik ログインを経由せず aramakisai-web.aramakisai.workers.dev
# へ非対話アクセスするための専用トークン。
# duration/min_days_for_renewal + create_before_destroy で
# ローテーション時の瞬断を避ける。
# ============================================================

resource "cloudflare_zero_trust_access_service_token" "e2e_ci" {
  account_id           = var.cloudflare_account_id
  name                 = "aramakisai-web E2E CI"
  duration             = "8760h"
  min_days_for_renewal = 30

  lifecycle {
    create_before_destroy = true
  }
}

# ============================================================
# Cloudflare Access: E2E Service Token 用 non_identity Policy
#
# decision = "non_identity" は既存の allow_authentik (decision = "allow")
# と共存できないため独立リソースとして追加。
# local.access_applications の for_each には相乗りさせず、
# aramakisai_web_workers_dev application_id を直接参照する
# (将来 local.access_applications に他アプリが追加されても
#  この E2E バイパスが意図せず継承されないようにするため)。
# ============================================================

resource "cloudflare_zero_trust_access_policy" "allow_e2e_service_token" {
  account_id     = var.cloudflare_account_id
  application_id = cloudflare_zero_trust_access_application.aramakisai_web_workers_dev.id
  name           = "Allow E2E Service Token"
  precedence     = 2
  decision       = "non_identity"

  include {
    service_token = [cloudflare_zero_trust_access_service_token.e2e_ci.id]
  }
}
