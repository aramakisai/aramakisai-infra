output "cp_node_ipv6" {
  description = "cp-node のパブリック IPv6 アドレス (IPv4 は無効)"
  value       = hcloud_server.nodes["cp-node"].ipv6_address
}

output "prod_node_1_ipv6" {
  description = "prod-node-1 のパブリック IPv6 アドレス (mail.aramakisai.com AAAA)"
  value       = hcloud_server.nodes["prod-node-1"].ipv6_address
}

output "prod_node_2_ipv6" {
  description = "prod-node-2 のパブリック IPv6 アドレス"
  value       = hcloud_server.nodes["prod-node-2"].ipv6_address
}

output "tailscale_auth_key" {
  description = "Tailscale auth key (機密情報 / apply 直後のみ必要)"
  value       = tailscale_tailnet_key.k3s_nodes.key
  sensitive   = true
}

output "tunnel_id" {
  description = "Cloudflare Tunnel ID (gitops マニフェスト等で参照)"
  value       = cloudflare_zero_trust_tunnel_cloudflared.main.id
}

output "tunnel_token" {
  description = "Cloudflare Tunnel トークン (cloudflared 起動に必要 / 機密情報)"
  value       = cloudflare_zero_trust_tunnel_cloudflared.main.tunnel_token
  sensitive   = true
}

# Hetzner Object Storage は hcloud provider 非対応のため手動管理
# バケット名: aramakisai-backups
# output "object_storage_bucket" {
#   description = "Hetzner Object Storage バケット名"
#   value       = hcloud_object_storage_bucket.backups.name
# }
