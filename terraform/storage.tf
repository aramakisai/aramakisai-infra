resource "hcloud_object_storage_bucket" "backups" {
  name     = "aramakisai-backups"
  location = var.hcloud_location

  # 用途: Velero / Longhorn のバックアップ保存先 (S3 互換 API)
  # エンドポイント: https://<location>.your-objectstorage.com
  # アクセスキーは Hetzner Robot ダッシュボードで発行し Infisical に保存すること
}
