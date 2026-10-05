#!/usr/bin/env bash
# verify-environment-protection.sh の判定を、Environments API の応答 JSON で検証するオフラインテスト。
# gh は PATH 上のダミーに差し替え、実 API には接続しない。
#
# 使い方: ./scripts/test-dr-environment-guard.sh

# shellcheck disable=SC2317,SC2329,SC2034
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
mkdir -p "${WORK}/bin"
# GH_ENV / GH_POLICIES (JSON) と GH_ENV_RC / GH_POLICIES_RC (終了コード) をダミー gh が返す
cat >"${WORK}/bin/gh" <<'STUB'
#!/bin/sh
case "$*" in
  *deployment-branch-policies*) printf '%s' "$GH_POLICIES"; exit "${GH_POLICIES_RC:-0}" ;;
  *) printf '%s' "$GH_ENV"; exit "${GH_ENV_RC:-0}" ;;
esac
STUB
chmod +x "${WORK}/bin/gh"
export PATH="${WORK}/bin:${PATH}"
export GITHUB_REPOSITORY=owner/repo

GUARD="${ROOT}/.github/scripts/verify-environment-protection.sh"

PASS=0
FAIL=0
assert_eq() {
  if [[ "$2" == "$3" ]]; then echo "  ✅ $1"; PASS=$((PASS + 1)); else echo "  ❌ $1 (expected=$2, actual=$3)"; FAIL=$((FAIL + 1)); fi
}
run_guard() { bash "${GUARD}" dr-recovery >/dev/null 2>&1; echo $?; }

OK_ENV='{"can_admins_bypass":false,"protection_rules":[{"type":"required_reviewers","prevent_self_review":false,"reviewers":[{"type":"Team","reviewer":{"slug":"infra"}}]},{"type":"branch_policy"}],"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}'
OK_POL='{"total_count":1,"branch_policies":[{"name":"main","type":"branch"}]}'

echo "=== EnvironmentGuard ==="
export GH_ENV="${OK_ENV}" GH_POLICIES="${OK_POL}"
assert_eq "全条件を満たす (自己承認可でも通る)" 0 "$(run_guard)"

GH_ENV="${OK_ENV/\"can_admins_bypass\":false/\"can_admins_bypass\":true}"
assert_eq "管理者 bypass 有効 -> 拒否" 1 "$(run_guard)"
GH_ENV='{"can_admins_bypass":false,"protection_rules":[{"type":"branch_policy"}],"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}'
assert_eq "required reviewers なし -> 拒否" 1 "$(run_guard)"
GH_ENV='{"can_admins_bypass":false,"protection_rules":[{"type":"required_reviewers","reviewers":[]}],"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}'
assert_eq "reviewers が空 -> 拒否" 1 "$(run_guard)"
GH_ENV='{"can_admins_bypass":false,"protection_rules":[{"type":"required_reviewers","reviewers":[{"type":"Team"}]}],"deployment_branch_policy":null}'
assert_eq "deployment branch 制限なし -> 拒否" 1 "$(run_guard)"
GH_ENV='{"can_admins_bypass":false,"protection_rules":[{"type":"required_reviewers","reviewers":[{"type":"Team"}]}],"deployment_branch_policy":{"protected_branches":true,"custom_branch_policies":false}}'
assert_eq "protected branches 方式 (custom でない) -> 拒否" 1 "$(run_guard)"
GH_ENV='{"protection_rules":[{"type":"required_reviewers","reviewers":[{"type":"Team"}]}],"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}'
assert_eq "can_admins_bypass 欠落 -> 拒否 (fail-closed)" 1 "$(run_guard)"
GH_ENV="${OK_ENV}"
GH_POLICIES='{"total_count":2,"branch_policies":[{"name":"main","type":"branch"},{"name":"dev","type":"branch"}]}'
assert_eq "main 以外のブランチも許可 -> 拒否" 1 "$(run_guard)"
GH_POLICIES='{"total_count":1,"branch_policies":[{"name":"release","type":"branch"}]}'
assert_eq "main でないブランチだけ -> 拒否" 1 "$(run_guard)"
GH_POLICIES='{"total_count":1,"branch_policies":[{"name":"main","type":"tag"}]}'
assert_eq "main タグ指定 -> 拒否" 1 "$(run_guard)"
GH_POLICIES='{"total_count":0,"branch_policies":[]}'
assert_eq "ポリシー 0 件 -> 拒否" 1 "$(run_guard)"
GH_POLICIES="${OK_POL}"
export GH_ENV_RC=1
assert_eq "Environment 取得失敗 -> 拒否" 1 "$(run_guard)"
export GH_ENV_RC=0 GH_POLICIES_RC=1
assert_eq "branch policy 取得失敗 -> 拒否" 1 "$(run_guard)"
export GH_POLICIES_RC=0; GH_ENV='not json'
assert_eq "不正な JSON -> 拒否" 1 "$(run_guard)"

echo ""
echo "${PASS} passed, ${FAIL} failed"
[[ "${FAIL}" -eq 0 ]]
