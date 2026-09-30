# frontend (OpenNext on Workers) の ISR incremental cache。
# location は作成後に変更できないため、Workers の実行拠点に近い apac に固定する。
resource "cloudflare_r2_bucket" "aramakisai_web_cache" {
  account_id = var.cloudflare_account_id
  name       = "aramakisai-web-cache"
  location   = "APAC"
}
