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

# SPF: mail.aramakisai.com (MX) からの送信を許可
resource "cloudflare_record" "spf" {
  zone_id = var.cloudflare_zone_id
  name    = "@"
  value   = "v=spf1 mx ~all"
  type    = "TXT"
  proxied = false
  comment = "SPF record"
}

# DMARC: SPF/DKIM 失敗時は quarantine、集計レポートを dmarc@ に送信
resource "cloudflare_record" "dmarc" {
  zone_id = var.cloudflare_zone_id
  name    = "_dmarc"
  value   = "v=DMARC1; p=quarantine; rua=mailto:dmarc@aramakisai.com; ruf=mailto:dmarc@aramakisai.com; fo=1"
  type    = "TXT"
  proxied = false
  comment = "DMARC record"
}

# DKIM: Stalwart 初回起動後に管理画面 (mail-admin.aramakisai.com) から
#       DKIM 鍵ペアを生成し、表示される TXT レコードをここに追加する
#
# 追加例:
# resource "cloudflare_record" "dkim" {
#   zone_id = var.cloudflare_zone_id
#   name    = "mail._domainkey"
#   value   = "v=DKIM1; k=rsa; p=<Stalwart が生成した公開鍵>"
#   type    = "TXT"
#   proxied = false
#   comment = "DKIM record (Stalwart)"
# }
