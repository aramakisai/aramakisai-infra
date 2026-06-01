#cloud-config
# Hetzner ノード初期設定: ホスト名設定 + Tailscale インストール・tailnet 接続

hostname: ${hostname}
fqdn: ${hostname}

# 依存パッケージ (Tailscale インストール用の最小限)
packages:
  - curl
  - jq

package_update: false

runcmd:
  # Tailscale インストール
  - curl -fsSL https://tailscale.com/install.sh | sh

  # tailnet に接続
  - tailscale up --auth-key="${tailscale_auth_key}" --hostname="${hostname}" --accept-routes

  # ホスト名を永続化
  - hostnamectl set-hostname "${hostname}"
