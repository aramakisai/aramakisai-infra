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

  # TCP 22 (SSH) は意図的に未定義
  # → Tailscale SSH を使用するため、パブリックインターネットからの SSH は不要
  #
  # K3s API (6443) / etcd (2379/2380) / kubelet (10250) も公開しない
  # → Tailscale ネットワーク内でのみアクセス
  #
  # Cloudflare Tunnel (cloudflared) はアウトバウンド接続のみ使用
  # → インバウンドルール不要
}
