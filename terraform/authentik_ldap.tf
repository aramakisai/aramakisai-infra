# ============================================================
# Docker Mailserver (DMS) 連携用 LDAP 設定
# ============================================================

# DMS用のLDAPプロバイダー
resource "authentik_provider_ldap" "dms" {
  name        = "DMS LDAP"
  base_dn     = "dc=ldap,dc=goauthentik,dc=io"
  bind_flow   = data.authentik_flow.default_authentication.id
  unbind_flow = data.authentik_flow.default_authentication.id
  # bind_flow (default-authentication-flow) には Authenticator Validation
  # ステージが含まれる。LDAP outpostのdirect binderはこのステージを
  # 正しく通過できず Invalid credentials (49) を返す既知の不具合がある
  # (goauthentik/authentik#4729, #10571)。MFA自体はLDAP bind経由では
  # 元々サポート対象外の運用のため、provider側で明示的に無効化する。
  mfa_support = false
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
    # authentik_host: Outpost → Authentik API のエンドポイント。
    # クラスター内通信のため Service 経由 (cluster.local) を使う。
    # 外部URL (Cloudflare Tunnel経由) を指定すると、LDAP bind のたびに
    # インターネット往復が発生しレイテンシが数秒〜数十秒に悪化する
    # (Postfix の virtual_alias_maps LDAP lookup がタイムアウトしメール送信不可になる)
    authentik_host          = "http://authentik-server.prod.svc.cluster.local"
    authentik_host_browser  = var.authentik_url
    authentik_host_insecure = false
    log_level               = "info"
  })
}

# DMS LDAP 検索・バインド用ユーザー
resource "authentik_user" "dms_service" {
  username = "mailserver-service"
  name     = "DMS LDAP Service Account"
  password = var.mailserver_ldap_bind_password
  attributes = jsonencode({
    mailAlias = []
  })
}
