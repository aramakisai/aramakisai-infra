# ============================================================
# Hetzner Object Storage
#
# 【注意】hetznercloud/hcloud provider は hcloud_object_storage_bucket を
#         サポートしていない。バケットは Hetzner Robot ダッシュボードで手動作成し、
#         アクセスキーを Infisical に保存すること。
#
# 用途: CNPG WAL アーカイブ + VolSync restic バックアップ + Directus アセット (directus-uploads/)
# エンドポイント: https://fsn1.your-objectstorage.com
# バケット名: aramakisai-backups
# ============================================================

# resource "hcloud_object_storage_bucket" "backups" {
#   name     = "aramakisai-backups"
#   location = var.hcloud_location
# }
