# ============================================================
# Docker Mailserver (DMS) 連携用 LDAP 設定
# ============================================================

# DMS用のLDAPプロバイダー
resource "authentik_provider_ldap" "dms" {
  name        = "DMS LDAP"
  base_dn     = "dc=ldap,dc=goauthentik,dc=io"
  bind_flow   = data.authentik_flow.default_authentication.id
  unbind_flow = data.authentik_flow.default_authentication.id
}

# DMS用のLDAPアプリケーション
resource "authentik_application" "dms_ldap" {
  name              = "DMS LDAP"
  slug              = "dms-ldap"
  protocol_provider = authentik_provider_ldap.dms.id
}

# クラスター内で稼働する LDAP Outpost の登録
resource "authentik_outpost" "dms_ldap" {
  name = "dms-ldap-outpost"
  type = "ldap"

  protocol_providers = [
    authentik_provider_ldap.dms.id
  ]

  # Outpost の設定値 (JSON形式の文字列)
  config = jsonencode({
    authentik_host          = var.authentik_url
    authentik_host_browser  = var.authentik_url
    authentik_host_insecure = false
    log_level               = "info"
  })
}
