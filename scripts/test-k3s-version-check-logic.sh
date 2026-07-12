#!/usr/bin/env bash
# k3s-version-check.sh の判定ロジック (classify_version_diff) を
# ネットワークアクセスなしで検証するユニットテスト。
#
# 使い方:
#   ./scripts/test-k3s-version-check-logic.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck source=/dev/null
source "${SCRIPT_DIR}/.github/scripts/k3s-version-check.sh"

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

echo "=== classify_version_diff ユニットテスト ==="
assert_eq "差分なし" "none" "$(classify_version_diff "v1.32.3+k3s1" "v1.32.3+k3s1")"
assert_eq "patch差分あり" "patch" "$(classify_version_diff "v1.32.3+k3s1" "v1.32.4+k3s1")"
assert_eq "build差分あり (+k3sN)" "patch" "$(classify_version_diff "v1.32.3+k3s1" "v1.32.3+k3s2")"
assert_eq "minor差分あり" "minor" "$(classify_version_diff "v1.32.3+k3s1" "v1.33.0+k3s1")"
assert_eq "major差分あり (minor扱い)" "minor" "$(classify_version_diff "v1.32.3+k3s1" "v2.0.0+k3s1")"
assert_eq "不正なフォーマット時" "minor" "$(classify_version_diff "v1.32.3" "v1.32.4+k3s1")"

echo ""
echo "${PASS} passed, ${FAIL} failed"
[[ "${FAIL}" -eq 0 ]]
