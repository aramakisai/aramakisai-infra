# ============================================================
# Authentik アプリケーション連携 (OIDC Providers & Applications)
# ============================================================

# ────────────────────────────────────────────────────────────
# 0. 独自 Property Mapping (Groups Scope)
# ────────────────────────────────────────────────────────────
resource "authentik_property_mapping_provider_scope" "oauth_scope_groups" {
  name       = "authentik default OAuth Mapping: OpenID 'groups'"
  scope_name = "groups"
  expression = "return {\n    'groups': [g.name for g in request.user.groups.all()]\n}"
}

# ────────────────────────────────────────────────────────────
# 1. Roundcube (Webmail) - 既存インポート
# ────────────────────────────────────────────────────────────
resource "authentik_provider_oauth2" "roundcube" {
  name          = "Roundcube"
  client_id     = "aramakisai-mail"
  client_secret = var.roundcube_oauth2_client_secret

  authorization_flow = data.authentik_flow.default_authorization.id
  invalidation_flow  = data.authentik_flow.default_invalidation.id

  allowed_redirect_uris = [
    {
      matching_mode = "strict"
      url           = "https://webmail.aramakisai.com/index.php/login/oauth"
    }
  ]

  property_mappings = [
    data.authentik_property_mapping_provider_scope.oauth_scope_openid.id,
    data.authentik_property_mapping_provider_scope.oauth_scope_email.id,
    data.authentik_property_mapping_provider_scope.oauth_scope_profile.id
  ]
}

resource "authentik_application" "roundcube" {
  name              = "Roundcube"
  slug              = "roundcube"
  protocol_provider = authentik_provider_oauth2.roundcube.id
  open_in_new_tab   = true
}

# ────────────────────────────────────────────────────────────
# 2. ArgoCD - 既存インポート
# ────────────────────────────────────────────────────────────
resource "authentik_provider_oauth2" "argocd" {
  name          = "ArgoCD"
  client_id     = "argocd"
  client_secret = var.argocd_oidc_client_secret

  authorization_flow = data.authentik_flow.default_authorization.id
  invalidation_flow  = data.authentik_flow.default_invalidation.id

  allowed_redirect_uris = [
    {
      matching_mode = "strict"
      url           = "https://argocd.aramakisai.com/auth/callback"
    }
  ]

  property_mappings = [
    data.authentik_property_mapping_provider_scope.oauth_scope_openid.id,
    data.authentik_property_mapping_provider_scope.oauth_scope_profile.id,
    data.authentik_property_mapping_provider_scope.oauth_scope_email.id,
    authentik_property_mapping_provider_scope.oauth_scope_groups.id
  ]
}

resource "authentik_application" "argocd" {
  name              = "ArgoCD"
  slug              = "argocd"
  protocol_provider = authentik_provider_oauth2.argocd.id
  open_in_new_tab   = true
}

# ────────────────────────────────────────────────────────────
# 3. Cloudflare Access - 新規作成
# ────────────────────────────────────────────────────────────
resource "authentik_provider_oauth2" "cloudflare" {
  name          = "Cloudflare Access"
  client_id     = var.authentik_cf_client_id
  client_secret = var.authentik_cf_client_secret

  authorization_flow = data.authentik_flow.default_authorization.id
  invalidation_flow  = data.authentik_flow.default_invalidation.id

  allowed_redirect_uris = [
    for uri in var.cloudflare_access_redirect_uris : {
      matching_mode = "strict"
      url           = uri
    }
  ]

  property_mappings = [
    data.authentik_property_mapping_provider_scope.oauth_scope_openid.id,
    data.authentik_property_mapping_provider_scope.oauth_scope_email.id,
    data.authentik_property_mapping_provider_scope.oauth_scope_profile.id
  ]
}

resource "authentik_application" "cloudflare" {
  name              = "Cloudflare Access"
  slug              = "cloudflare"
  protocol_provider = authentik_provider_oauth2.cloudflare.id
}
