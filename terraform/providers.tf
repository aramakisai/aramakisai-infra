terraform {
  required_version = ">= 1.9"

  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.50"
    }
    tailscale = {
      source  = "tailscale/tailscale"
      version = "~> 0.17"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 4.0"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    authentik = {
      source  = "goauthentik/authentik"
      version = ">= 2024.12.0"
    }
    uptimerobot = {
      source  = "uptimerobot/uptimerobot"
      version = "~> 1.8"
    }
    healthchecksio = {
      source  = "kristofferahl/healthchecksio"
      version = "~> 1.6"
    }
    netdata = {
      source  = "netdata/netdata"
      version = "~> 0.4"
    }
  }

  # Terraform Cloud (HCP Terraform) 無料枠
  # organization / workspaces は実際の値に変更してください
  cloud {
    organization = "aramakisai"
    workspaces {
      name = "aramakisai-infra"
    }
  }
}

# 認証情報は環境変数から自動読み込み
#   HCLOUD_TOKEN
#   CLOUDFLARE_API_TOKEN
#   TAILSCALE_OAUTH_CLIENT_ID
#   TAILSCALE_OAUTH_CLIENT_SECRET

provider "hcloud" {}

provider "tailscale" {
  # 環境変数 TAILSCALE_OAUTH_CLIENT_ID / TAILSCALE_OAUTH_CLIENT_SECRET から自動読み込み
}

provider "cloudflare" {}

provider "null" {}

provider "random" {}

provider "authentik" {
  url = var.authentik_url
  # 空文字の場合は AUTHENTIK_TOKEN 環境変数にフォールバックさせる
  token = var.authentik_token != "" ? var.authentik_token : null
}

provider "uptimerobot" {
  api_key = var.uptimerobot_api_key != "" ? var.uptimerobot_api_key : null
}

provider "healthchecksio" {
  api_key = var.healthchecksio_api_key != "" ? var.healthchecksio_api_key : null
}

provider "netdata" {
  auth_token = var.netdata_api_token != "" ? var.netdata_api_token : null
}
