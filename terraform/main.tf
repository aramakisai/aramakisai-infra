locals {
  # ノード定義: 名前 → プライベート IP のマッピング
  nodes = {
    "cp-node"     = { private_ip = "10.0.1.1" }
    "prod-node-1" = { private_ip = "10.0.1.2" }
    "prod-node-2" = { private_ip = "10.0.1.3" }
  }
}

# ============================================================
# SSH キー
# ============================================================

resource "hcloud_ssh_key" "default" {
  name       = "k3s-deploy-key"
  public_key = var.ssh_public_key

  labels = {
    project = "aramakisai"
    managed = "terraform"
  }
}

# ============================================================
# Hetzner ノード (cx23: 2vCPU/4GB/40GB NVMe)
# ============================================================

resource "hcloud_server" "nodes" {
  for_each = local.nodes

  name         = each.key
  server_type  = "cx23"
  image        = var.hcloud_image
  location     = var.hcloud_location
  ssh_keys     = [hcloud_ssh_key.default.id]
  firewall_ids = [hcloud_firewall.k3s_nodes.id]

  # パブリックネットワーク設定を明示する
  # IPv4 を有効化。ダウンロードなど GitHub へのアクセスに必要。
  # ノード間通信は private network (10.0.1.0/24) を使用する。
  # 外部 SSH アクセスは Tailscale、外部 HTTP/SMTP は Cloudflare Tunnel。
  public_net {
    ipv4_enabled = true
    ipv6_enabled = true
  }

  # cloud-init: Tailscale インストール + tailnet 接続
  user_data = templatefile("${path.module}/templates/cloud-init.yaml.tpl", {
    tailscale_auth_key = tailscale_tailnet_key.k3s_nodes.key
    hostname           = each.key
  })

  # プライベートネットワークへの接続
  network {
    network_id = hcloud_network.main.id
    ip         = each.value.private_ip
  }

  labels = {
    project = "aramakisai"
    managed = "terraform"
    role    = each.key == "cp-node" ? "control-plane" : "worker"
  }

  depends_on = [
    hcloud_network_subnet.nodes,
    hcloud_firewall.k3s_nodes,
  ]
}

# ============================================================
# Ansible Bootstrap (Tailscale 接続確認後に実行)
# ============================================================

# TODO: Ansible bootstrap を手動実行に変更
# HCP Terraform リモート実行ではこのリソースが機能しないため、
# Tailscale にノードが登録された後、手動で以下を実行:
# cd ansible && ansible-playbook -i inventory/tailscale.yml playbooks/k3s-bootstrap.yml
#
# resource "null_resource" "ansible_bootstrap" {
#   # ノードが再作成 (taint) されたときのみ再実行する
#   triggers = {
#     cp_node_id     = hcloud_server.nodes["cp-node"].id
#     prod_node_1_id = hcloud_server.nodes["prod-node-1"].id
#     prod_node_2_id = hcloud_server.nodes["prod-node-2"].id
#   }
#
#   provisioner "local-exec" {
#     # 機密情報は environment ブロックで渡す (コマンド文字列に展開しない)
#     environment = {
#       ANSIBLE_HOST_KEY_CHECKING = "False"
#       K3S_TOKEN                 = var.k3s_token
#       CLOUDFLARE_TUNNEL_TOKEN   = cloudflare_zero_trust_tunnel_cloudflared.main.tunnel_token
#       CLOUDFLARE_TUNNEL_ID      = cloudflare_zero_trust_tunnel_cloudflared.main.id
#       TAILSCALE_API_KEY         = var.tailscale_api_key
#       TAILSCALE_TAILNET         = var.tailscale_tailnet
#     }
#
#     command = <<-EOT
#       set -e
#
#       echo "=== Waiting for K3s nodes to register in Tailscale ==="
#       for HOST in cp-node prod-node-1 prod-node-2; do
#         echo "Polling Tailscale API for $HOST..."
#         RETRIES=0
#         MAX_RETRIES=60
#         until curl -sf \
#           -H "Authorization: Bearer $TAILSCALE_API_KEY" \
#           "https://api.tailscale.com/api/v2/tailnet/$TAILSCALE_TAILNET/devices" \
#           | jq -e --arg h "$HOST" '.devices[] | select(.hostname == $h) | .addresses[0]' > /dev/null 2>&1; do
#           RETRIES=$((RETRIES + 1))
#           if [ $RETRIES -ge $MAX_RETRIES ]; then
#             echo "ERROR: $HOST did not register within timeout (5min)"
#             exit 1
#           fi
#           echo "  $HOST not yet registered (attempt $RETRIES/$MAX_RETRIES), retrying in 5s..."
#           sleep 5
#         done
#         echo "  $HOST registered."
#       done
#
#       echo "=== All nodes registered. Running Ansible... ==="
#       ansible-playbook \
#         -i ${path.root}/../ansible/inventory/tailscale.yml \
#         ${path.root}/../ansible/playbooks/k3s-bootstrap.yml
#     EOT
#   }
#
#   depends_on = [
#     hcloud_server.nodes,
#     cloudflare_zero_trust_tunnel_cloudflared.main,
#     cloudflare_zero_trust_tunnel_cloudflared_config.main,
#   ]
# }
