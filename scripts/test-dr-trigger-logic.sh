#!/usr/bin/env bash
# dr-trigger.sh の判定ロジック (classify_state / is_abort_comment) を
# 実際の Tailscale API・GitHub API を呼ばずに検証するユニットテスト。
#
# 使い方:
#   ./scripts/test-dr-trigger-logic.sh

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

echo "=== classify_state ユニットテスト (Requirement 1 AC1-3) ==="
assert_eq "Tailscaleオフライン -> NodeFailureSuspected" \
  "NodeFailureSuspected" "$(classify_state 0 0)"
assert_eq "Tailscaleオフライン+複数エンドポイントダウン -> NodeFailureSuspected" \
  "NodeFailureSuspected" "$(classify_state 0 3)"
assert_eq "Tailscaleオンライン+2エンドポイントダウン -> NodeFailureSuspected" \
  "NodeFailureSuspected" "$(classify_state 1 2)"
assert_eq "Tailscaleオンライン+1エンドポイントダウン -> SingleEndpointDown" \
  "SingleEndpointDown" "$(classify_state 1 1)"
assert_eq "Tailscaleオンライン+ダウンなし -> Healthy" \
  "Healthy" "$(classify_state 1 0)"

echo ""
echo "=== is_abort_comment ユニットテスト (Requirement 1.6, author_association フィルタ) ==="
is_abort_comment OWNER "abort"; assert_eq "OWNERのabortコメント -> 有効" "0" "$?"
is_abort_comment MEMBER "中止します"; assert_eq "MEMBERの中止コメント -> 有効" "0" "$?"
is_abort_comment COLLABORATOR "Abort please"; assert_eq "COLLABORATORのAbortコメント(大文字) -> 有効" "0" "$?"
is_abort_comment NONE "abort"; assert_eq "NONEのabortコメント -> 無効" "1" "$?"
is_abort_comment CONTRIBUTOR "中止"; assert_eq "CONTRIBUTOR(権限不足)の中止コメント -> 無効" "1" "$?"
is_abort_comment OWNER "thanks for the heads up"; assert_eq "OWNERだが無関係コメント -> 無効" "1" "$?"

echo ""
echo "${PASS} passed, ${FAIL} failed"
[[ "${FAIL}" -eq 0 ]]
