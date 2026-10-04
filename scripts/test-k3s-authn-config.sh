#!/usr/bin/env bash
# k3s-server ロールの認証設定・監査ポリシー・config.yaml を localhost で描画し、
# 照合規則表の各行・許可リスト空での描画失敗・起動引数の変数反映を判定する。
#
# 使い方:
#   ./scripts/test-k3s-authn-config.sh

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROLE="${ROOT}/ansible/roles/k3s-server"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

cat >"${WORK}/render.yml" <<PLAY
- hosts: localhost
  gather_facts: false
  connection: local
  vars_files:
    - ${ROLE}/defaults/main.yml
  vars:
    ansible_host: node.example
    k3s_private_ip: 192.0.2.1
    k3s_cluster_init: true
  tasks:
    - ansible.builtin.template: {src: "${ROLE}/templates/authentication-config.yaml.j2", dest: "{{ out }}/authn.yaml", mode: "0600"}
    - ansible.builtin.template: {src: "${ROLE}/templates/audit-policy.yaml.j2", dest: "{{ out }}/audit.yaml", mode: "0600"}
    - ansible.builtin.template: {src: "${ROLE}/templates/config.yaml.j2", dest: "{{ out }}/config.yaml", mode: "0600"}
PLAY

# ansible は stdout が非ブロッキングだと起動しないため cat 経由にする
render() { # render <outdir> [extra-vars...]
  local out="$1"; shift
  mkdir -p "${out}"
  ANSIBLE_LOCALHOST_WARNING=False ANSIBLE_INVENTORY_UNPARSED_WARNING=False \
    ansible-playbook -i localhost, -e "out=${out}" "$@" "${WORK}/render.yml" </dev/null 2>&1 | cat
  return "${PIPESTATUS[0]}"
}

PASS=0
FAIL=0
ok() { echo "  ✅ $1"; PASS=$((PASS + 1)); }
ng() { echo "  ❌ $1"; FAIL=$((FAIL + 1)); }

echo "=== 既定値の描画 ==="
if render "${WORK}/default" >"${WORK}/default.log"; then ok "既定値で描画できる"; else ng "既定値で描画できる"; cat "${WORK}/default.log"; fi

if python3 - "${WORK}/default" <<'PY'
import sys, yaml
d = sys.argv[1]
a = yaml.safe_load(open(f"{d}/authn.yaml"))
def need(c, m):
    if not c:
        print("    NG:", m); sys.exit(1)
need(a["apiVersion"] == "apiserver.config.k8s.io/v1" and a["kind"] == "AuthenticationConfiguration", "apiVersion/kind")
need(a["anonymous"] == {"enabled": False}, "anonymous 明示無効")
need(len(a["jwt"]) == 1, "jwt authenticator は 1 つ")
j = a["jwt"][0]
need(j["issuer"]["url"] == "https://token.actions.githubusercontent.com", "issuer")
need(len(j["issuer"]["audiences"]) == 1, "audience は 1 つ")
rules = j["claimValidationRules"]
need(all(r.get("message") for r in rules), "全規則に message")
ex = "\n".join(r["expression"] for r in rules)
for want in ["repository_owner_id", "repository_id", "refs/heads/main", "job_workflow_ref",
             "event_name", "environment", "runner_environment", "github-hosted",
             "infra-health-check.yml", "intrusion-response.yml", "dr-recovery.yml", "kube-cert-issue.yml",
             "dr-recovery", "schedule", "workflow_dispatch"]:
    need(want in ex, f"規則に {want}")
need("k3s-upgrade" not in ex and "pull_request" not in ex, "k3s-upgrade / pull_request 系を含まない")
need("claims.?environment" in ex, "environment は optional 参照")
need('startsWith("aramakisai/aramakisai-infra/.github/workflows/")' in ex, "job_workflow_ref の <owner>/<repo> 前置照合")
need(len(rules) == 7, f"規則数 7 (実際 {len(rules)})")
cm = j["claimMappings"]
need(set(cm) == {"username"}, "claimMappings は username のみ (group/extra なし)")
need(cm["username"]["expression"].startswith('"gha:" +'), "username は gha: プレフィックス")
need(any("startsWith('gha:')" in r["expression"] or 'startsWith("gha:")' in r["expression"] for r in j["userValidationRules"]), "userValidationRules")

p = yaml.safe_load(open(f"{d}/audit.yaml"))
need(p["kind"] == "Policy" and p["rules"][-1] == {"level": "Metadata"}, "監査ポリシー Metadata")
need(p["omitStages"] == ["RequestReceived"], "omitStages")

c = yaml.safe_load(open(f"{d}/config.yaml"))
api = {x.split("=", 1)[0]: x.split("=", 1)[1] for x in c["kube-apiserver-arg"]}
need(api["authentication-config"] == "/etc/rancher/k3s/authentication-config.yaml", "authentication-config")
need(api["audit-policy-file"] == "/etc/rancher/k3s/audit-policy.yaml", "audit-policy-file")
need(api["audit-log-path"].endswith("audit.log"), "audit-log-path")
need((api["audit-log-maxsize"], api["audit-log-maxbackup"], api["audit-log-maxage"]) == ("100", "4", "30"), "監査ログ上限の既定値")
need(c["kube-controller-manager-arg"] == ["cluster-signing-duration=168h"], "cluster-signing-duration")
PY
then ok "認証設定・監査ポリシー・config.yaml の内容"; else ng "認証設定・監査ポリシー・config.yaml の内容"; fi

echo "=== 変数の上書きが config.yaml に出る ==="
render "${WORK}/override" -e k3s_client_cert_max_duration=24h -e k3s_audit_log_max_size_mb=7 \
  -e k3s_audit_log_max_backup=2 -e k3s_audit_log_max_age_days=3 -e k3s_github_oidc_audience=test-aud >"${WORK}/override.log" || cat "${WORK}/override.log"
if python3 - "${WORK}/override" <<'PY'
import sys, yaml
d = sys.argv[1]
c = yaml.safe_load(open(f"{d}/config.yaml"))
api = dict(x.split("=", 1) for x in c["kube-apiserver-arg"])
ok = (c["kube-controller-manager-arg"] == ["cluster-signing-duration=24h"]
      and (api["audit-log-maxsize"], api["audit-log-maxbackup"], api["audit-log-maxage"]) == ("7", "2", "3")
      and yaml.safe_load(open(f"{d}/authn.yaml"))["jwt"][0]["issuer"]["audiences"] == ["test-aud"])
sys.exit(0 if ok else 1)
PY
then ok "変数値が引数と audience に出る"; else ng "変数値が引数と audience に出る"; fi

echo "=== 許可リストが空なら描画が失敗する ==="
if render "${WORK}/empty" -e '{"k3s_github_oidc_workflows": {}}' >"${WORK}/empty.log"; then
  ng "許可リスト空で失敗する"
else
  if grep -q "k3s_github_oidc_workflows" "${WORK}/empty.log"; then ok "許可リスト空で失敗する"; else ng "失敗理由に変数名が出る"; cat "${WORK}/empty.log"; fi
fi

echo ""
echo "PASS=${PASS} FAIL=${FAIL}"
[[ "${FAIL}" -eq 0 ]]
