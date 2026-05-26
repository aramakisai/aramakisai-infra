resource "hcloud_firewall" "k3s_nodes" {
  name = "k3s-nodes"

  labels = {
    project = "aramakisai"
    managed = "terraform"
  }

  # Tailscale: DERP リレー / NAT トラバーサル (UDP)
  rule {
    direction  = "in"
    protocol   = "udp"
    port       = "41641"
    source_ips = ["0.0.0.0/0", "::/0"]
    description = "Tailscale DERP / direct UDP"
  }

  # ICMP (ping / Path MTU Discovery)
  rule {
    direction  = "in"
    protocol   = "icmp"
    source_ips = ["0.0.0.0/0", "::/0"]
    description = "ICMP"
  }

  # ============================================================
  # Stalwart メールサーバー (hostPort 経由)
  #
  # StatefulSet に hostPort を設定し prod-node-1 に固定しているため、
  # 標準ポートをそのまま開放する。NodePort (30xxx) は不要。
  #
  # ⚠️  Hetzner はデフォルトでポート 25 をブロックしている。
  #     初回 apply 前に Hetzner サポートへ「ポート 25 開放申請」を行うこと。
  #     申請方法: https://docs.hetzner.com/cloud/servers/faq/#why-can-i-not-send-any-mails-from-my-server
  # ============================================================

  # SMTP 25: MTA 間の受信 (外部 MTA → Stalwart)
  # MX レコードはポートを指定できないため標準ポート 25 が必須
  rule {
    direction   = "in"
    protocol    = "tcp"
    port        = "25"
    source_ips  = ["0.0.0.0/0", "::/0"]
    description = "Stalwart SMTP"
  }

  # SMTP Submission 587: MUA → MTA (STARTTLS)
  rule {
    direction   = "in"
    protocol    = "tcp"
    port        = "587"
    source_ips  = ["0.0.0.0/0", "::/0"]
    description = "Stalwart Submission"
  }

  # SMTPS 465: MUA → MTA (Implicit TLS)
  rule {
    direction   = "in"
    protocol    = "tcp"
    port        = "465"
    source_ips  = ["0.0.0.0/0", "::/0"]
    description = "Stalwart SMTPS"
  }

  # IMAP 143: メールクライアント (STARTTLS)
  rule {
    direction   = "in"
    protocol    = "tcp"
    port        = "143"
    source_ips  = ["0.0.0.0/0", "::/0"]
    description = "Stalwart IMAP"
  }

  # IMAPS 993: メールクライアント (Implicit TLS)
  rule {
    direction   = "in"
    protocol    = "tcp"
    port        = "993"
    source_ips  = ["0.0.0.0/0", "::/0"]
    description = "Stalwart IMAPS"
  }

  # TCP 22 (SSH) は意図的に未定義
  # → Tailscale SSH を使用するため、パブリックインターネットからの SSH は不要
  #
  # K3s API (6443) / etcd (2379/2380) / kubelet (10250) も公開しない
  # → Tailscale ネットワーク内でのみアクセス
  #
  # Cloudflare Tunnel (cloudflared) はアウトバウンド接続のみ使用
  # → インバウンドルール不要
  #
  # Stalwart Web Admin (443) はインバウンドを開けない
  # → Cloudflare Tunnel 経由で mail-admin.aramakisai.com からのみアクセス
  # → TLS 証明書は Cloudflare DNS-01 ACME で取得 (ポート不要)
}
