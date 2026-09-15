# ============================================================
# Zitadel Actions v2 (vaultwarden-rbac-sync webhook、PoC)
# ============================================================
#
# ロール/グループ変更(user_grant追加・変更・削除)イベントをvaultwarden-rbac-sync
# 常駐Pod(gitops/manifests/prod/vaultwarden-rbac-sync/、task 4で常駐Deployment化)へ
# webhook通知する。target_type=REST_WEBHOOKはinterrupt_on_error=falseと組み合わせる
# ことで、webhook送達失敗がZitadel側の実操作(ロール付与等)を巻き込んで失敗させない。
#
# event group "user.grant" は zitadel/internal/repository/usergrant の
# UserGrantAddedType 等 (added/changed/cascade.changed/removed/cascade.removed/
# deactivated/reactivated) をまとめて捕捉する。

resource "zitadel_action_target" "vaultwarden_rbac_sync" {
  name = "vaultwarden-rbac-sync-webhook"

  endpoint           = var.vaultwarden_rbac_sync_webhook_endpoint
  target_type        = "REST_WEBHOOK"
  timeout            = "10s"
  interrupt_on_error = false
}

resource "zitadel_action_execution_event" "vaultwarden_rbac_sync_user_grant" {
  group      = "user.grant"
  target_ids = [zitadel_action_target.vaultwarden_rbac_sync.id]
}
