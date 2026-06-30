locals {
  # Cloudflare Tunnel への CNAME ターゲット
  tunnel_cname = "${cloudflare_zero_trust_tunnel_cloudflared.main.id}.cfargotunnel.com"
}

# ArgoCD UI
resource "cloudflare_record" "argocd" {
  zone_id = var.cloudflare_zone_id
  name    = "argocd"
  value   = local.tunnel_cname
  type    = "CNAME"
  proxied = true
  comment = "ArgoCD UI (Cloudflare Tunnel)"
}

# Authentik IdP
resource "cloudflare_record" "idp" {
  zone_id = var.cloudflare_zone_id
  name    = "idp"
  value   = local.tunnel_cname
  type    = "CNAME"
  proxied = true
  comment = "Authentik IdP (Cloudflare Tunnel)"
}

# Production フロントエンド (Cloudflare Pages)
# CNAME Flattening により apex でも CNAME が使用可能
# TODO: フロントエンド移行完了後にコメントアウトを解除して apply する
# resource "cloudflare_record" "apex" {
#   zone_id = var.cloudflare_zone_id
#   name    = "@"
#   value   = "aramakisai-web.pages.dev"
#   type    = "CNAME"
#   proxied = true
#   comment = "Production frontend (Cloudflare Pages)"
# }

# Staging フロントエンドは廃止 — Cloudflare Pages PR preview URL (*.pages.dev) を使用

# Staging API
# api.stg (2階層) は Cloudflare Universal SSL のカバー範囲外 (TLS handshake failure) のため
# stg-api (1階層、*.aramakisai.com ワイルドカードでカバー) に変更
resource "cloudflare_record" "api_stg" {
  zone_id = var.cloudflare_zone_id
  name    = "stg-api"
  value   = local.tunnel_cname
  type    = "CNAME"
  proxied = true
  comment = "Staging API (Cloudflare Tunnel)"
}

# Room Presence Tracker (実行委員室 在室管理)
resource "cloudflare_record" "presence" {
  zone_id = var.cloudflare_zone_id
  name    = "presence"
  value   = local.tunnel_cname
  type    = "CNAME"
  proxied = true
  comment = "Room Presence Tracker (Cloudflare Tunnel)"
}

# Vaultwarden (Password Manager)
resource "cloudflare_record" "vault" {
  zone_id = var.cloudflare_zone_id
  name    = "vault"
  value   = local.tunnel_cname
  type    = "CNAME"
  proxied = true
  comment = "Vaultwarden (Cloudflare Tunnel)"
}

# Production API (Directus)
resource "cloudflare_record" "api" {
  zone_id = var.cloudflare_zone_id
  name    = "api"
  value   = local.tunnel_cname
  type    = "CNAME"
  proxied = true
  comment = "Directus API (Cloudflare Tunnel)"
}

# ============================================================
# Stalwart メールサーバー
# ============================================================

# mail.aramakisai.com → prod-node-1 のみ (A + AAAA)
#
# StatefulSet は hostNetwork を使用しており、Pod が稼働するノードのみがポート
# 25/587/465/143/993/443 に応答する。StatefulSet は prod-node-1 に固定
# (statefulset.yaml の nodeSelector: kubernetes.io/hostname: prod-node-1) して
# あるため、DNS も prod-node-1 の IP 1 件のみを指定する。
#
# proxied = false: Cloudflare プロキシは SMTP/IMAP を中継できない
resource "cloudflare_record" "mail_prod_node_1" {
  zone_id = var.cloudflare_zone_id
  name    = "mail"
  value   = hcloud_server.nodes["prod-node-1"].ipv6_address
  type    = "AAAA"
  proxied = false
  comment = "Stalwart mail IPv6 (prod-node-1 固定)"
}

