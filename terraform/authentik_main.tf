# ============================================================
# Authentik 共通データソースおよび基本定義
# ============================================================

# デフォルトフローのデータソース取得
data "authentik_flow" "default_authorization" {
  slug = "default-provider-authorization-implicit-consent"
}

data "authentik_flow" "default_authentication" {
  slug = "default-authentication-flow"
}

data "authentik_flow" "default_source_authentication" {
  slug = "default-source-authentication"
}

data "authentik_flow" "default_source_enrollment" {
  slug = "default-source-enrollment"
}

data "authentik_flow" "default_invalidation" {
  slug = "default-provider-invalidation-flow"
}

# デフォルトプロパティマッピング (OAuth2 Scopes)
data "authentik_property_mapping_provider_scope" "oauth_scope_openid" {
  managed = "goauthentik.io/providers/oauth2/scope-openid"
}

data "authentik_property_mapping_provider_scope" "oauth_scope_profile" {
  managed = "goauthentik.io/providers/oauth2/scope-profile"
}

data "authentik_property_mapping_provider_scope" "oauth_scope_email" {
  managed = "goauthentik.io/providers/oauth2/scope-email"
}
