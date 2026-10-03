#!/usr/bin/env bash
# recovery.sh の生存確認ゲート・各ステップの判定ロジックを、外部 API (Hetzner/Tailscale/TFC/
# kubectl/ansible) を関数スタブに差し替えてオフラインで検証する。実クラウドには一切接続しない。
#
# 使い方: ./scripts/test-dr-recovery-logic.sh

# スタブ関数・source 先から参照する変数は静的解析では追えない
# shellcheck disable=SC2317,SC2034,SC2218
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export KUBECONFIG_FILE
KUBECONFIG_FILE="$(mktemp)"
CALLS="$(mktemp)"
trap 'rm -f "${KUBECONFIG_FILE}" "${CALLS}"' EXIT

# shellcheck source=/dev/null
source "${ROOT}/.github/scripts/recovery.sh"
set +e +u
set +o pipefail

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
calls() { tr '\n' ' ' <"${CALLS}"; }

echo "=== 入力検証 ==="
validate_target_node prod-node-1; assert_eq "terraform に定義済みのノードは受理" 0 $?
validate_target_node prod-node-9; assert_eq "未定義ノードは拒否" 1 $?
validate_target_node 'prod-node-1; rm -rf /'; assert_eq "コマンド注入風の値は拒否" 1 $?
validate_target_node ''; assert_eq "空は拒否" 1 $?
assert_eq "inventory の cluster-init ホスト" "prod-node-1" "$(inventory_init_host "${ROOT}/ansible/inventory/tailscale.yml")"

echo ""
echo "=== Tailscale デバイス選別 (offline のみ・名前一致のみ) ==="
DEVICES='{"devices":[
 {"id":"old","hostname":"prod-node-1","connectedToControl":false},
 {"id":"dup","hostname":"prod-node-1-1","connectedToControl":true},
 {"id":"stale-dup","hostname":"prod-node-1-2","connectedToControl":false},
 {"id":"other-node","hostname":"prod-node-10","connectedToControl":false},
 {"id":"node2","hostname":"prod-node-2","connectedToControl":false},
 {"id":"laptop","hostname":"laptop","connectedToControl":false}]}'
assert_eq "offline の削除対象は対象ノード名一致のみ" "old stale-dup" "$(ts_device_ids "${DEVICES}" prod-node-1 offline | tr '\n' ' ' | sed 's/ $//')"
assert_eq "接続中の重複デバイスは削除対象外" "dup" "$(ts_device_ids "${DEVICES}" prod-node-1 online)"
ts_node_registered "${DEVICES}" prod-node-1; assert_eq "完全一致で接続中でなければ未登録" 1 $?
ts_node_registered '{"devices":[{"hostname":"prod-node-1","connectedToControl":true}]}' prod-node-1; assert_eq "完全一致で接続中なら登録済み" 0 $?

echo ""
echo "=== 生存確認ゲート ==="
ts_token() { echo tok; }
ts_devices() { echo "${TS_JSON}"; }
endpoint_up() { return "${EP_RC}"; }
kubectl_r() { return "${KUBECTL_RC}"; }
hcloud_server_status() { echo "${HC_STATUS}"; }
set_world() { HC_STATUS="$1"; TS_JSON="$2"; EP_RC="$3"; KUBECTL_RC="$4"; }
OFFLINE='{"devices":[{"hostname":"prod-node-1","connectedToControl":false}]}'
ONLINE='{"devices":[{"hostname":"prod-node-1","connectedToControl":true}]}'
gate() { liveness_gate "$(collect_signals prod-node-1 2>/dev/null)" 2>/dev/null; echo $?; }

DR_FORCE=0
set_world absent "${OFFLINE}" 1 1
assert_eq "全シグナルが死 (サーバー不在) -> 進行" 0 "$(gate)"
set_world off "${OFFLINE}" 1 1
assert_eq "サーバー停止のみ -> 進行" 0 "$(gate)"
set_world running "${OFFLINE}" 1 1
assert_eq "Hetzner が running -> 停止" 1 "$(gate)"
set_world absent "${ONLINE}" 1 1
assert_eq "Tailscale オンライン -> 停止" 1 "$(gate)"
set_world absent "${OFFLINE}" 0 1
assert_eq "公開エンドポイントが 1 つでも応答 -> 停止" 1 "$(gate)"
set_world absent "${OFFLINE}" 1 0
assert_eq "kubectl get nodes が成功 -> 停止" 1 "$(gate)"
set_world unknown "${OFFLINE}" 1 1
assert_eq "Hetzner API 失敗 (判定不能) -> 停止" 1 "$(gate)"
set_world starting "${OFFLINE}" 1 1
assert_eq "起動途中のサーバー -> 停止" 1 "$(gate)"
ts_token() { return 1; }
set_world absent "${OFFLINE}" 1 1
assert_eq "Tailscale API 失敗 (判定不能) -> 停止" 1 "$(gate)"
ts_token() { echo tok; }
DR_FORCE=1
set_world running "${ONLINE}" 0 0
assert_eq "DR_FORCE=1 のときだけ上書きして進行" 0 "$(gate)"
DR_FORCE=0

