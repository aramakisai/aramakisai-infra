#!/usr/bin/env bash
# dr-trigger.sh の判定ロジック (classify_state / is_abort_comment) を
# 実際の Tailscale API・GitHub API を呼ばずに検証するユニットテスト。
#
# 使い方:
#   ./scripts/test-dr-trigger-logic.sh

# スタブ関数・source 先から参照する変数は静的解析では追えない
# shellcheck disable=SC2317,SC2034,SC2218
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck source=/dev/null
source "${SCRIPT_DIR}/.github/scripts/dr-trigger.sh"

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

echo "=== classify_state ==="
assert_eq "全ノード断 -> NodeFailureSuspected" "NodeFailureSuspected" "$(classify_state 0 0)"
assert_eq "全断+複数エンドポイントダウン -> NodeFailureSuspected" "NodeFailureSuspected" "$(classify_state 0 3)"
assert_eq "全ノード接続+2エンドポイントダウン -> NodeFailureSuspected" "NodeFailureSuspected" "$(classify_state 1 2)"
assert_eq "全ノード接続+1エンドポイントダウン -> SingleEndpointDown" "SingleEndpointDown" "$(classify_state 1 1)"
assert_eq "全ノード接続+ダウンなし -> Healthy" "Healthy" "$(classify_state 1 0)"
assert_eq "一部切断 (クォーラム維持)+ダウンなし -> NodeDegraded" "NodeDegraded" "$(classify_state degraded 0)"
assert_eq "一部切断+1エンドポイントダウン -> NodeDegraded" "NodeDegraded" "$(classify_state degraded 1)"
assert_eq "一部切断+2エンドポイントダウン -> NodeFailureSuspected" "NodeFailureSuspected" "$(classify_state degraded 2)"
assert_eq "判定不能+2エンドポイントダウン -> NodeFailureSuspected" "NodeFailureSuspected" "$(classify_state unknown 2)"
assert_eq "判定不能+1エンドポイントダウン -> SingleEndpointDown" "SingleEndpointDown" "$(classify_state unknown 1)"
assert_eq "判定不能+ダウンなし -> Healthy" "Healthy" "$(classify_state unknown 0)"

echo ""
echo "=== tailscale_state_from_devices (実在ノードの単一障害とクォーラム喪失の区別) ==="
dev() { # hostname connected
  printf '{"hostname":"%s","connectedToControl":%s}' "$1" "$2"
}
mk() { local IFS=,; echo "{\"devices\":[$*]}"; }
assert_eq "1ノード接続 -> 1" "1" "$(tailscale_state_from_devices "$(mk "$(dev prod-node-1 true)")")"
assert_eq "1ノード切断 -> 0" "0" "$(tailscale_state_from_devices "$(mk "$(dev prod-node-1 false)")")"
assert_eq "ノード未検出 -> 0" "0" "$(tailscale_state_from_devices "$(mk "$(dev other-host true)")")"
assert_eq "3ノード中1台切断 -> degraded" "degraded" \
  "$(tailscale_state_from_devices "$(mk "$(dev prod-node-1 true)" "$(dev prod-node-2 true)" "$(dev prod-node-3 false)")")"
assert_eq "3ノード中2台切断 (クォーラム喪失) -> 0" "0" \
  "$(tailscale_state_from_devices "$(mk "$(dev prod-node-1 true)" "$(dev prod-node-2 false)" "$(dev prod-node-3 false)")")"
assert_eq "3ノード全接続 -> 1" "1" \
  "$(tailscale_state_from_devices "$(mk "$(dev prod-node-1 true)" "$(dev prod-node-2 true)" "$(dev prod-node-3 true)")")"
assert_eq "ノード以外のデバイスは数えない" "1" \
  "$(tailscale_state_from_devices "$(mk "$(dev prod-node-1 true)" "$(dev laptop false)" "$(dev ci-runner false)")")"

echo ""
echo "=== decide_final_state (連続判定の閾値, PROBE_COUNT=3) ==="
PROBE_COUNT=3
F=NodeFailureSuspected
assert_eq "障害1回 -> Pending (通知しない)" "Pending" "$(decide_final_state $F)"
assert_eq "障害2回 -> Pending" "Pending" "$(decide_final_state $F $F)"
assert_eq "障害3回連続 -> NodeFailureSuspected" "NodeFailureSuspected" "$(decide_final_state $F $F $F)"
assert_eq "障害の後に回復 -> Healthy" "Healthy" "$(decide_final_state $F Healthy)"
assert_eq "障害の後に単体ダウン -> SingleEndpointDown" "SingleEndpointDown" "$(decide_final_state $F SingleEndpointDown)"
assert_eq "初回から Healthy -> Healthy" "Healthy" "$(decide_final_state Healthy)"

echo ""
echo "=== main: 通知のみ (dispatch せず、重複起票しない) ==="
CALLS=$(mktemp)
probe_once() { echo "${PROBE_STATE}|1|"; }
sleep() { :; }
notify_discord() { echo "notify" >>"${CALLS}"; }
create_incident_issue() { echo "create" >>"${CALLS}"; echo 101; }
comment_issue() { echo "comment" >>"${CALLS}"; }
close_issue_recovered() { echo "close" >>"${CALLS}"; }
gh() { echo "gh $*" >>"${CALLS}"; }
find_open_incident() { echo "${OPEN_ISSUE}"; }
hourly_slot() { return "${SLOT}"; }
export TAILSCALE_OAUTH_CLIENT_ID=x TAILSCALE_OAUTH_CLIENT_SECRET=x TAILSCALE_TAILNET=x DISCORD_OPS_WEBHOOK_URL=x GH_TOKEN=x
run_main() { : >"${CALLS}"; PROBE_STATE="$1" OPEN_ISSUE="$2" SLOT="$3" main >/dev/null 2>&1; tr '\n' ' ' <"${CALLS}"; }

assert_eq "障害+Issueなし -> 起票と通知のみ" "create notify " "$(run_main NodeFailureSuspected '' 1)"
assert_eq "障害+open Issueあり (毎時枠外) -> 何もしない (重複起票なし)" "" "$(run_main NodeFailureSuspected 101 1)"
assert_eq "障害+open Issueあり (毎時枠) -> 追記と再通知のみ" "comment notify " "$(run_main NodeFailureSuspected 101 0)"
assert_eq "Healthy+open Issueあり -> クローズ" "close " "$(run_main Healthy 101 1)"
assert_eq "単体ダウン (毎時枠外) -> 通知しない" "" "$(run_main SingleEndpointDown '' 1)"
assert_eq "いずれの経路でも dispatch API を呼ばない" "0" "$(grep -c dispatches "${CALLS}")"
rm -f "${CALLS}"

echo ""
echo "${PASS} passed, ${FAIL} failed"
[[ "${FAIL}" -eq 0 ]]
