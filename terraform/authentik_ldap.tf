# ============================================================
# Docker Mailserver (DMS) 連携用 LDAP 設定
# ============================================================

# LDAP bind専用の認証フロー
#
# default-authentication-flow (Web/SSOログイン共通) は authentication = "none"
# のため、LDAP Outpost経由のflow実行では常に Invalid credentials (49) を返す
# 既知の不具合がある (goauthentik/authentik#14210)。flowの "Authentication"
# 設定を require_outpost にする必要があり、Web用の共有flowを直接変更すると
# 人間のログインが壊れるため、LDAP bind専用の最小フロー(identification+
# passwordのみ、MFAステージなし)を別途用意する。
resource "authentik_flow" "ldap_bind" {
  name        = "LDAP Bind Flow"
  slug        = "ldap-bind-flow"
  title       = "LDAP Bind"
  designation = "authentication"
  # require_outpost は別途Outpost側トークン認識の問題を引き起こした
  # (Authentikログ: "Attempted remote-ip override without token")。
  # 元のdefault-authentication-flowも authentication = none だったため、
  # ここでは同じ none に揃える (本不具合の本質はこの設定値ではなかった)。
  authentication = "none"
}

resource "authentik_stage_identification" "ldap_bind_identification" {
  name        = "ldap-bind-identification-stage"
  user_fields = ["username"]
}

resource "authentik_stage_password" "ldap_bind_password" {
  name = "ldap-bind-password-stage"

  backends = [
    "authentik.core.auth.InbuiltBackend"
  ]
}

resource "authentik_flow_stage_binding" "ldap_bind_identification_bind" {
  target = authentik_flow.ldap_bind.uuid
  stage  = authentik_stage_identification.ldap_bind_identification.id
  order  = 10
}

resource "authentik_flow_stage_binding" "ldap_bind_password_bind" {
  target = authentik_flow.ldap_bind.uuid
  stage  = authentik_stage_password.ldap_bind_password.id
  order  = 20
}

# DMS用のLDAPプロバイダー
resource "authentik_provider_ldap" "dms" {
  name        = "DMS LDAP"
  base_dn     = "dc=ldap,dc=goauthentik,dc=io"
  bind_flow   = authentik_flow.ldap_bind.uuid # target には UUID が必要なため uuid を使用
  unbind_flow = authentik_flow.ldap_bind.uuid
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
