output "cp_node_ip" {
  description = "cp-node のパブリック IPv4 アドレス"
  value       = hcloud_server.nodes["cp-node"].ipv4_address
}

output "prod_node_1_ip" {
  description = "prod-node-1 のパブリック IPv4 アドレス"
  value       = hcloud_server.nodes["prod-node-1"].ipv4_address
}

output "prod_node_2_ip" {
  description = "prod-node-2 のパブリック IPv4 アドレス"
  value       = hcloud_server.nodes["prod-node-2"].ipv4_address
}

output "tailscale_auth_key" {
  description = "Tailscale auth key (機密情報 / apply 直後のみ必要)"
  value       = tailscale_tailnet_key.k3s_nodes.key
  sensitive   = true
}

output "tunnel_id" {
  description = "Cloudflare Tunnel ID (gitops マニフェスト等で参照)"
  value       = cloudflare_tunnel.main.id
}

output "tunnel_token" {
  description = "Cloudflare Tunnel トークン (cloudflared 起動に必要 / 機密情報)"
  value       = cloudflare_tunnel.main.tunnel_token
  sensitive   = true
}

output "object_storage_bucket" {
  description = "Hetzner Object Storage バケット名"
  value       = hcloud_object_storage_bucket.backups.name
}
