# ============================================================
# Healthchecks.io — Dead Man's Switch (mailserver バックアップ生存確認)
# ============================================================
# Discord連携 (channels) はHealthchecks.ioコンソールでの手動設定とする
# (Requirement 7.3の例外。DISCORD_OPS_WEBHOOK_URLをコンソールに直接入力し、
#  同値をInfisicalにも記録してローテーション追跡を可能にする)

resource "healthchecksio_check" "mailserver_backup" {
  name = "mailserver-backup"
  desc = "VolSync ReplicationSource (mailserver-backup) の6時間毎バックアップ完了確認"

  tags = ["mailserver", "volsync", "backup"]

  timeout = 6 * 3600 # 6時間インターバル
  grace   = 2 * 3600 # grace 2時間
}

# 当日監視 (ops-dashboard collector) の評価が止まったことを検知する。
# timeout・grace は初期値 (仮)。通知先はコンソールで手動設定する
resource "healthchecksio_check" "festival_monitor" {
  name = "festival-monitor"
  desc = "当日監視 collector の評価ループの生存確認 (評価成功のたびに ping)"

  tags = ["festival", "ops-dashboard"]

  timeout = 60
  grace   = 180
}
