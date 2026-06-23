output "prod_node_1_ipv6" {
  description = "prod-node-1 のパブリック IPv6 アドレス (mail.aramakisai.com AAAA)"
  value       = hcloud_server.nodes["prod-node-1"].ipv6_address
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

output "healthchecksio_mailserver_backup_ping_url" {
  description = "Healthchecks.io mailserver-backup check の ping URL (Infisical の HEALTHCHECKS_MAILSERVER_BACKUP_PING_URL へ反映 / 機密情報)"
  value       = healthchecksio_check.mailserver_backup.ping_url
  sensitive   = true
}

output "netdata_room_id" {
  description = "Netdata Cloud aramakisai-prod Room ID (Infisical の NETDATA_CLAIM_ROOMS へ反映)"
  value       = netdata_room.prod.id
}

output "vaultwarden_rbac_sync_authentik_token" {
  description = "vaultwarden-rbac-sync用Authentik APIトークン (apply後にInfisicalのVAULTWARDEN_RBAC_SYNC_AUTHENTIK_API_TOKENへ手動反映 / 機密情報)"
  value       = authentik_token.vaultwarden_rbac_sync.key
  sensitive   = true
}

# Hetzner Object Storage は hcloud provider 非対応のため手動管理
# バケット名: aramakisai-backups
# output "object_storage_bucket" {
#   description = "Hetzner Object Storage バケット名"
#   value       = hcloud_object_storage_bucket.backups.name
# }
