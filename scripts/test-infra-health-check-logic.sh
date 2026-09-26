#!/usr/bin/env bash
# shellcheck disable=SC2034,SC2317,SC2329 # kc()の再定義/上書きはソース先の関数からのみ参照されるため誤検知する
# infra-health-check.sh の判定ロジック (check_disk_usage / check_cnpg_archiving) を
# 実際のクラスターに接続せず、kc() をスタブして検証するユニットテスト。
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

kc() { echo '{"node":{"fs":{"usedBytes":25854394368,"capacityBytes":80321626112}}}'; }
DISK_THRESHOLD_PERCENT=85
assert_eq "使用率32% (閾値85%) -> ok" "ok|32" "$(check_disk_usage)"

kc() { echo '{"node":{"fs":{"usedBytes":90000000000,"capacityBytes":100000000000}}}'; }
assert_eq "使用率90% (閾値85%) -> breach" "breach|90" "$(check_disk_usage)"

kc() { echo '{"node":{"fs":{}}}'; }
assert_eq "fs情報欠落 -> unknown" "unknown|0" "$(check_disk_usage)"

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

echo ""
echo "${PASS} passed, ${FAIL} failed"
[[ "${FAIL}" -eq 0 ]]
