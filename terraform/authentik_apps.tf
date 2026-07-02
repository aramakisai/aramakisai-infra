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

# Roundcube 専用 email mapping: IMAP identity を ML 共有メールボックスにルーティングする。
#
# 優先度: 部署グループ > 管理者 (admin) > 個人メール
#
# leader グループは部署所属が前提のため IMAP identity は部署アドレスのまま。
# 部署リーダーの全 Shared/* アクセスは acl_groups 経由（mail_acl_groups scope）で解決する。
resource "authentik_property_mapping_provider_scope" "oauth_scope_email_roundcube" {
  name       = "Roundcube: OpenID 'email' with ML group priority"
  scope_name = "email"
  expression = <<-EOT
    groups = request.user.groups.all()
    dept_mails = []
    admin_mail = None

    for g in groups:
        mail_attr = g.attributes.get("mail", None)
        if mail_attr:
            # mail 属性は string または list の可能性がある
            if isinstance(mail_attr, list):
                mail = mail_attr[0]
            else:
                mail = mail_attr

            # 管理者グループは優先度最低（部署グループが無い場合のみフォールバック）
            if g.name == "admin" or (isinstance(mail, str) and mail.startswith("admin@")):
                admin_mail = mail
            else:
                dept_mails.append(mail)

    email = dept_mails[0] if dept_mails else (admin_mail if admin_mail else request.user.email)

    return {
        "email": email,
    }
  EOT
}

# Roundcube 専用 mail_acl_groups scope:
# ログインユーザーの現在のグループ所属から mailAclSlug を動的に計算して返す。
# discord-group-sync-policy のキャッシュ値に依存せず常に最新のグループ所属を反映する。
# Dovecot oauth2 passdb が introspection レスポンスからこの値を取得し、
# DOVECOT_USER_ATTRS の %{passdb:mailAclGroups} 経由で acl_groups に注入する。
# これにより ml-* の共有 LDAP 属性ではなく per-user のグループ所属が ACL に反映される。
resource "authentik_property_mapping_provider_scope" "oauth_scope_mail_acl_groups" {
  name       = "Roundcube: mail_acl_groups for Dovecot ACL"
  scope_name = "mail_acl_groups"
  expression = <<-EOT
    # Dovecot: passdb extra field が "userdb_xxx" 名の場合、userdb フィールド "xxx" として
    # 自動注入される。ただし oauth2 passdb は introspection レスポンスのフィールドを
    # 自動転送しないため、dovecot-oauth2.conf.ext 側で
    #   pass_attrs = userdb_acl_groups=%%{oauth2:userdb_acl_groups}
    # を設定して introspection の "userdb_acl_groups" クレームを passdb extra field に
    # マップする (dovecot-oauth2-external-secret.yaml 参照)。これにより userdb field
    # acl_groups へ反映され Dovecot ACL plugin の group= マッチが機能する。
    # LDAP user_attrs (%%{passdb:xxx}) は展開非対応のため使えない。
    return {
        "userdb_acl_groups": ",".join(sorted({
            g.attributes.get("mailAclSlug")
            for g in request.user.groups.all()
            if g.attributes.get("mailAclSlug")
        }))
    }
  EOT
}

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
    authentik_property_mapping_provider_scope.oauth_scope_email_roundcube.id,
    data.authentik_property_mapping_provider_scope.oauth_scope_profile.id,
    authentik_property_mapping_provider_scope.oauth_scope_mail_acl_groups.id,
  ]
}

resource "authentik_application" "roundcube" {
  name              = "Roundcube"
  slug              = "roundcube"
  protocol_provider = authentik_provider_oauth2.roundcube.id
  open_in_new_tab   = true
  meta_icon         = "fa://fa-solid fa-envelope"
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
  meta_icon         = "fa://fa-solid fa-dharmachakra"
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
  meta_icon         = "fa://fa-solid fa-cloud"
}

# ────────────────────────────────────────────────────────────
# 4. Vaultwarden - 新規作成
# ────────────────────────────────────────────────────────────

# Vaultwardenはemail_verified=trueを要求するが、Authentikデフォルトmappingが
# falseを返すケースがあるため、強制的にTrueを返す専用mappingを使用。
resource "authentik_property_mapping_provider_scope" "oauth_scope_email_vaultwarden" {
  name       = "Vaultwarden: OpenID 'email' with verified"
  scope_name = "email"
  expression = <<-EOT
    return {
      "email": request.user.email,
      "email_verified": True,
    }
  EOT
}

resource "authentik_provider_oauth2" "vaultwarden" {
  name          = "Vaultwarden"
  client_id     = "vaultwarden"
  client_secret = var.vaultwarden_oidc_client_secret
  signing_key   = data.authentik_certificate_key_pair.default.id

  authorization_flow = data.authentik_flow.default_authorization.id
  invalidation_flow  = data.authentik_flow.default_invalidation.id

  allowed_redirect_uris = [
    {
      matching_mode = "strict"
      url           = "https://vault.aramakisai.com/identity/connect/oidc-signin"
    }
  ]

  property_mappings = [
    data.authentik_property_mapping_provider_scope.oauth_scope_openid.id,
    authentik_property_mapping_provider_scope.oauth_scope_email_vaultwarden.id,
    data.authentik_property_mapping_provider_scope.oauth_scope_profile.id,
    authentik_property_mapping_provider_scope.oauth_scope_groups.id
  ]
}

