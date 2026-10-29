# frontend のスタンプラリー記録 (Workers の D1 バインディング)。
# 本番と dev で DB を分け、PR プレビューは dev 側を使う。
resource "cloudflare_d1_database" "stamps" {
  account_id = var.cloudflare_account_id
  name       = "aramakisai-stamps"
}

resource "cloudflare_d1_database" "stamps_dev" {
  account_id = var.cloudflare_account_id
  name       = "aramakisai-stamps-dev"
}