resource "cloudflare_record" "mail_prod_node_1_ipv4" {
  zone_id = var.cloudflare_zone_id
  name    = "mail"
  value   = hcloud_server.nodes["prod-node-1"].ipv4_address
  type    = "A"
  proxied = false
  comment = "Stalwart mail IPv4 (prod-node-1 固定)"
}

# PTR (rDNS): Hetzner が管理する逆引き DNS。外部 MTA がスパム判定に使用する。
resource "hcloud_rdns" "mail_ipv4" {
  server_id  = hcloud_server.nodes["prod-node-1"].id
  ip_address = hcloud_server.nodes["prod-node-1"].ipv4_address
  dns_ptr    = "mail.aramakisai.com"
}

resource "hcloud_rdns" "mail_ipv6" {
  server_id  = hcloud_server.nodes["prod-node-1"].id
  ip_address = hcloud_server.nodes["prod-node-1"].ipv6_address
  dns_ptr    = "mail.aramakisai.com"
}

# MX レコード: aramakisai.com のメールを mail.aramakisai.com に転送
resource "cloudflare_record" "mx" {
  zone_id  = var.cloudflare_zone_id
  name     = "@"
  value    = "mail.aramakisai.com"
  type     = "MX"
  priority = 10
  proxied  = false
  comment  = "Stalwart MX record"
}

# SPF (ドメイン全体): MX で許可されたサーバーからの送信を許可
resource "cloudflare_record" "spf" {
  zone_id = var.cloudflare_zone_id
  name    = "@"
  value   = "v=spf1 mx -all"
  type    = "TXT"
  proxied = false
  comment = "SPF record"
}

# SPF (mail サブドメイン): mail.aramakisai.com 自体からの送信を許可
resource "cloudflare_record" "spf_mail" {
  zone_id = var.cloudflare_zone_id
  name    = "mail"
  value   = "v=spf1 a -all"
  type    = "TXT"
  proxied = false
  comment = "SPF record for mail subdomain (Stalwart)"
}

# DMARC: SPF/DKIM 失敗時は reject、集計・フォレンジックレポートを postmaster@ に送信
resource "cloudflare_record" "dmarc" {
  zone_id = var.cloudflare_zone_id
  name    = "_dmarc"
  value   = "v=DMARC1; p=reject; rua=mailto:postmaster@aramakisai.com; ruf=mailto:postmaster@aramakisai.com" # confidential:allow
  type    = "TXT"
  proxied = false
  comment = "DMARC record"
}

# ============================================================
# DKIM 署名鍵 (DMS 用)
# ============================================================

resource "cloudflare_record" "dkim" {
  zone_id = var.cloudflare_zone_id
  name    = "mail._domainkey"
  value   = "v=DKIM1; h=sha256; k=rsa; p=MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA5B0r+Gw8taECJlZrSjeqpu7ofGRvKguAY03NgLIOdRctFfX95Dn0/IF+ujCGPC7u5qFaPw/AomifYmuZ9f4+eNtQCu3IkJdLiF1SEQHgfWZKezPGmeH+Bd7VyvjkVCJZe2KJNiN4HNc0pCXRt437mSr2xid1pug4niUUe3wiwDveio75+tOpAxdP4sbgmd6/RJbtKhSF/cZ1HCKe/yEjN59a3f1ebzOtJTGImEbz6jvb8IB7U3F82tBKaCSpp11jl8P3Z+SmmcPG278D4E5o2/W0YpwDkqxafMzhqH2yG4LR9spoq4szxQaExHpYeg2pJZU9w5KzpR+0zi5U7WM0AQIDAQAB"
  type    = "TXT"
  proxied = false
  comment = "DKIM public key TXT record for DMS"
}

# ============================================================
# Resend バウンスドメイン (send.aramakisai.com)
# ============================================================
#
# Resend のバウンスアドレスは <id>@send.aramakisai.com 形式。
# MX レコードがないと NO_DNS_FOR_FROM でスパムスコアが悪化し、
# SPF がないと SPF_NONE で認証失敗になる。

