# ============================================================
# Hetzner Object Storage
#
# 【注意】hetznercloud/hcloud provider は hcloud_object_storage_bucket を
#         サポートしていない。バケットは Hetzner Robot ダッシュボードで手動作成し、
#         アクセスキーを Infisical に保存すること。
#
# 用途: Velero / Longhorn のバックアップ保存先 (S3 互換 API)
# エンドポイント: https://<location>.your-objectstorage.com
# バケット名: aramakisai-backups
# ============================================================

# resource "hcloud_object_storage_bucket" "backups" {
#   name     = "aramakisai-backups"
#   location = var.hcloud_location
# }
