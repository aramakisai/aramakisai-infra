# ============================================================
# Zitadel OIDC Application (RPアプリ、PoC)
# ============================================================
#
# CMS/Vaultwarden/Roundcubeそれぞれに対応するOIDC Client (Authorization Code Flow)。
# PKCEはZitadel側に個別トグルは無く、RPがcode_challengeを送る限り自動的にサポートされる
# (confidential clientでもPKCE併用可)。既存authentik_apps.tfと同じくauth_method_typeは
# BASIC (client_secret併用) とし、redirect_uriは既存authentik設定を踏襲する
# (CMS/Roundcube/VaultwardenいずれもコールバックパスはRPアプリ自身が固定しておりIdP非依存)。

resource "zitadel_application_oidc" "cms" {
  project_id = zitadel_project.aramakisai.id
  name       = "cms-prod"

  app_type         = "OIDC_APP_TYPE_WEB"
  auth_method_type = "OIDC_AUTH_METHOD_TYPE_BASIC"
  grant_types      = ["OIDC_GRANT_TYPE_AUTHORIZATION_CODE"]
  response_types   = ["OIDC_RESPONSE_TYPE_CODE"]
  version          = "OIDC_VERSION_1_0"
  dev_mode         = false

  # CMS側のコールバックパスは authentik-endpoints.ts に '/auth/authentik/callback' として
  # ハードコードされている (IdPをまたいで再利用する汎用OIDCハンドラのため、パス自体はリネームしない。
  # task 5.1のk3d実機検証でAuthorization Code Flowが/api/auth/authentik/callbackでのみ
  # 成立することを確認済み)。
  redirect_uris = [
    "https://cms.aramakisai.com/api/auth/authentik/callback",
    "http://localhost:3000/api/auth/authentik/callback",
  ]
  post_logout_redirect_uris = ["https://cms.aramakisai.com/"]

  # CMSはgroups相当をProject Roleのroles claimから読み取る (authentik_apps.tfの
  # oauth_scope_groups mappingに相当する処理をCMS側の切替実装(task 5.1)で行う)
  id_token_role_assertion     = true
  access_token_role_assertion = true
}

resource "zitadel_application_oidc" "vaultwarden" {
  project_id = zitadel_project.aramakisai.id
  name       = "vaultwarden"

  app_type         = "OIDC_APP_TYPE_WEB"
  auth_method_type = "OIDC_AUTH_METHOD_TYPE_BASIC"
  grant_types      = ["OIDC_GRANT_TYPE_AUTHORIZATION_CODE"]
  response_types   = ["OIDC_RESPONSE_TYPE_CODE"]
  version          = "OIDC_VERSION_1_0"
  dev_mode         = false

  redirect_uris             = ["https://vault.aramakisai.com/identity/connect/oidc-signin"]
  post_logout_redirect_uris = ["https://vault.aramakisai.com/"]

  id_token_role_assertion     = true
  access_token_role_assertion = true
}

resource "zitadel_application_oidc" "roundcube" {
  project_id = zitadel_project.aramakisai.id
  name       = "roundcube"

  app_type         = "OIDC_APP_TYPE_WEB"
  auth_method_type = "OIDC_AUTH_METHOD_TYPE_BASIC"
  grant_types      = ["OIDC_GRANT_TYPE_AUTHORIZATION_CODE"]
  response_types   = ["OIDC_RESPONSE_TYPE_CODE"]
  version          = "OIDC_VERSION_1_0"
  dev_mode         = false

  redirect_uris             = ["https://webmail.aramakisai.com/index.php/login/oauth"]
  post_logout_redirect_uris = ["https://webmail.aramakisai.com/"]

  id_token_role_assertion     = true
  access_token_role_assertion = true
}

resource "zitadel_application_oidc" "cloudflare_access" {
  project_id = zitadel_project.aramakisai.id
  name       = "cloudflare-access"

  app_type         = "OIDC_APP_TYPE_WEB"
  auth_method_type = "OIDC_AUTH_METHOD_TYPE_BASIC"
  grant_types      = ["OIDC_GRANT_TYPE_AUTHORIZATION_CODE"]
  response_types   = ["OIDC_RESPONSE_TYPE_CODE"]
  version          = "OIDC_VERSION_1_0"
  dev_mode         = false

  # コールバック先はCloudflare Zero Trust側固定 (var.cloudflare_access_redirect_uris、team domain由来)
  redirect_uris = var.cloudflare_access_redirect_uris

  id_token_role_assertion     = true
  access_token_role_assertion = true
}
