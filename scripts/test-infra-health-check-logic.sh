#!/usr/bin/env bash
# shellcheck disable=SC2034,SC2317,SC2329 # kc()/kubelet_stats()の再定義/上書きはソース先の関数からのみ参照されるため誤検知する
# infra-health-check.sh の判定ロジック (check_disk_usage / check_cnpg_archiving) を
# 実際のクラスターに接続せず、kubelet_stats() / kc() をスタブして検証するユニットテスト。
#
# 使い方:
#   ./scripts/test-infra-health-check-logic.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck source=/dev/null
source "${SCRIPT_DIR}/.github/scripts/infra-health-check.sh"

PASS=0
FAIL=0

assert_eq() {
  local desc="$1" expected="$2" actual="$3"
  if [[ "${expected}" == "${actual}" ]]; then
    echo "  ✅ ${desc}"
    PASS=$((PASS + 1))
  else
    echo "  ❌ ${desc} (expected=${expected}, actual=${actual})"
    FAIL=$((FAIL + 1))
  fi
}

echo "=== check_disk_usage ユニットテスト ==="

kubelet_stats() { echo '{"node":{"fs":{"usedBytes":25854394368,"capacityBytes":80321626112}}}'; }
DISK_THRESHOLD_PERCENT=85
assert_eq "使用率32% (閾値85%) -> ok" "ok|32" "$(check_disk_usage)"

kubelet_stats() { echo '{"node":{"fs":{"usedBytes":90000000000,"capacityBytes":100000000000}}}'; }
assert_eq "使用率90% (閾値85%) -> breach" "breach|90" "$(check_disk_usage)"

kubelet_stats() { echo '{"node":{"fs":{}}}'; }
assert_eq "fs情報欠落 -> error" "error|0" "$(check_disk_usage)"

kubelet_stats() { return 22; }
assert_eq "stats取得失敗 -> error" "error|0" "$(check_disk_usage)" 2>/dev/null

echo ""
echo "=== check_cnpg_archiving ユニットテスト ==="

kc() {
  cat <<'EOF'
{
  "items": [
    {
      "metadata": {"namespace": "prod", "name": "authentik-db"},
      "status": {"conditions": [
        {"type": "ContinuousArchiving", "status": "True", "message": "Continuous archiving is working"}
      ]}
    },
    {
      "metadata": {"namespace": "zitadel", "name": "zitadel-db"},
      "status": {"conditions": [
        {"type": "ContinuousArchiving", "status": "False", "message": "failed to archive WAL"}
      ]}
    },
    {
      "metadata": {"namespace": "prod", "name": "presence-db"},
      "status": {"conditions": []}
    }
  ]
}
EOF
}
result="$(check_cnpg_archiving)"
assert_eq "authentik-db: 正常" "1" "$(echo "${result}" | grep -c '^prod/authentik-db|ok|$')"
assert_eq "zitadel-db: アーカイブ失敗検知" "1" "$(echo "${result}" | grep -c '^zitadel/zitadel-db|breach|failed to archive WAL$')"
assert_eq "presence-db: 条件未設定はunknown" "1" "$(echo "${result}" | grep -c '^prod/presence-db|unknown|$')"

kc() { return 1; }
assert_eq "CNPG一覧取得失敗 -> error" "1" "$(check_cnpg_archiving 2>/dev/null | grep -c '^[^|]*|error|')"

echo ""
echo "=== main の終了ステータス ==="

export DISCORD_OPS_WEBHOOK_URL=x GH_TOKEN=x ACTIONS_ID_TOKEN_REQUEST_URL=x ACTIONS_ID_TOKEN_REQUEST_TOKEN=x
CALLS="$(mktemp)"
setup_kubeconfig() { :; }
sync_breach() { echo "breach $1" >>"${CALLS}"; }
sync_recovered() { echo "recovered $1" >>"${CALLS}"; }
whoami_ok() { echo "gha:infra-health-check"; }
cnpg_ok() { echo '{"items":[{"metadata":{"namespace":"prod","name":"db"},"status":{"conditions":[{"type":"ContinuousArchiving","status":"True"}]}}]}'; }
stats_ok() { echo '{"node":{"fs":{"usedBytes":10,"capacityBytes":100}}}'; }

# 引数: whoami関数 cnpg関数 stats関数。kc は "auth" なら whoami、それ以外は cnpg を呼ぶ
run_main() {
  local who_fn="$1" cnpg_fn="$2" stats_fn="$3"
  : >"${CALLS}"
  kc() { if [[ "$1" == "auth" ]]; then "${who_fn}"; else "${cnpg_fn}"; fi; }
  kubelet_stats() { "${stats_fn}"; }
  (main) >/dev/null 2>&1
}
fail() { return 1; }

run_main whoami_ok cnpg_ok stats_ok
assert_eq "正常 -> 0" "0" "$?"

run_main fail cnpg_ok stats_ok
assert_eq "認証失敗 -> 非0" "1" "$?"
assert_eq "認証失敗 -> 他チェック未実行" "0" "$(wc -l <"${CALLS}")"

whoami_other() { echo "system:anonymous"; }
run_main whoami_other cnpg_ok stats_ok
assert_eq "想定外ユーザー -> 非0" "1" "$?"

run_main whoami_ok fail stats_ok
assert_eq "CNPG取得失敗 -> 非0" "1" "$?"
assert_eq "CNPG取得失敗でもdiskチェックは続行" "1" "$(grep -c '^recovered disk-' "${CALLS}")"

run_main whoami_ok cnpg_ok fail
assert_eq "stats取得失敗 -> 非0" "1" "$?"
assert_eq "stats取得失敗でdiskのIssueをクローズしない" "0" "$(grep -c '^recovered disk-' "${CALLS}")"
assert_eq "stats取得失敗でもCNPGチェックは続行" "1" "$(grep -c '^recovered cnpg-wal-prod/db' "${CALLS}")"
rm -f "${CALLS}"

echo ""
echo "${PASS} passed, ${FAIL} failed"
[[ "${FAIL}" -eq 0 ]]