resource "cloudflare_record" "send_mx" {
  zone_id  = var.cloudflare_zone_id
  name     = "send"
  value    = "feedback-smtp.ap-northeast-1.amazonses.com"
  type     = "MX"
  priority = 10
  proxied  = false
  comment  = "Resend bounce domain MX (SES feedback SMTP)"
}

resource "cloudflare_record" "send_spf" {
  zone_id = var.cloudflare_zone_id
  name    = "send"
  value   = "v=spf1 include:amazonses.com ~all"
  type    = "TXT"
  proxied = false
  comment = "SPF for Resend bounce domain"
}

# ============================================================
# SMTP TLS レポート
# ============================================================

resource "cloudflare_record" "smtp_tls" {
  zone_id = var.cloudflare_zone_id
  name    = "_smtp._tls"
  value   = "v=TLSRPTv1; rua=mailto:postmaster@aramakisai.com" # confidential:allow
  type    = "TXT"
  proxied = false
  comment = "SMTP TLS reporting (RFC 8460)"
}

# ============================================================
# メールクライアント自動設定 (autoconfig / autodiscover)
# ============================================================

# Roundcube Webmail: https://webmail.aramakisai.com
# Roundcube 自身が Authentik OAuth2 で保護 → Tunnel → roundcube Pod
resource "cloudflare_record" "webmail" {
  zone_id = var.cloudflare_zone_id
  name    = "webmail"
  value   = local.tunnel_cname
  type    = "CNAME"
  proxied = true
  comment = "Roundcube Webmail (Cloudflare Tunnel)"
}

# Thunderbird 系: https://autoconfig.aramakisai.com/mail/config-v1.1.xml
resource "cloudflare_record" "autoconfig" {
  zone_id = var.cloudflare_zone_id
  name    = "autoconfig"
  value   = "mail.aramakisai.com"
  type    = "CNAME"
  proxied = false
  comment = "Thunderbird autoconfig (Stalwart)"
}

# Outlook 系: https://autodiscover.aramakisai.com/autodiscover/autodiscover.xml
resource "cloudflare_record" "autodiscover" {
  zone_id = var.cloudflare_zone_id
  name    = "autodiscover"
  value   = "mail.aramakisai.com"
  type    = "CNAME"
  proxied = false
  comment = "Outlook autodiscover (Stalwart)"
}

# ============================================================
# SRV レコード (メールクライアント自動設定)
# ============================================================

resource "cloudflare_record" "srv_imaps" {
  zone_id = var.cloudflare_zone_id
  name    = "_imaps._tcp"
  type    = "SRV"
  proxied = false
  data {
    priority = 0
    weight   = 1
    port     = 993
    target   = "mail.aramakisai.com"
  }
  comment = "IMAPS SRV (Mailserver)"
}

resource "cloudflare_record" "srv_imap" {
  zone_id = var.cloudflare_zone_id
  name    = "_imap._tcp"
  type    = "SRV"
  proxied = false
  data {
    priority = 0
    weight   = 1
    port     = 143
    target   = "mail.aramakisai.com"
  }
  comment = "IMAP SRV (Mailserver)"
}

resource "cloudflare_record" "srv_submissions" {
  zone_id = var.cloudflare_zone_id
  name    = "_submissions._tcp"
  type    = "SRV"
  proxied = false
  data {
    priority = 0
    weight   = 1
    port     = 465
    target   = "mail.aramakisai.com"
  }
  comment = "SMTPS SRV (Mailserver)"
}

resource "cloudflare_record" "srv_submission" {
  zone_id = var.cloudflare_zone_id
  name    = "_submission._tcp"
  type    = "SRV"
  proxied = false
  data {
    priority = 0
    weight   = 1
    port     = 587
    target   = "mail.aramakisai.com"
  }
  comment = "SMTP Submission SRV (Mailserver)"
}
