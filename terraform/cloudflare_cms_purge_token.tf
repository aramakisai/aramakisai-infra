# CMS (Payload) が media の非公開化・削除時に cms.aramakisai.com のエッジキャッシュを
# purge するための API トークン。権限は対象 zone の Cache Purge だけに絞る。
# 値は sensitive output から取り出して Infisical の CLOUDFLARE_PURGE_TOKEN に手動登録する
# (e2e_ci の Service Token と同じ流儀。Infisical provider は使っていない)。
#
# apply する Terraform 側の CLOUDFLARE_API_TOKEN に User > API Tokens > Edit 権限が必要
# (トークンを作れるのは、その権限を持つトークンだけ)。

data "cloudflare_api_token_permission_groups" "all" {}

resource "cloudflare_api_token" "cms_cache_purge" {
  name = "aramakisai-cms cache purge"

  policy {
    effect = "allow"
    permission_groups = [
      data.cloudflare_api_token_permission_groups.all.zone["Cache Purge"],
    ]
    resources = {
      "com.cloudflare.api.account.zone.${var.cloudflare_zone_id}" = "*"
    }
  }
}