echo ""
echo "=== Terraform plan 検査 (対象サーバー作成 + auth key のみ許可) ==="
rc() { # name actions-json
  jq -cn --arg a "$1" --argjson x "$2" '{address: $a, change: {actions: $x}}'
}
plan() { local IFS=,; echo "{\"resource_changes\":[$*]}"; }
SRV='hcloud_server.nodes["prod-node-1"]'
OK_PLAN=$(plan "$(rc "${SRV}" '["create"]')" "$(rc tailscale_tailnet_key.k3s_nodes '["delete","create"]')" "$(rc hcloud_network.main '["no-op"]')")
plan_scope_ok "${OK_PLAN}" prod-node-1; assert_eq "サーバー作成+auth key 置換のみ -> OK" 0 $?
plan_scope_ok "$(plan "$(rc "${SRV}" '["create"]')" "$(rc hcloud_placement_group.k3s_nodes '["update"]')")" prod-node-1
assert_eq "placement group 変更が混入 -> NG" 1 $?
plan_scope_ok "$(plan "$(rc "${SRV}" '["create"]')" "$(rc 'hcloud_server.nodes["prod-node-2"]' '["create"]')")" prod-node-1
assert_eq "他ノード作成が混入 -> NG" 1 $?
plan_scope_ok "$(plan "$(rc "${SRV}" '["create"]')" "$(rc 'cloudflare_record.mail_a' '["update"]')")" prod-node-1
assert_eq "DNS レコード変更が混入 -> NG" 1 $?
plan_scope_ok "$(plan "$(rc "${SRV}" '["delete","create"]')")" prod-node-1
assert_eq "サーバーの置換 (既存を破棄) -> NG" 1 $?
plan_scope_ok "$(plan "$(rc tailscale_tailnet_key.k3s_nodes '["delete","create"]')")" prod-node-1
assert_eq "サーバー作成を含まない (既に存在) -> NG" 1 $?
plan_scope_ok "$(plan "$(rc "${SRV}" '["no-op"]')")" prod-node-1
assert_eq "変更なし -> NG" 1 $?

echo ""
echo "=== recreate_node: スコープ外 plan なら Tailscale 削除も apply もしない ==="
tfc_create_run() { echo run-1; }
tfc_wait_plan() { echo confirmable; }
tfc_plan_json() { echo "${PLAN_JSON}"; }
tfc_wait_applied() { echo "wait_applied" >>"${CALLS}"; }
tfc_api() { echo "tfc $1 $2" >>"${CALLS}"; }
ts_devices() { echo "${DEVICES}"; }
curl() { echo "curl $*" >>"${CALLS}"; }
record() { :; }

: >"${CALLS}"
PLAN_JSON=$(plan "$(rc "${SRV}" '["create"]')" "$(rc hcloud_placement_group.k3s_nodes '["update"]')")
(recreate_node prod-node-1) >/dev/null 2>&1
assert_eq "異常終了する" 1 "$?"
assert_eq "run を discard し DELETE/apply は呼ばない" "tfc POST /runs/run-1/actions/discard " "$(calls)"

: >"${CALLS}"
PLAN_JSON="${OK_PLAN}"
(recreate_node prod-node-1) >/dev/null 2>&1
assert_eq "正常終了する" 0 "$?"
assert_eq "offline の旧デバイスだけ DELETE してから apply" \
  "curl -sf -X DELETE -H Authorization: Bearer tok https://api.tailscale.com/api/v2/device/old curl -sf -X DELETE -H Authorization: Bearer tok https://api.tailscale.com/api/v2/device/stale-dup tfc POST /runs/run-1/actions/apply wait_applied " "$(calls)"

echo ""
echo "=== CNPG 待機対象の動的列挙 ==="
CLUSTERS='{"items":[
 {"metadata":{"namespace":"prod","name":"directus-db"},"spec":{"instances":1},"status":{"phase":"Cluster in healthy state"}},
 {"metadata":{"namespace":"zitadel","name":"zitadel-db"},"spec":{"instances":1},"status":{"phase":"Setting up primary"}},
 {"metadata":{"namespace":"prod","name":"frozen-db"},"spec":{"instances":0},"status":{"phase":"x"}},
 {"metadata":{"namespace":"prod","name":"hib-db","annotations":{"cnpg.io/hibernation":"on"}},"spec":{"instances":1},"status":{"phase":"x"}}]}'
