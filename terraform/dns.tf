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

# Wiki.js (Cloudflare Tunnel)
resource "cloudflare_record" "wiki" {
  zone_id = var.cloudflare_zone_id
  name    = "wiki"
  value   = local.tunnel_cname
  type    = "CNAME"
  proxied = true
  comment = "Wiki.js (Cloudflare Tunnel)"
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

# ============================================================
# Workers Custom Domains
# ============================================================

# NOTE: このリポジトリがpinしているCloudflareプロバイダ(~> 4.0, 実体4.52.8)には
#       cloudflare_workers_custom_domain は存在しない(v5系以降のリソース名。
#       `terraform providers schema -json` で実際にスキーマを確認して判明)。
#       4.x系での正しいリソース名は cloudflare_workers_domain。
#       Cloudflare側で自動的にDNS/ルーティングを処理するため、同名のcloudflare_record
#       (CNAME) リソースは別途作成しないこと。
#
#       environment引数について: これはCloudflareのWorkers Services API上の環境名であり、
#       aramakisai-web側 wrangler.toml の `[env.dev]` (Wranglerが独自にworker名を
#       aramakisai-web-devへ変えるだけの仕組み)とは無関係な別概念。実際に
#       `GET /accounts/{id}/workers/services/aramakisai-web-dev` を叩いて確認したところ、
#       modern wrangler deploy でデプロイされたWorkerは常に "production" という単一の
#       環境のみを持つ(cloudflare/terraform-provider-cloudflare#5618 が指す既知の懸念と同種)。
#       そのため environment には "dev" ではなく "production" を指定する。
#       【代替手順】
#       もし apply 時に失敗する場合は、一旦このリソースをコメントアウトし、
#       Cloudflare ダッシュボード (Workers & Pages > 対象Worker > Triggers > Custom Domains)
#       から手動で `dev.aramakisai.com` を追加した上で、
#       `terraform import cloudflare_workers_domain.aramakisai_web_dev <account_id>/dev.aramakisai.com`
#       を実行して state に取り込むこと。

resource "cloudflare_workers_domain" "aramakisai_web_dev" {
  account_id  = var.cloudflare_account_id
  zone_id     = var.cloudflare_zone_id
  hostname    = "dev.aramakisai.com"
  service     = "aramakisai-web-dev"
  environment = "production"
}