resource "authentik_application" "vaultwarden" {
  name              = "Vaultwarden"
  slug              = "vaultwarden"
  protocol_provider = authentik_provider_oauth2.vaultwarden.id
  open_in_new_tab   = true
  meta_icon         = "fa://fa-solid fa-key"
}

# ────────────────────────────────────────────────────────────
# 5. Room Presence Tracker - 新規作成
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
  # request.user.socialaccount_set は django-allauth の属性で Authentik には存在しない
  # (AttributeError -> /authorize が invalid_request:malformed を返しログイン不能になっていた)。
  # Authentik 本来の OAuth Source Connection モデルを使う。例外時はNoneを返し、
  # discord連携が未完了/取得失敗でもログイン自体は通すフェイルセーフにする。
  expression = <<-EOT
    try:
        from authentik.sources.oauth.models import UserOAuthSourceConnection
        connection = UserOAuthSourceConnection.objects.filter(
            user=request.user, source__slug="discord"
        ).first()
        return connection.identifier if connection else None
    except Exception:
        return None
  EOT
}

resource "authentik_provider_oauth2" "room_presence" {
  name          = "Room Presence Tracker"
  client_id     = "aramakisai-room-presence"
  client_secret = var.authentik_room_presence_client_secret
  signing_key   = data.authentik_certificate_key_pair.default.id

  authorization_flow = data.authentik_flow.default_authorization.id
  invalidation_flow  = data.authentik_flow.default_invalidation.id

  # 注意: grant_types はこのTerraform providerのschemaに存在せず設定不可。
  # 新規作成されたProviderはgrant_typesが空リストになり、response_type=codeの
  # authorize要求が"invalid_request: malformed"で即時拒否される
  # (import済みの他provider [argocd等] は元々全種設定済みだったため発覚しなかった)。
  # Authentik API (PATCH /api/v3/providers/oauth2/{id}/) で直接修正すること。

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
  meta_icon         = "fa://fa-solid fa-door-open"
}

# ────────────────────────────────────────────────────────────
# 6. Directus CMS - SSO 連携
# ────────────────────────────────────────────────────────────

# 学生団体担当者グループ (実行委員は authentik_discord.tf の executive グループを再利用)
resource "authentik_group" "student_exhibitor" {
  name = "student_exhibitor"
}

# prod
resource "authentik_provider_oauth2" "directus_prod" {
  name          = "directus-prod"
  client_id     = "directus-prod"
  client_secret = var.directus_prod_oidc_client_secret
  signing_key   = data.authentik_certificate_key_pair.default.id

  authorization_flow = data.authentik_flow.default_authorization.id
  invalidation_flow  = data.authentik_flow.default_invalidation.id

  allowed_redirect_uris = [{
    matching_mode = "strict"
    url           = "https://api.aramakisai.com/auth/login/authentik/callback"
  }]

  property_mappings = [
    data.authentik_property_mapping_provider_scope.oauth_scope_openid.id,
    data.authentik_property_mapping_provider_scope.oauth_scope_profile.id,
    data.authentik_property_mapping_provider_scope.oauth_scope_email.id,
    authentik_property_mapping_provider_scope.oauth_scope_groups.id,
  ]
}

resource "authentik_application" "directus_prod" {
  name              = "Directus (prod)"
  slug              = "directus-prod"
  protocol_provider = authentik_provider_oauth2.directus_prod.id
  open_in_new_tab   = true
  meta_icon         = "fa://fa-solid fa-database"
}

# stg
resource "authentik_provider_oauth2" "directus_stg" {
  name          = "directus-stg"
  client_id     = "directus-stg"
  client_secret = var.directus_stg_oidc_client_secret
  signing_key   = data.authentik_certificate_key_pair.default.id

  authorization_flow = data.authentik_flow.default_authorization.id
  invalidation_flow  = data.authentik_flow.default_invalidation.id

  allowed_redirect_uris = [{
    matching_mode = "strict"
    url           = "https://stg-api.aramakisai.com/auth/login/authentik/callback"
  }]

  property_mappings = [
    data.authentik_property_mapping_provider_scope.oauth_scope_openid.id,
    data.authentik_property_mapping_provider_scope.oauth_scope_profile.id,
    data.authentik_property_mapping_provider_scope.oauth_scope_email.id,
    authentik_property_mapping_provider_scope.oauth_scope_groups.id,
  ]
}

resource "authentik_application" "directus_stg" {
  name              = "Directus (stg)"
  slug              = "directus-stg"
  protocol_provider = authentik_provider_oauth2.directus_stg.id
  open_in_new_tab   = true
  meta_icon         = "fa://fa-solid fa-database"
}
