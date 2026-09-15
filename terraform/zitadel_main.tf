# ============================================================
# Zitadel Terraform Provider 基本設定 (authentikの後継、PoC)
# ============================================================
#
# required_providers自体はTerraformの制約(モジュール内で1箇所のみ)によりproviders.tfへ
# 集約する(zitadel/zitadel追加のみ)。design.md File Structure Plan通り、実際のprovider
# 設定(domain/token)はここzitadel_main.tfへ分離する。domain/tokenはTFC workspace変数
# (TF_VAR_zitadel_domain, TF_VAR_zitadel_token等)として本番運用時に注入する想定。
#
# 認証はPAT(access_token)を使う。発行元はansible/roles/zitadel-bootstrap
# (machine user: terraform-provider, role: IAM_OWNER)。

provider "zitadel" {
  domain       = var.zitadel_domain
  port         = var.zitadel_port != "" ? var.zitadel_port : null
  insecure     = var.zitadel_insecure
  access_token = var.zitadel_token != "" ? var.zitadel_token : null
}
