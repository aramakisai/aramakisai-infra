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

STALE='{"hostname":"prod-node-1","connectedToControl":false,"lastSeen":"2020-01-01T00:00:00Z"}'
RECENT_OFF="{\"hostname\":\"prod-node-2\",\"connectedToControl\":false,\"lastSeen\":\"$(date -u -d '-1 hour' '+%Y-%m-%dT%H:%M:%SZ')\"}"
assert_eq "古い切断済み残骸デバイスは数えない" "1" \
  "$(tailscale_state_from_devices "$(mk "${STALE}" "$(dev prod-node-1-x true)" "$(dev prod-node-2 true)")")"
assert_eq "最近まで見えていた切断デバイスは数える (単一障害)" "degraded" \
  "$(tailscale_state_from_devices "$(mk "$(dev prod-node-1 true)" "$(dev prod-node-3 true)" "${RECENT_OFF}")")"
assert_eq "hostname 欠落デバイスが混ざっても jq は失敗しない" "1" \
  "$(tailscale_state_from_devices "$(mk "$(dev prod-node-1 true)" '{"connectedToControl":false}' '{"hostname":null}')")"
if tailscale_state_from_devices '{"devices":"unexpected"}' >/dev/null 2>&1; then rc=0; else rc=1; fi
assert_eq "想定外の応答形式は jq 失敗として検知できる" "1" "${rc}"

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
echo "=== 状態保持・再通知の間引き ==="
STATE_FILE="$(mktemp)"; rm -f "${STATE_FILE}"
assert_eq "状態ファイルが無ければ通知する" 0 "$(should_notify node_failure; echo $?)"
mark_notified node_failure
assert_eq "通知直後は間引く" 1 "$(should_notify node_failure; echo $?)"
assert_eq "別種別は独立して通知する" 0 "$(should_notify NodeDegraded; echo $?)"
state_set "notified_node_failure" "$(($(date -u +%s) - RENOTIFY_SECONDS - 1))"
assert_eq "最終通知から間隔が空けば再通知する" 0 "$(should_notify node_failure; echo $?)"
assert_eq "連続 Healthy 回数の既定値は 0" "0" "$(state_get healthy_streak 0)"
rm -f "${STATE_FILE}" "${STATE_FILE}.tmp"

echo ""
echo "=== main: 通知のみ (dispatch せず、重複起票しない) ==="
EXT=$(mktemp) # curl / gh の全呼び出し記録。通知・起票・クローズの実関数を通し、外部呼び出しだけを差し替える
probe_once() { echo "${PROBE_STATE}|1|"; }
sleep() { :; }
curl() { echo "curl $*" >>"${EXT}"; }
gh() {
  echo "gh $*" >>"${EXT}"
  case "$*" in
    "issue list"*) [[ "${LOOKUP_RC:-0}" == "0" ]] || return 1; echo "${OPEN_ISSUE}" ;;
    "issue create"*) echo "https://github.com/x/y/issues/101" ;;
  esac
}
export TAILSCALE_OAUTH_CLIENT_ID=x TAILSCALE_OAUTH_CLIENT_SECRET=x TAILSCALE_TAILNET=x DISCORD_OPS_WEBHOOK_URL=x GH_TOKEN=x
STATE_FILE="$(mktemp)"
fresh_state() { rm -f "${STATE_FILE}" "${STATE_FILE}.tmp"; }
# 外部呼び出しを create / notify / comment / close に要約して返す
run_main() { # state open_issue [lookup_rc]
  : >"${EXT}"; PROBE_STATE="$1" OPEN_ISSUE="$2" LOOKUP_RC="${3:-0}" main >/dev/null 2>&1
  awk '/^curl -sf -X POST/ {printf "notify "} /^gh issue create/ {printf "create "} /^gh issue comment/ {printf "comment "} /^gh issue close/ {printf "close "}' "${EXT}"
}

fresh_state
assert_eq "障害+Issueなし -> 起票と通知のみ" "create notify " "$(run_main NodeFailureSuspected '')"
assert_eq "障害継続+open Issueあり (通知直後) -> 何もしない (重複起票なし)" "" "$(run_main NodeFailureSuspected 101)"
state_set notified_node_failure "$(($(date -u +%s) - RENOTIFY_SECONDS - 1))"
assert_eq "障害継続+open Issueあり (間隔経過) -> 追記と再通知のみ" "comment notify " "$(run_main NodeFailureSuspected 101)"
fresh_state
assert_eq "Issue 検索に失敗 -> 起票せず通知のみ" "notify " "$(run_main NodeFailureSuspected '' 1)"
fresh_state
assert_eq "単体ダウン -> 通知 (起票しない)" "notify " "$(run_main SingleEndpointDown '')"
assert_eq "単体ダウン継続 (通知直後) -> 再通知しない" "" "$(run_main SingleEndpointDown '')"

fresh_state
assert_eq "Healthy 1 回目 -> まだクローズしない" "" "$(run_main Healthy 101)"
assert_eq "Healthy 2 回目 -> まだクローズしない" "" "$(run_main Healthy 101)"
assert_eq "Healthy 3 回連続 -> クローズ" "close " "$(run_main Healthy 101)"
fresh_state
run_main Healthy 101 >/dev/null; run_main Healthy 101 >/dev/null
run_main SingleEndpointDown 101 >/dev/null
assert_eq "途中で非 Healthy を挟むと連続回数がリセットされる" "" "$(run_main Healthy 101)"
fresh_state
run_main Healthy '' >/dev/null; run_main Healthy '' >/dev/null
assert_eq "Issue 検索失敗時は Healthy が続いてもクローズしない" "" "$(run_main Healthy '' 1)"

echo ""
echo "=== どのケースでも dispatch API を呼ばない ==="
: >"${EXT}"
for st in NodeFailureSuspected Healthy SingleEndpointDown NodeDegraded; do
  fresh_state; PROBE_STATE="${st}" OPEN_ISSUE="" main >/dev/null 2>&1
  fresh_state; PROBE_STATE="${st}" OPEN_ISSUE=101 main >/dev/null 2>&1
done
assert_eq "全状態・Issue 有無で外部呼び出しが記録されている (検証が空振りでない)" "1" "$([[ $(wc -l <"${EXT}") -gt 5 ]] && echo 1 || echo 0)"
assert_eq "記録された curl/gh に dispatch 系の呼び出しが無い" "0" "$(grep -cE 'dispatches|workflow run|actions/workflows' "${EXT}")"
rm -f "${EXT}" "${STATE_FILE}" "${STATE_FILE}.tmp"

echo ""
echo "${PASS} passed, ${FAIL} failed"
[[ "${FAIL}" -eq 0 ]]
