#!/usr/bin/env bash
# Tailscale デバイスの選別・登録判定を、API レスポンスの fixture JSON でオフライン検証する。
# 使い方: ./scripts/test-tailscale-devices.sh
# shellcheck disable=SC2034
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=/dev/null
source "${ROOT}/.github/scripts/recovery.sh"
set +e +u
set +o pipefail

PASS=0
FAIL=0
assert_eq() {
  if [[ "$2" == "$3" ]]; then PASS=$((PASS + 1)); echo "  ok   $1"; else FAIL=$((FAIL + 1)); echo "  FAIL $1 (expected=[$2] actual=[$3])"; fi
}
rc() { "$@" >/dev/null 2>&1; echo $?; }

T=tn.ts.net
OLD='{"id":"old","hostname":"scaletest-3","name":"scaletest-3.tn.ts.net","connectedToControl":false,"created":"2026-10-09T01:00:00Z"}'
NEW_DUP='{"id":"new","hostname":"scaletest-3","name":"scaletest-3-1.tn.ts.net","connectedToControl":true,"created":"2026-10-09T02:00:00Z"}'
NEW_OK='{"id":"new","hostname":"scaletest-3","name":"scaletest-3.tn.ts.net","connectedToControl":true,"created":"2026-10-09T02:00:00Z"}'
OTHER='{"id":"other","hostname":"scaletest-30","name":"scaletest-30.tn.ts.net","connectedToControl":true,"created":"2026-10-09T00:00:00Z"}'
SIB='{"id":"sib","hostname":"scaletest-3-1","name":"scaletest-3-1.tn.ts.net","connectedToControl":true,"created":"2026-10-09T00:00:00Z"}'
devs() { local IFS=,; echo "{\"devices\":[$*]}"; }
SINCE=2026-10-09T01:30:00+00:00

echo "=== 旧 offline + 新 online (名前が -N) ==="
D=$(devs "${OLD}" "${NEW_DUP}" "${OTHER}")
assert_eq "旧デバイスが残る間は未登録" 1 "$(rc ts_node_registered "${D}" scaletest-3)"
assert_eq "削除対象は offline の旧のみ" "old" "$(ts_device_ids "${D}" scaletest-3 offline)"
assert_eq "診断出力に旧デバイスの ID が出る" 1 "$(ts_node_diag "${D}" scaletest-3 | grep -c '^old ')"

echo "=== 旧のみ ==="
D=$(devs "${OLD}")
assert_eq "offline の旧のみは未登録" 1 "$(rc ts_node_registered "${D}" scaletest-3)"
assert_eq "削除対象は旧" "old" "$(ts_device_ids "${D}" scaletest-3 offline)"

echo "=== 新のみ (旧削除後) ==="
D=$(devs "${NEW_OK}" "${OTHER}")
assert_eq "名前が -N なしで接続中なら登録済み" 0 "$(rc ts_node_registered "${D}" scaletest-3)"
assert_eq "作成時刻以降なら登録済み" 0 "$(rc ts_node_registered "${D}" scaletest-3 "${SINCE}")"
assert_eq "作成時刻より前のデバイスは未登録" 1 "$(rc ts_node_registered "${D}" scaletest-3 2026-10-09T03:00:00+00:00)"
assert_eq "online は削除対象にならない" "" "$(ts_device_ids "${D}" scaletest-3 offline)"

echo "=== 別ノードの online 同名系 ==="
D=$(devs "${SIB}" "${OTHER}")
assert_eq "scaletest-3-1 や scaletest-30 は scaletest-3 の登録とみなさない" 1 "$(rc ts_node_registered "${D}" scaletest-3)"
assert_eq "online の同名系は削除対象にならない" "" "$(ts_device_ids "${D}" scaletest-3 offline)"
D=$(devs "${SIB}" "${OLD}")
assert_eq "online の同名系があっても offline の旧だけ削除対象" "old" "$(ts_device_ids "${D}" scaletest-3 offline)"
D=$(devs '{"id":"x","hostname":"scaletest-3","connectedToControl":true}' "${NEW_OK}")
assert_eq "name 欠落デバイスが混ざっても判定できる" 0 "$(rc ts_node_registered "${D}" scaletest-3)"

echo ""
echo "${PASS} passed, ${FAIL} failed"
[[ "${FAIL}" -eq 0 ]]
