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
  signing_key   = data.authentik_certificate_key_pair.default.id

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
  signing_key   = data.authentik_certificate_key_pair.default.id

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
  signing_key   = data.authentik_certificate_key_pair.default.id

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

# ────────────────────────────────────────────────────────────
# 4. Room Presence Tracker - 新規作成
# 実行委員室の入退室管理アプリ用 OIDC Provider
# student_id / discord_id はカスタム Property Mapping で id_token に含める
# ────────────────────────────────────────────────────────────
resource "authentik_property_mapping_provider_scope" "oauth_scope_student_id" {
  name       = "Room Presence: student_id"
  scope_name = "student_id"
  expression = "return request.user.attributes.get(\"student_id\", None)"
}

resource "authentik_property_mapping_provider_scope" "oauth_scope_discord_id" {
  name       = "Room Presence: discord_id"
  scope_name = "discord_id"
  expression = <<-EOT
    social = request.user.socialaccount_set.filter(provider="discord").first()
    return social.uid if social else None
  EOT
}

resource "authentik_provider_oauth2" "room_presence" {
  name          = "Room Presence Tracker"
  client_id     = "aramakisai-room-presence"
  client_secret = var.authentik_room_presence_client_secret
  signing_key   = data.authentik_certificate_key_pair.default.id

  authorization_flow = data.authentik_flow.default_authorization.id
  invalidation_flow  = data.authentik_flow.default_invalidation.id

  allowed_redirect_uris = [
    {
      matching_mode = "strict"
      url           = "https://presence.aramakisai.com/api/auth/callback/authentik"
    }
  ]

  property_mappings = [
    data.authentik_property_mapping_provider_scope.oauth_scope_openid.id,
    data.authentik_property_mapping_provider_scope.oauth_scope_email.id,
    data.authentik_property_mapping_provider_scope.oauth_scope_profile.id,
    authentik_property_mapping_provider_scope.oauth_scope_groups.id,
    authentik_property_mapping_provider_scope.oauth_scope_student_id.id,
    authentik_property_mapping_provider_scope.oauth_scope_discord_id.id
  ]
}

resource "authentik_application" "room_presence" {
  name              = "Room Presence Tracker"
  slug              = "room-presence"
  protocol_provider = authentik_provider_oauth2.room_presence.id
  open_in_new_tab   = true
}
