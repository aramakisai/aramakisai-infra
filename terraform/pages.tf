# ============================================================
# Cloudflare Pages Project (未使用)
#
# 実際のデプロイは Workers (opennextjs-cloudflare, frontend/wrangler.toml +
# .github/workflows/frontend-ci.yml) が担っており、このプロジェクトは
# デプロイパイプラインからは呼ばれていない。aramakisai.com の紐付けは
# terraform/dns.tf の cloudflare_workers_domain.aramakisai_web_prod で行う。
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
