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

resource "healthchecksio_check" "cms_jobs" {
  name = "cms-jobs"
  desc = "Payload Jobs (payload_jobs) に30分以上未完了の行が無いことの確認"

  tags = ["cms", "payload", "jobs"]

  timeout = 15 * 60 # 15分インターバル
  grace   = 30 * 60 # 一時的な失敗1〜2回は許容
}
