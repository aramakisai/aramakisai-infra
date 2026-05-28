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

# Staging フロントエンド
resource "cloudflare_record" "stg" {
  zone_id = var.cloudflare_zone_id
  name    = "stg"
  value   = local.tunnel_cname
  type    = "CNAME"
  proxied = true
  comment = "Staging frontend (Cloudflare Tunnel)"
}

# Staging API
resource "cloudflare_record" "api_stg" {
  zone_id = var.cloudflare_zone_id
  name    = "api.stg"
  value   = local.tunnel_cname
  type    = "CNAME"
  proxied = true
  comment = "Staging API (Cloudflare Tunnel)"
}

# ============================================================
# Stalwart メールサーバー
# ============================================================

# mail.aramakisai.com → prod-node-1 のみ (シングル A レコード)
#
# StatefulSet は hostPort を使用しており、Pod が稼働するノードのみがポート
# 25/587/465/143/993 に応答する。StatefulSet は prod-node-1 に固定
# (statefulset.yaml の nodeSelector: kubernetes.io/hostname: prod-node-1) して
# あるため、DNS も prod-node-1 の IP 1 件のみを指定する。
#
# ラウンドロビン DNS は使用しない:
# replicas: 1 の StatefulSet では Pod が 1 台のノードにしか存在しないため、
# 他のノードへの接続はすべてタイムアウトになるため。
#
# proxied = false: Cloudflare プロキシは SMTP/IMAP を中継できない
resource "cloudflare_record" "mail_prod_node_1" {
  zone_id = var.cloudflare_zone_id
  name    = "mail"
  value   = hcloud_server.nodes["prod-node-1"].ipv6_address
  type    = "AAAA"
  proxied = false
  comment = "Stalwart mail IPv6 (prod-node-1 固定 / hostPort)"
}

# ⚠️  terraform apply 後に Hetzner PTR (rDNS) の設定が必要
# IPv6 は apply でサーバーを作成して初めて割り当てられるため、apply 後に実施する。
#
# 手順:
#   1. terraform apply 完了後
#   2. terraform output prod_node_1_ipv6 で IPv6 アドレスを確認
#   3. Hetzner Console → Servers → prod-node-1 → Networking → IPv6 → PTR
#      <IPv6 アドレス> → mail.aramakisai.com を設定
#
# PTR レコードがないと外部 MTA (Gmail 等) に spam 判定されメールが届かない

# mail-admin.aramakisai.com → Cloudflare Tunnel (Web Admin UI)
# proxied = true: Cloudflare が TLS を終端する
resource "cloudflare_record" "mail_admin" {
  zone_id = var.cloudflare_zone_id
  name    = "mail-admin"
  value   = local.tunnel_cname
  type    = "CNAME"
  proxied = true
  comment = "Stalwart Web Admin (Cloudflare Tunnel)"
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
# ra=postmaster: SPF 失敗の集計レポートを postmaster@ に送信 (Stalwart 推奨)
resource "cloudflare_record" "spf" {
  zone_id = var.cloudflare_zone_id
  name    = "@"
  value   = "v=spf1 mx ra=postmaster -all"
  type    = "TXT"
  proxied = false
  comment = "SPF record"
}

# SPF (mail サブドメイン): mail.aramakisai.com 自体からの送信を許可
resource "cloudflare_record" "spf_mail" {
  zone_id = var.cloudflare_zone_id
  name    = "mail"
  value   = "v=spf1 a ra=postmaster -all"
  type    = "TXT"
  proxied = false
  comment = "SPF record for mail subdomain (Stalwart)"
}

# DMARC: SPF/DKIM 失敗時は reject、集計・フォレンジックレポートを postmaster@ に送信
resource "cloudflare_record" "dmarc" {
  zone_id = var.cloudflare_zone_id
  name    = "_dmarc"
  value   = "v=DMARC1; p=reject; rua=mailto:postmaster@aramakisai.com; ruf=mailto:postmaster@aramakisai.com"
  type    = "TXT"
  proxied = false
  comment = "DMARC record"
}

# ============================================================
# DKIM 署名鍵
# ============================================================
#
# v0.16.6 の初回起動時に DB が初期化され、旧秘密鍵 (202605e / 202605r) が消失した。
# 秘密鍵のない公開鍵レコードは無意味なため、Terraform 管理から削除する。
#
# 新しい DKIM レコードは Stalwart の dkimManagement: Automatic + dnsManagement が
# Cloudflare API 経由で自動登録する (gitops/manifests/prod/stalwart/settings-configmap.yaml 参照)。
# Stalwart が登録するレコードは Terraform state には含まれないため drift は発生しない。
#
# 削除手順:
#   terraform apply \
#     -target cloudflare_record.dkim_ed25519 \
#     -target cloudflare_record.dkim_rsa
# (リソース定義を削除した後に apply すると Cloudflare から削除される)

# ============================================================
# SMTP TLS レポート
# ============================================================

resource "cloudflare_record" "smtp_tls" {
  zone_id = var.cloudflare_zone_id
  name    = "_smtp._tls"
  value   = "v=TLSRPTv1; rua=mailto:postmaster@aramakisai.com"
  type    = "TXT"
  proxied = false
  comment = "SMTP TLS reporting (RFC 8460)"
}

# ============================================================
# メールクライアント自動設定 (autoconfig / autodiscover)
# ============================================================

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
  comment = "IMAPS SRV (Stalwart)"
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
  comment = "IMAP SRV (Stalwart)"
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
  comment = "SMTPS SRV (Stalwart)"
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
  comment = "SMTP Submission SRV (Stalwart)"
}

resource "cloudflare_record" "srv_jmap" {
  zone_id = var.cloudflare_zone_id
  name    = "_jmap._tcp"
  type    = "SRV"
  proxied = false
  data {
    priority = 0
    weight   = 1
    port     = 443
    target   = "mail.aramakisai.com"
  }
  comment = "JMAP SRV (Stalwart)"
}

resource "cloudflare_record" "srv_caldavs" {
  zone_id = var.cloudflare_zone_id
  name    = "_caldavs._tcp"
  type    = "SRV"
  proxied = false
  data {
    priority = 0
    weight   = 1
    port     = 443
    target   = "mail.aramakisai.com"
  }
  comment = "CalDAV SRV (Stalwart)"
}

resource "cloudflare_record" "srv_carddavs" {
  zone_id = var.cloudflare_zone_id
  name    = "_carddavs._tcp"
  type    = "SRV"
  proxied = false
  data {
    priority = 0
    weight   = 1
    port     = 443
    target   = "mail.aramakisai.com"
  }
  comment = "CardDAV SRV (Stalwart)"
}
