resource "tailscale_tailnet_key" "k3s_nodes" {
  reusable      = true  # 3ノードを1つのキーで処理するため
  ephemeral     = false # ノードをデバイスリストに永続登録 (MagicDNS 維持のため)
  preauthorized = true  # 管理者承認なしに tailnet に接続できる
  expiry        = 3600  # auth key 自体は1時間で失効 (apply 後は不要)
  tags          = var.tailscale_tags

  description = "K3s bootstrap key managed expires 1h"
}

# 注意:
#   - expiry=3600 は auth key の有効期限。デバイス登録自体 (ephemeral=false) は永続。
#   - terraform apply のたびに新しいキーが発行される (既存ノードは再接続不要)。
#   - Tailscale ACL で var.tailscale_tags に対応するタグを事前に定義すること。
#     例: "tag:k3s-node" を ACL の tagOwners に追加
