#!/usr/bin/env bash
# GitHub Environment の保護設定を Environments API で検査する。満たさなければ非 0 で終了する。
#   verify-environment-protection.sh <environment>
#
# Environment が未作成だと GitHub は承認なしで自動作成して job を実行してしまうため、
# 実行時に required reviewers・管理者 bypass 無効・deployment branch が main だけであることを確かめる。
# prevent_self_review は検査しない (起動者本人の承認を認める運用)。
# 環境変数: GITHUB_REPOSITORY、GH_TOKEN (gh 用)

set -euo pipefail

env_name="${1:-}"
[[ -n "${env_name}" ]] || { echo "usage: verify-environment-protection.sh <environment>" >&2; exit 2; }

fail() { echo "::error::Environment ${env_name}: $*"; exit 1; }

base="repos/${GITHUB_REPOSITORY}/environments/${env_name}"
env_json="$(gh api "${base}")" || fail "保護設定を取得できません"
policies_json="$(gh api "${base}/deployment-branch-policies")" || fail "deployment branch policy を取得できません"

# jq の失敗 (不正な JSON) と値の欠落はどちらも不備として扱う (fail-closed)
reviewers="$(jq -er '[.protection_rules[]? | select(.type == "required_reviewers") | .reviewers[]?] | length' <<<"${env_json}")" \
  || fail "応答を解釈できません"
((reviewers >= 1)) || fail "required reviewers が設定されていません (docs/dr-runbook.md)"

# jq の // は false にも効くため、欠落と false を型で区別する
[[ "$(jq -r 'if .can_admins_bypass == false then "false" else "unset" end' <<<"${env_json}")" == "false" ]] \
  || fail "管理者による保護ルールの bypass が無効になっていません"

[[ "$(jq -r '.deployment_branch_policy.custom_branch_policies // false' <<<"${env_json}")" == "true" ]] \
  || fail "deployment branch が selected branches (custom) に制限されていません"

only_main="$(jq -r '[.branch_policies[]?] | length == 1 and .[0].name == "main" and .[0].type == "branch"' <<<"${policies_json}")" \
  || fail "deployment branch policy を解釈できません"
[[ "${only_main}" == "true" ]] || fail "deployment branch が main だけに制限されていません"
