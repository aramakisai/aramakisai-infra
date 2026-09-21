# ============================================================
# Cloudflare Access: Zitadel OIDC IdP 登録
#
# client_id/client_secretはvar.zitadel_cf_access_client_id/secretを参照する
# (ansible/roles/zitadel-bootstrap が cloudflare-access OIDC Applicationを作成し、
# 発行されたclient_id/secretをInfisicalへ登録する。project/role/application管理が
# Ansible側へ移ったため、authentik時代のauthentik_cf_client_id/secretと同じ
# チキンエッグ回避パターンに戻った。variables.tf参照)。
# ============================================================

resource "cloudflare_zero_trust_access_identity_provider" "zitadel" {
  account_id = var.cloudflare_account_id
  name       = "Zitadel"
  type       = "oidc"

  config {
    client_id     = var.zitadel_cf_access_client_id
    client_secret = var.zitadel_cf_access_client_secret

    # Zitadel標準OIDCエンドポイント (v2 API、instance固定パス)
    auth_url  = "https://idp.aramakisai.com/oauth/v2/authorize"
    token_url = "https://idp.aramakisai.com/oauth/v2/token"
    certs_url = "https://idp.aramakisai.com/oauth/v2/keys"

    scopes = ["openid", "email", "profile"]
  }
}

# ============================================================
# Cloudflare Access Applications
#
# 保護対象:
#   aramakisai-web.aramakisai.workers.dev       Workers.dev 既定URL (本番は aramakisai.com 経由)
#                                                誤って外部に晒さないよう Zitadel OIDC で保護
#   aramakisai-web-dev.aramakisai.workers.dev   env.dev worker (aramakisai-web-dev) の
#                                                Workers.dev 既定URL。dev.aramakisai.com custom
#                                                domain とは別に自動生成されるため個別に保護要
#
# 非保護 (自前認証あり):
#   webmail.aramakisai.com   Roundcube が IdP OAuth2 で保護 (本番切替はtask9.4、現状はauthentik)
#                            CF Access を重ねると二重認証になるため除外
#   argocd.aramakisai.com    ArgoCD 自前認証 (admin / IdP SSO) で保護
# ============================================================

resource "cloudflare_zero_trust_access_application" "aramakisai_web_workers_dev" {
  account_id = var.cloudflare_account_id
  name       = "aramakisai-web (workers.dev)"
  # wrangler versions upload が発行するPRプレビューURLはバージョンID由来の
  # ラベルがデプロイごとに変わるため、完全一致では捕捉できない。
  # 基底URL (aramakisai-web.aramakisai.workers.dev) は workers_dev = false により
  # 実体がなく、保護対象に含めると認証後のコールバックが404へ落ちる
  domain           = "*-aramakisai-web.aramakisai.workers.dev"
  type             = "self_hosted"
  session_duration = "24h"

  destinations {
    type = "public"
    uri  = "*-aramakisai-web.aramakisai.workers.dev"
  }

  auto_redirect_to_identity = true
  allowed_idps              = [cloudflare_zero_trust_access_identity_provider.zitadel.id]
}

resource "cloudflare_zero_trust_access_application" "aramakisai_web_dev" {
  account_id       = var.cloudflare_account_id
  name             = "aramakisai-web (dev)"
  domain           = "dev.aramakisai.com"
  type             = "self_hosted"
  session_duration = "24h"

  auto_redirect_to_identity = true
  allowed_idps              = [cloudflare_zero_trust_access_identity_provider.zitadel.id]
}

resource "cloudflare_zero_trust_access_application" "aramakisai_web_dev_workers_dev" {
  account_id       = var.cloudflare_account_id
  name             = "aramakisai-web-dev (workers.dev)"
  domain           = "aramakisai-web-dev.aramakisai.workers.dev"
  type             = "self_hosted"
  session_duration = "24h"

  auto_redirect_to_identity = true
  allowed_idps              = [cloudflare_zero_trust_access_identity_provider.zitadel.id]
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

resource "cloudflare_zero_trust_access_policy" "allow_zitadel" {
  for_each = local.access_applications

  account_id     = var.cloudflare_account_id
  application_id = each.value
  name           = "Allow via Zitadel"
  precedence     = 1
  decision       = "allow"

  include {
    login_method = [cloudflare_zero_trust_access_identity_provider.zitadel.id]
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
# decision = "non_identity" は既存の allow_zitadel (decision = "allow")
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