assert_eq "instances=0 と hibernation を除外して列挙" "prod/directus-db zitadel/zitadel-db" "$(cnpg_active_clusters "${CLUSTERS}" | tr '\n' ' ' | sed 's/ $//')"
assert_eq "healthy でないのは稼働中クラスターのみ" "zitadel/zitadel-db" "$(cnpg_unhealthy "${CLUSTERS}")"

echo ""
echo "=== mailserver リストアの opt-in と再実行ガード ==="
kubectl_r() { echo "${PVC_ANNOTATION}"; }
DR_RESTORE_MAIL=0 PVC_ANNOTATION=""; mail_restore_needed >/dev/null 2>&1; assert_eq "opt-in なし -> スキップ" 1 $?
DR_RESTORE_MAIL=1 PVC_ANNOTATION=""; mail_restore_needed >/dev/null 2>&1; assert_eq "opt-in + 未リストア PVC -> 実行" 0 $?
DR_RESTORE_MAIL=1 PVC_ANNOTATION="2026-01-01T00:00:00Z"; mail_restore_needed >/dev/null 2>&1; assert_eq "リストア済み PVC は再実行で上書きしない" 1 $?
DR_RESTORE_MAIL=0

echo ""
echo "=== main: ゲートで停止した場合はインフラ操作に進まない ==="
unset -f curl tfc_api
ts_devices() { echo "${TS_JSON}"; }
kubectl_r() { return "${KUBECTL_RC}"; }
record() { :; }
for fn in recreate_node poweron_node run_ansible wait_tailscale_registered wait_argocd_healthy wait_cnpg_healthy; do
  eval "${fn}() { echo ${fn} >>\"\${CALLS}\"; }"
done
repair_bootstrap_secrets() { :; }
repair_mail_tls() { :; }
mail_restore_needed() { return 1; }
report_cnpg_recovery_points() { :; }
hcloud_peer_servers() { echo "${PEERS}"; }
export DR_TARGET_NODE=prod-node-1 K3S_TOKEN=x ARGOCD_GITHUB_DEPLOY_KEY=x CLOUDFLARE_TUNNEL_TOKEN=x CLOUDFLARE_TUNNEL_ID=x
export INFISICAL_CLIENT_ID=x INFISICAL_CLIENT_SECRET=x HCLOUD_TOKEN=x TAILSCALE_OAUTH_CLIENT_ID=x TAILSCALE_OAUTH_CLIENT_SECRET=x
export TAILSCALE_TAILNET=x TFC_API_TOKEN=x TFC_WORKSPACE_ID=x KUBECONFIG=x
DR_ANSIBLE_INVENTORY="${ROOT}/ansible/inventory/tailscale.yml"
run_main() { : >"${CALLS}"; (main) >/dev/null 2>&1; echo "rc=$? $(calls)"; }

PEERS=""
set_world running "${ONLINE}" 0 0
assert_eq "生存シグナルあり -> 停止しインフラ操作なし" "rc=1 " "$(run_main)"
set_world absent "${OFFLINE}" 1 1
assert_eq "不在 -> 再作成 -> 接続待機 -> ansible -> 待機" \
  "rc=0 recreate_node wait_tailscale_registered run_ansible wait_argocd_healthy wait_cnpg_healthy " "$(run_main)"
set_world off "${OFFLINE}" 1 1
assert_eq "停止 -> 電源投入 (再作成しない)" \
  "rc=0 poweron_node wait_tailscale_registered run_ansible wait_argocd_healthy wait_cnpg_healthy " "$(run_main)"
PEERS="prod-node-2"
assert_eq "残存サーバーあり -> cluster-init せず停止" "rc=1 " "$(run_main)"
PEERS=""
DR_TARGET_NODE=prod-node-2
assert_eq "cluster-init ホスト以外は自動復旧しない" "rc=1 " "$(run_main)"
DR_TARGET_NODE=prod-node-1
DR_FORCE=1
set_world running "${OFFLINE}" 1 1
assert_eq "force + サーバー稼働中 -> インフラ操作なしで ansible から再実行" \
  "rc=0 wait_tailscale_registered run_ansible wait_argocd_healthy wait_cnpg_healthy " "$(run_main)"

echo ""
echo "${PASS} passed, ${FAIL} failed"
[[ "${FAIL}" -eq 0 ]]
