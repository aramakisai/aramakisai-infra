# ============================================================
# Cloudflare Pages Project
#
# デプロイは GitHub Actions + wrangler pages deploy が担う。
# source ブロック (GitHub App 連携) は不要。
#
# 既存プロジェクトが Cloudflare 上に存在する場合のインポート:
#   terraform import cloudflare_pages_project.aramakisai_web <account_id>/aramakisai-web
# ============================================================

resource "cloudflare_pages_project" "aramakisai_web" {
  account_id        = var.cloudflare_account_id
  name              = "aramakisai-web"
  production_branch = "main"

  build_config {
    build_command   = "pnpm run build"
    destination_dir = ".vercel/output/static"
    root_dir        = "frontend"
  }

  deployment_configs {
    production {
      environment_variables = {
        NODE_VERSION = "22"
      }
    }
    preview {
      environment_variables = {
        NODE_VERSION = "22"
      }
    }
  }
}

# ============================================================
# Cloudflare Pages Custom Domain
#
# TLS 証明書発行は apply 後に Cloudflare 側で非同期処理（通常数分）。
# apply 直後に https://aramakisai.com が応答しない場合は数分待機すること。
# TODO: フロントエンド移行完了後に dns.tf の apex record と同時にコメントアウトを解除して apply する
# ============================================================

# resource "cloudflare_pages_domain" "aramakisai_web_prod" {
#   account_id   = var.cloudflare_account_id
#   project_name = cloudflare_pages_project.aramakisai_web.name
#   domain       = "aramakisai.com"
# }
