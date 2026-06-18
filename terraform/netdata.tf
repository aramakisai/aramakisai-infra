# ============================================================
# Netdata Cloud — Room / ノード割当
# ============================================================
# child エージェント (gitops/apps/prod/netdata.yaml) が Claim する Room を
# Terraform で宣言的に管理する。Space は Netdata Cloud コンソールで作成済みの
# ものを var.netdata_space_id で参照する (Space作成自体はAPI非対応のため対象外)。

resource "netdata_room" "prod" {
  space_id    = var.netdata_space_id
  name        = "aramakisai-prod"
  description = "荒牧祭インフラ本番ノードの軽量メトリクス可視化 (managed by Terraform)"
}

resource "netdata_node_room_member" "prod_node_1" {
  space_id = var.netdata_space_id
  room_id  = netdata_room.prod.id

  node_names = [
    "prod-node-1",
  ]
}
