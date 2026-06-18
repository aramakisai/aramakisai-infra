# ============================================================
# Hetzner Cloud
# ============================================================

variable "hcloud_location" {
  description = "Hetzner データセンターロケーション (fsn1/nbg1/hel1)"
  type        = string
  default     = "fsn1"
}

variable "hcloud_image" {
  description = "ノードのベース OS イメージ"
  type        = string
  default     = "debian-13"
}

variable "ssh_public_key" {
  description = "ノードに登録する SSH 公開鍵 (cat ~/.ssh/id_ed25519.pub)"
  type        = string
}

# ============================================================
# Tailscale
# ============================================================

variable "tailscale_tailnet" {
  description = "Tailnet 名 (Tailscale admin console の Organization name)"
  type        = string
  default     = "aramakisai.com"
}

variable "tailscale_tags" {
  description = "Tailscale ノードに付与する ACL タグ (tag: prefix 込みで指定)"
  type        = list(string)
  default     = ["tag:k3s-node"]
}

variable "tailscale_api_key" {
  description = "Tailscale API キー (null_resource の Tailscale ポーリングで使用)"
  type        = string
  sensitive   = true
  default     = ""
  # 環境変数 TF_VAR_tailscale_api_key または TAILSCALE_API_KEY から設定
}

# ============================================================
# Cloudflare
# ============================================================

variable "cloudflare_zone_id" {
  description = "aramakisai.com の Cloudflare Zone ID"
  type        = string
}

variable "cloudflare_account_id" {
  description = "Cloudflare アカウント ID"
  type        = string
}

variable "cf_tunnel_secret" {
  description = "Cloudflare Tunnel のシークレット (openssl rand -base64 32 で生成)"
  type        = string
  sensitive   = true
}

# ============================================================
# Cloudflare Access × Authentik OIDC
# ============================================================

variable "authentik_cf_client_id" {
  description = "Authentik OIDC Client ID (Cloudflare Access IdP 登録用)"
  type        = string
  sensitive   = true
  default     = ""
  # Authentik はクラスター上で動くため初回 apply 時は空でよい
  # Authentik セットアップ後に HCP Terraform ワークスペースで設定して再 apply する
}

variable "authentik_cf_client_secret" {
  description = "Authentik OIDC Client Secret"
  type        = string
  sensitive   = true
  default     = ""
  # 同上
}

# ============================================================
# K3s
# ============================================================

variable "k3s_token" {
  description = "K3s クラスタ参加トークン (openssl rand -hex 32 で生成)"
  type        = string
  sensitive   = true
  # 環境変数 TF_VAR_k3s_token で渡す
}

# ============================================================
# Authentik IaC
# ============================================================

variable "authentik_url" {
  description = "Authentik API Endpoint URL"
  type        = string
  default     = "https://idp.aramakisai.com"
}

variable "authentik_token" {
  description = "Authentik Admin API Token"
  type        = string
  sensitive   = true
  default     = ""
}

variable "discord_client_id" {
  description = "Discord OAuth2 Client ID"
  type        = string
  default     = ""
}

variable "discord_client_secret" {
  description = "Discord OAuth2 Client Secret"
  type        = string
  sensitive   = true
  default     = ""
}

variable "roundcube_oauth2_client_secret" {
  description = "Roundcube OAuth2 Client Secret (aramakisai-mail)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "argocd_oidc_client_secret" {
  description = "ArgoCD OIDC Client Secret (argocd)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "authentik_room_presence_client_secret" {
  description = "Room Presence Tracker OIDC Client Secret (aramakisai-room-presence)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "cloudflare_access_redirect_uris" {
  description = "Cloudflare Access OIDC Redirect URIs"
  type        = list(string)
  default     = ["https://aramakisai.cloudflareaccess.com/cdn-cgi/access/callback"]
}

variable "discord_guild_id" {
  description = "Discord Server/Guild ID to sync roles and membership from"
  type        = string
  default     = ""
}

variable "mailserver_ldap_bind_password" {
  description = "LDAP Bind Password for DMS service account"
  type        = string
  sensitive   = true
  default     = ""
}

# ============================================================
# Observability SaaS (UptimeRobot / Healthchecks.io / Netdata Cloud)
# ============================================================

variable "uptimerobot_api_key" {
  description = "UptimeRobot API キー (UptimeRobot ダッシュボード → My Settings → API Settings で発行)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "healthchecksio_api_key" {
  description = "Healthchecks.io API キー (Healthchecks.io → Settings → API Access で発行)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "netdata_api_token" {
  description = "Netdata Cloud API トークン (scope:all、Netdata Cloud → Space Settings → API Tokens で発行)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "netdata_space_id" {
  description = "Netdata Cloud Space ID (Netdata Cloud コンソールで作成済みの Space を指定)"
  type        = string
  default     = ""
}
