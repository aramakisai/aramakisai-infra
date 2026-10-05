#!/usr/bin/env bash
# recovery.sh の生存確認ゲート・各ステップの判定ロジックを、外部 API (Hetzner/Tailscale/TFC/
# kubectl/ansible) を関数スタブに差し替えてオフラインで検証する。実クラウドには一切接続しない。
# スタブ漏れで実コマンドが呼ばれないよう、curl/gh/kubectl/infisical/ansible-playbook/terraform は
# PATH 上でも呼び出し記録だけ残すダミーに差し替え、最後に「呼ばれていないこと」を検証する。
#
# 使い方: ./scripts/test-dr-recovery-logic.sh

# スタブ関数・source 先から参照する変数は静的解析では追えない
# shellcheck disable=SC2317,SC2329,SC2034,SC2218,SC2030,SC2031
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
export KUBECONFIG_FILE="${WORK}/kubeconfig"
: >"${KUBECONFIG_FILE}"
CALLS="${WORK}/calls"
export STUB_LOG="${WORK}/stub-log"
: >"${STUB_LOG}"
trap 'rm -rf "${WORK}"' EXIT

mkdir -p "${WORK}/bin"
for cmd in curl gh kubectl infisical ansible-playbook terraform; do
  # shellcheck disable=SC2016 # $STUB_LOG と $* はダミー側のシェルで展開させる
  printf '#!/bin/sh\necho "%s $*" >>"$STUB_LOG"\nexit 99\n' "${cmd}" >"${WORK}/bin/${cmd}"
  chmod +x "${WORK}/bin/${cmd}"
done
# run_ansible が渡す SSH 鍵の状態を、ansible-playbook の代わりに呼ばれる timeout ダミーで記録する
export KEY_INFO="${WORK}/key-info"
cat >"${WORK}/bin/timeout" <<'STUB'
#!/bin/sh
{ echo "path=$ANSIBLE_PRIVATE_KEY_FILE"; stat -c 'mode=%a' "$ANSIBLE_PRIVATE_KEY_FILE"; echo "content=$(cat "$ANSIBLE_PRIVATE_KEY_FILE")"; echo "args=$*"; } >"$KEY_INFO"
exit 0
STUB
chmod +x "${WORK}/bin/timeout"
export PATH="${WORK}/bin:${PATH}"

# shellcheck source=/dev/null
source "${ROOT}/.github/scripts/recovery.sh"
set +e +u
set +o pipefail
# 生存確認ゲート以降のテストは refresh_kubeconfig をスタブするため、実体を別名で退避する
eval "real_$(declare -f refresh_kubeconfig)"

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
echo "=== Tailscale デバイス選別 (offline のみ・名前一致のみ・hostname 欠落に耐える) ==="
DEVICES='{"devices":[
 {"id":"old","hostname":"prod-node-1","connectedToControl":false},
 {"id":"dup","hostname":"prod-node-1-1","connectedToControl":true},
 {"id":"stale-dup","hostname":"prod-node-1-2","connectedToControl":false},
 {"id":"other-node","hostname":"prod-node-10","connectedToControl":false},
 {"id":"node2","hostname":"prod-node-2","connectedToControl":false},
 {"id":"nohost","connectedToControl":false},
 {"id":"nullhost","hostname":null,"connectedToControl":false},
 {"id":"laptop","hostname":"laptop","connectedToControl":false}]}'
assert_eq "offline の削除対象は対象ノード名一致のみ" "old stale-dup" "$(ts_device_ids "${DEVICES}" prod-node-1 offline | tr '\n' ' ' | sed 's/ $//')"
assert_eq "接続中の重複デバイスは削除対象外" "dup" "$(ts_device_ids "${DEVICES}" prod-node-1 online)"
ts_node_registered "${DEVICES}" prod-node-1; assert_eq "完全一致で接続中でなければ未登録" 1 $?
ts_node_registered '{"devices":[{"hostname":"prod-node-1","connectedToControl":true},{"connectedToControl":true}]}' prod-node-1
assert_eq "hostname 欠落デバイスが混ざっても登録済みを判定できる" 0 $?

echo ""
echo "=== 生存確認ゲート ==="
ts_token() { echo tok; }
ts_devices() { echo "${TS_JSON}"; }
endpoint_up() { return "${EP_RC}"; }
kubectl_r() { return "${KUBECTL_RC}"; }
refresh_kubeconfig() { echo refresh_kubeconfig >>"${CALLS}"; return "${REFRESH_RC:-0}"; }
hcloud_server_status() { echo "${HC_STATUS}"; }
tfc_server_in_state() { return "${STATE_RC}"; }
set_world() { HC_STATUS="$1"; TS_JSON="$2"; EP_RC="$3"; KUBECTL_RC="$4"; STATE_RC=0; }
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
REFRESH_RC=1
set_world absent "${OFFLINE}" 1 0
assert_eq "kubeconfig 生成失敗 (CA 取得不可 = 到達不能) -> kubectl は dead として進行" 0 "$(gate)"
assert_eq "kubeconfig 生成失敗のシグナルは kubectl=dead" "kubectl=dead" "$(collect_signals prod-node-1 2>/dev/null | grep kubectl)"
REFRESH_RC=0
assert_eq "kubeconfig 生成成功のシグナルは kubectl=alive" "kubectl=alive" "$( set_world absent "${OFFLINE}" 1 0; collect_signals prod-node-1 2>/dev/null | grep kubectl)"
set_world absent "${OFFLINE}" 1 1
assert_eq "kubeconfig は作れるが kubectl 失敗 -> dead として進行" 0 "$(gate)"
set_world unknown "${OFFLINE}" 1 1
assert_eq "Hetzner API 失敗 (判定不能) -> 停止" 1 "$(gate)"
set_world starting "${OFFLINE}" 1 1
assert_eq "起動途中のサーバー -> 停止" 1 "$(gate)"
set_world absent "${OFFLINE}" 1 1; STATE_RC=1
assert_eq "Hetzner 不在でも TFC state に無い (不整合) -> 停止" 1 "$(gate)"
STATE_RC=2
assert_eq "Hetzner 不在で TFC state を取得できない -> 停止" 1 "$(gate)"
set_world absent '{"devices":"unexpected"}' 1 1
assert_eq "Tailscale 応答の形式が想定外 (jq 失敗) -> dead でなく判定不能として停止" 1 "$(gate)"
set_world absent '{"devices":[{"connectedToControl":false}]}' 1 1
assert_eq "hostname 欠落デバイスのみ -> jq 失敗せず dead 扱いで進行" 0 "$(gate)"
ts_token() { return 1; }
set_world absent "${OFFLINE}" 1 1
assert_eq "Tailscale API 失敗 (判定不能) -> 停止" 1 "$(gate)"
ts_token() { echo tok; }
DR_FORCE=1
set_world running "${ONLINE}" 0 0
assert_eq "DR_FORCE=1 のときだけ上書きして進行" 0 "$(gate)"
DR_FORCE=0

echo ""
echo "=== サーバー状態の突き合わせ ==="
set_world absent "${OFFLINE}" 1 1
assert_eq "不在かつ state にある -> absent" "absent" "$(server_state prod-node-1 2>/dev/null)"
STATE_RC=1
assert_eq "不在だが state に無い -> unknown" "unknown" "$(server_state prod-node-1 2>/dev/null)"
set_world off "${OFFLINE}" 1 1
STATE_RC=2
assert_eq "off は state を見ずそのまま" "off" "$(server_state prod-node-1 2>/dev/null)"

echo ""
echo "=== Terraform plan 検査 ==="
rc() { # address actions-json
  jq -cn --arg a "$1" --argjson x "$2" '{address: $a, change: {actions: $x}}'
}
plan() { local IFS=,; echo "{\"resource_changes\":[$*]}"; }
SRV='hcloud_server.nodes["prod-node-1"]'
KEY=tailscale_tailnet_key.k3s_nodes
OK_PLAN=$(plan "$(rc "${SRV}" '["create"]')" "$(rc "${KEY}" '["delete","create"]')" "$(rc hcloud_network.main '["no-op"]')")
plan_scope_ok "${OK_PLAN}" prod-node-1; assert_eq "サーバー作成+auth key 置換のみ -> OK" 0 $?
plan_scope_ok "$(plan "$(rc "${SRV}" '["create"]')" "$(rc hcloud_placement_group.k3s_nodes '["update"]')")" prod-node-1
assert_eq "placement group 変更が混入 -> NG" 1 $?
plan_scope_ok "$(plan "$(rc "${SRV}" '["create"]')" "$(rc 'hcloud_server.nodes["prod-node-2"]' '["create"]')")" prod-node-1
assert_eq "他ノード作成が混入 -> NG" 1 $?
plan_scope_ok "$(plan "$(rc "${SRV}" '["create"]')" "$(rc cloudflare_record.mx '["update"]')")" prod-node-1
assert_eq "メール以外の DNS 変更が混入 -> NG" 1 $?
plan_scope_ok "$(plan "$(rc "${SRV}" '["delete","create"]')")" prod-node-1
assert_eq "サーバーの置換 (既存を破棄) -> NG" 1 $?
plan_scope_ok "$(plan "$(rc "${KEY}" '["delete","create"]')")" prod-node-1
assert_eq "サーバー作成を含まない (既に存在) -> NG" 1 $?
plan_scope_ok "$(plan "$(rc "${SRV}" '["no-op"]')")" prod-node-1
assert_eq "変更なし -> NG" 1 $?

DNS_OK=$(plan "$(rc cloudflare_record.mail_prod_node_1 '["delete","create"]')" "$(rc cloudflare_record.mail_prod_node_1_ipv4 '["update"]')" \
  "$(rc hcloud_rdns.mail_ipv4 '["create"]')" "$(rc hcloud_rdns.mail_ipv6 '["update"]')" "$(rc "${KEY}" '["delete","create"]')" "$(rc "${SRV}" '["no-op"]')")
scope_mail_dns "${DNS_OK}"; assert_eq "メール 4 リソースの update/replace/create のみ -> OK" 0 $?
scope_mail_dns "$(plan "$(rc cloudflare_record.mail_prod_node_1 '["update"]')" "$(rc cloudflare_record.mx '["update"]')")"
assert_eq "対象外の DNS レコードが混入 -> NG" 1 $?
scope_mail_dns "$(plan "$(rc cloudflare_record.mail_prod_node_1 '["update"]')" "$(rc "${SRV}" '["update"]')")"
assert_eq "サーバーへの変更が混入 -> NG" 1 $?
scope_mail_dns "$(plan "$(rc hcloud_rdns.mail_ipv4 '["delete"]')")"
assert_eq "純粋な削除 -> NG" 1 $?

echo ""
echo "=== tfc_target_apply: スコープ外・失敗時の discard と apply 前フック ==="
tfc_create_run() { echo "create_run $*" >>"${CALLS}"; echo run-1; }
tfc_wait_plan() { echo "${PLAN_KIND:-confirmable}"; }
tfc_plan_json() { echo "${PLAN_JSON}"; }
tfc_wait_applied() { echo "wait_applied" >>"${CALLS}"; }
tfc_api() { echo "tfc $1 $2" >>"${CALLS}"; }
record() { :; }
ts_token() { echo tok; }
ts_devices() { echo "${DEVICES}"; }
curl() { echo "curl $*" >>"${CALLS}"; }

: >"${CALLS}"; PLAN_KIND=confirmable
PLAN_JSON=$(plan "$(rc "${SRV}" '["create"]')" "$(rc hcloud_placement_group.k3s_nodes '["update"]')")
(recreate_node prod-node-1) >/dev/null 2>&1
assert_eq "スコープ外 plan で異常終了する" 1 "$?"
assert_eq "run を discard し DELETE/apply は呼ばない" \
  "create_run DR recovery: server prod-node-1 hcloud_server.nodes[\"prod-node-1\"] tfc POST /runs/run-1/actions/discard " "$(calls)"

: >"${CALLS}"
PLAN_JSON="${OK_PLAN}"
(recreate_node prod-node-1) >/dev/null 2>&1
assert_eq "正常終了する" 0 "$?"
assert_eq "plan 検査後に offline の旧デバイスだけ DELETE してから apply (discard しない)" \
  "create_run DR recovery: server prod-node-1 hcloud_server.nodes[\"prod-node-1\"] curl -sf -X DELETE -H Authorization: Bearer tok https://api.tailscale.com/api/v2/device/old curl -sf -X DELETE -H Authorization: Bearer tok https://api.tailscale.com/api/v2/device/stale-dup tfc POST /runs/run-1/actions/apply wait_applied " "$(calls)"

: >"${CALLS}"
tfc_wait_plan() { echo "run が失敗しました" >&2; exit 1; }
(set -e; recreate_node prod-node-1) >/dev/null 2>&1
assert_eq "plan 待機で失敗しても planned のまま残さず discard する" "1" "$(grep -c discard "${CALLS}")"
tfc_wait_plan() { echo "${PLAN_KIND:-confirmable}"; }

: >"${CALLS}"; PLAN_KIND=no-changes
(recreate_node prod-node-1) >/dev/null 2>&1
assert_eq "サーバー作成の plan に変更なし -> 異常終了 (存在するはず)" 1 "$?"
PLAN_KIND=confirmable

: >"${CALLS}"
PLAN_JSON="${DNS_OK}"
(update_mail_dns prod-node-1) >/dev/null 2>&1
assert_eq "メール DNS/rDNS は 4 アドレスだけを target にした別 run" \
  "create_run DR recovery: mail DNS/rDNS cloudflare_record.mail_prod_node_1 cloudflare_record.mail_prod_node_1_ipv4 hcloud_rdns.mail_ipv4 hcloud_rdns.mail_ipv6 tfc POST /runs/run-1/actions/apply wait_applied " "$(calls)"
: >"${CALLS}"
PLAN_JSON=$(plan "$(rc cloudflare_record.mx '["update"]')")
(update_mail_dns prod-node-1) >/dev/null 2>&1
DNS_RC=$?
assert_eq "メール DNS run のスコープ逸脱 -> 異常終了" 1 "${DNS_RC}"
assert_eq "メール DNS run のスコープ逸脱 -> discard し apply しない" "1 0" "$(grep -c discard "${CALLS}") $(grep -c apply "${CALLS}")"
: >"${CALLS}"
(update_mail_dns prod-node-2) >/dev/null 2>&1
assert_eq "prod-node-1 以外は DNS 更新しない" "" "$(calls)"

echo ""
echo "=== refresh_kubeconfig: kube-oidc.sh で OIDC kubeconfig を作る (失敗は return 1、旧ファイルは壊さない) ==="
KUBE_OIDC="${WORK}/kube-oidc-stub.sh"
cat >"${KUBE_OIDC}" <<'STUB'
#!/bin/sh
echo "kube-oidc $*" >>"$CALLS"
[ "${OIDC_RC:-0}" = "0" ] || exit "${OIDC_RC}"
[ "$1" = "kubeconfig" ] && printf 'apiVersion: v1' >"$2"
STUB
chmod +x "${KUBE_OIDC}"
export CALLS
: >"${CALLS}"; echo old >"${KUBECONFIG_FILE}"
(export OIDC_RC=0; real_refresh_kubeconfig) >/dev/null 2>&1; assert_eq "生成成功: kubeconfig を書き換える" "apiVersion: v1" "$(cat "${KUBECONFIG_FILE}")"
assert_eq "kubeconfig モードで KUBECONFIG_FILE に書かせる" "kube-oidc kubeconfig ${KUBECONFIG_FILE}.new" "$(head -1 "${CALLS}")"
echo old >"${KUBECONFIG_FILE}"
(export OIDC_RC=1; real_refresh_kubeconfig) >/dev/null 2>&1; assert_eq "生成失敗 (CA 取得不可など) -> return 1" 1 $?
assert_eq "失敗時に古い kubeconfig を上書きしない" "old" "$(cat "${KUBECONFIG_FILE}")"
assert_eq "失敗時に一時ファイルを残さない" "no" "$([[ -e "${KUBECONFIG_FILE}.new" ]] && echo yes || echo no)"
assert_eq "recovery.sh は共有 kubeconfig・Infisical の kube 資格情報を参照しない" "0" \
  "$(grep -cE 'secrets get KUBECONFIG|dr_infisical_token|echo "\$\{KUBECONFIG\}"' "${ROOT}/.github/scripts/recovery.sh")"

echo ""
echo "=== CNPG 待機対象・凍結アプリの除外 ==="
CLUSTERS='{"items":[
 {"metadata":{"namespace":"prod","name":"directus-db"},"spec":{"instances":1},"status":{"phase":"Cluster in healthy state"}},
 {"metadata":{"namespace":"zitadel","name":"zitadel-db"},"spec":{"instances":1},"status":{"phase":"Setting up primary"}},
 {"metadata":{"namespace":"prod","name":"frozen-db"},"spec":{"instances":0},"status":{"phase":"x"}},
 {"metadata":{"namespace":"prod","name":"hib-db","annotations":{"cnpg.io/hibernation":"on"}},"spec":{"instances":1},"status":{"phase":"x"}}]}'
assert_eq "instances=0 と hibernation を除外して列挙" "prod/directus-db zitadel/zitadel-db" "$(cnpg_active_clusters "${CLUSTERS}" | tr '\n' ' ' | sed 's/ $//')"
assert_eq "healthy でないのは稼働中クラスターのみ" "zitadel/zitadel-db" "$(cnpg_unhealthy "${CLUSTERS}")"

APPS='{"items":[
 {"metadata":{"name":"cms"},"status":{"health":{"status":"Healthy"}}},
 {"metadata":{"name":"vaultwarden"},"status":{"health":{"status":"Progressing"},"resources":[{"kind":"StatefulSet","namespace":"prod","name":"vaultwarden"},{"kind":"Service","namespace":"prod","name":"vw"}]}},
 {"metadata":{"name":"mailserver"},"status":{"health":{"status":"Progressing"},"resources":[{"kind":"StatefulSet","namespace":"prod","name":"mailserver"}]}},
 {"metadata":{"name":"empty"},"status":{"health":{"status":"Missing"}}}]}'
assert_eq "未 Healthy の Application" "vaultwarden mailserver empty" "$(argocd_unhealthy_apps "${APPS}" | tr '\n' ' ' | sed 's/ $//')"
kubectl_r() { case "$*" in *vaultwarden*) echo 0 ;; *) echo 1 ;; esac; }
app_is_frozen "${APPS}" vaultwarden; assert_eq "全ワークロード replicas=0 -> 凍結扱い" 0 $?
app_is_frozen "${APPS}" mailserver; assert_eq "稼働中のワークロードあり -> 凍結扱いにしない" 1 $?
app_is_frozen "${APPS}" empty; assert_eq "ワークロード不明 -> 凍結扱いにしない" 1 $?

echo ""
echo "=== run_ansible: SSH 鍵を 0600 の一時ファイルで渡し、終了時に削除する ==="
CI_SSH_PRIVATE_KEY="-----KEY-----" K3S_TOKEN=x CLOUDFLARE_TUNNEL_TOKEN=x CLOUDFLARE_TUNNEL_ID=x ARGOCD_GITHUB_DEPLOY_KEY=x
DR_SKIP_INFRA=1
(run_ansible prod-node-1) >/dev/null 2>&1
DR_SKIP_INFRA=0
KEY_PATH=$(sed -n 's/^path=//p' "${KEY_INFO}")
assert_eq "ANSIBLE_PRIVATE_KEY_FILE が設定され権限 600" "mode=600" "$(grep '^mode=' "${KEY_INFO}")"
assert_eq "鍵の内容が書き出される" "content=-----KEY-----" "$(grep '^content=' "${KEY_INFO}")"
assert_eq "対象ノードに --limit される" "1" "$(grep -c -- '--limit prod-node-1' "${KEY_INFO}")"
assert_eq "終了後に鍵ファイルが残らない" "gone" "$([[ -e "${KEY_PATH}" ]] && echo exists || echo gone)"

echo ""
echo "=== git_in_sync / mail_rs_paused ==="
REAL_ROOT="${REPO_ROOT}"
GITT="${WORK}/git"; mkdir -p "${GITT}"
git init -q --bare --initial-branch=main "${GITT}/origin.git"
git clone -q "${GITT}/origin.git" "${GITT}/work" 2>/dev/null
git -C "${GITT}/work" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
git -C "${GITT}/work" push -q origin HEAD:main 2>/dev/null
git -C "${GITT}/work" branch -q -M main 2>/dev/null
git -C "${GITT}/work" branch -q --set-upstream-to=origin/main main 2>/dev/null
REPO_ROOT="${GITT}/work"
git_in_sync >/dev/null 2>&1; assert_eq "origin/main と一致・変更なし -> 0" 0 $?
touch "${GITT}/work/dirty"
git_in_sync >/dev/null 2>&1; assert_eq "未コミット (untracked) の変更あり -> 1" 1 $?
rm -f "${GITT}/work/dirty"
git clone -q "${GITT}/origin.git" "${GITT}/other" 2>/dev/null
git -C "${GITT}/other" -c user.email=t@t -c user.name=t commit -q --allow-empty -m next
git -C "${GITT}/other" push -q origin HEAD:main 2>/dev/null
git_in_sync >/dev/null 2>&1; assert_eq "main が進んで HEAD と不一致 -> 1" 1 $?
git -C "${GITT}/work" remote set-url origin "${GITT}/missing.git"
git_in_sync >/dev/null 2>&1; assert_eq "fetch 失敗 -> 2" 2 $?
mkdir -p "${GITT}/work/gitops/manifests/prod/mailserver"
printf 'spec:\n  paused: true\n' >"${GITT}/work/gitops/manifests/prod/mailserver/replication-source.yaml"
mail_rs_paused; assert_eq "paused: true -> 0" 0 $?
printf 'spec:\n  sourcePVC: x\n' >"${GITT}/work/gitops/manifests/prod/mailserver/replication-source.yaml"
mail_rs_paused; assert_eq "paused 無し -> 1" 1 $?
REPO_ROOT="${REAL_ROOT}"
mail_rs_paused; assert_eq "リポジトリ現状の replication-source.yaml は paused でない (通常時は常に同期)" 1 $?

echo ""
echo "=== bootstrap Secret 修復は ESO 用認証情報から作る ==="
kubectl_r() {
  echo "kubectl $*" >>"${CALLS}"
  case "$*" in
    *"get secret infisical-auth"*) echo -n "${AUTH_B64:-}" ;;
    *"get secret aramakisai-infra-repo"*) echo -n "${KEY_B64:-}" ;;
  esac
}
: >"${CALLS}"; AUTH_B64=$(echo -n id | base64) KEY_B64=$(head -c 200 /dev/zero | tr '\0' a | base64 -w0)
repair_bootstrap_secrets >/dev/null 2>&1
assert_eq "正常な Secret は触らない" "0" "$(grep -c "create secret" "${CALLS}")"
: >"${CALLS}"; AUTH_B64="" INFISICAL_CLIENT_ID=ci-id INFISICAL_CLIENT_SECRET=ci-sec
(repair_bootstrap_secrets) >/dev/null 2>&1
assert_eq "infisical-auth が空なら CI identity の値で作成する" "1" "$(grep -c "clientId=ci-id" "${CALLS}")"

echo ""
echo "=== main: 経路ごとの実行順 ==="
unset -f curl tfc_api
record() { :; }
init_record() { :; }
ts_devices() { echo "${TS_JSON}"; }
kubectl_r() { return "${KUBECTL_RC}"; }
for fn in refresh_kubeconfig recreate_node update_mail_dns poweron_node run_ansible wait_tailscale_registered wait_k3s_ready \
  repair_bootstrap_secrets wait_argocd_healthy wait_cnpg_healthy; do
  eval "${fn}() { echo ${fn} >>\"\${CALLS}\"; }"
done
report_cnpg_recovery_points() { :; }
hcloud_peer_servers() { echo "${PEERS}"; }
ansible_ready() { return "${ANSIBLE_READY_RC}"; }
ANSIBLE_READY_RC=0
git_in_sync() { return "${GIT_SYNC_RC}"; }
mail_rs_paused() { return "${MAIL_PAUSED_RC}"; }
GIT_SYNC_RC=0
MAIL_PAUSED_RC=0
export DR_TARGET_NODE=prod-node-1 K3S_TOKEN=x ARGOCD_GITHUB_DEPLOY_KEY=x CLOUDFLARE_TUNNEL_TOKEN=x CLOUDFLARE_TUNNEL_ID=x
export INFISICAL_CLIENT_ID=x INFISICAL_CLIENT_SECRET=x INFISICAL_PROJECT_ID=x HCLOUD_TOKEN=x
export TAILSCALE_OAUTH_CLIENT_ID=x TAILSCALE_OAUTH_CLIENT_SECRET=x TAILSCALE_TAILNET=x TFC_API_TOKEN=x TFC_WORKSPACE_ID=x
export GH_TOKEN=dummy CI_SSH_PRIVATE_KEY=key
DR_ANSIBLE_INVENTORY="${ROOT}/ansible/inventory/tailscale.yml"
# errexit が効く別プロセス相当の環境で main を実行する (途中の失敗が後続段階を止めることまで検証)
run_main() { : >"${CALLS}"; (set -e; main) >/dev/null 2>&1; echo "rc=$? $(calls)"; }
TAIL="repair_bootstrap_secrets wait_argocd_healthy wait_cnpg_healthy "

PEERS=""
set_world running "${ONLINE}" 0 0
assert_eq "生存シグナルあり -> 停止しインフラ操作なし" "rc=1 refresh_kubeconfig " "$(run_main)"
set_world absent "${OFFLINE}" 1 1
assert_eq "不在 -> 再作成 -> メール DNS -> 接続待機 -> ansible -> 修復と待機" \
  "rc=0 refresh_kubeconfig recreate_node update_mail_dns wait_tailscale_registered run_ansible refresh_kubeconfig ${TAIL}" "$(run_main)"
set_world off "${OFFLINE}" 1 1
assert_eq "停止 -> 電源投入 -> Ready 確認のみ (Ansible・再作成なし)" \
  "rc=0 refresh_kubeconfig poweron_node wait_tailscale_registered wait_k3s_ready ${TAIL}" "$(run_main)"
PEERS="prod-node-2"
assert_eq "残存サーバーあり -> cluster-init せず停止" "rc=1 refresh_kubeconfig " "$(run_main)"
PEERS=""
set_world absent "${OFFLINE}" 1 1
ANSIBLE_READY_RC=1
assert_eq "冪等化前の playbook -> 破壊的操作の前に停止" "rc=1 refresh_kubeconfig " "$(run_main)"
set_world off "${OFFLINE}" 1 1
assert_eq "電源投入だけの経路は playbook 未冪等化でも進める" \
  "rc=0 refresh_kubeconfig poweron_node wait_tailscale_registered wait_k3s_ready ${TAIL}" "$(run_main)"
ANSIBLE_READY_RC=0
set_world absent "${OFFLINE}" 1 1
GIT_SYNC_RC=1
assert_eq "HEAD が origin/main と不一致 -> 破壊的操作の前に停止" "rc=1 refresh_kubeconfig " "$(run_main)"
GIT_SYNC_RC=2
assert_eq "git fetch 失敗 -> 停止" "rc=1 refresh_kubeconfig " "$(run_main)"
GIT_SYNC_RC=0
MAIL_PAUSED_RC=1
assert_eq "再作成の経路で mail RS が paused でない -> 停止" "rc=1 refresh_kubeconfig " "$(run_main)"
set_world off "${OFFLINE}" 1 1
assert_eq "電源投入のみの経路は mail RS の paused を要求しない" \
  "rc=0 refresh_kubeconfig poweron_node wait_tailscale_registered wait_k3s_ready ${TAIL}" "$(run_main)"
GIT_SYNC_RC=1
assert_eq "電源投入のみの経路は git 同期も要求しない (Ansible を流さない)" \
  "rc=0 refresh_kubeconfig poweron_node wait_tailscale_registered wait_k3s_ready ${TAIL}" "$(run_main)"
GIT_SYNC_RC=0; MAIL_PAUSED_RC=0
DR_TARGET_NODE=prod-node-2
assert_eq "cluster-init ホスト以外は自動復旧しない" "rc=1 refresh_kubeconfig " "$(run_main)"
DR_TARGET_NODE=prod-node-1
set_world absent "${OFFLINE}" 1 1; STATE_RC=1
assert_eq "TFC state と不整合 -> 停止" "rc=1 refresh_kubeconfig " "$(run_main)"

DR_FORCE=1
set_world running "${OFFLINE}" 1 1
assert_eq "force + 稼働中 -> インフラ操作なしで ansible から再実行" \
  "rc=0 refresh_kubeconfig wait_tailscale_registered run_ansible refresh_kubeconfig ${TAIL}" "$(run_main)"
DR_FORCE=0

echo ""
echo "=== main: 途中の失敗で後続段階に進まない (errexit) ==="
set_world absent "${OFFLINE}" 1 1
for fn in wait_tailscale_registered run_ansible refresh_kubeconfig repair_bootstrap_secrets wait_argocd_healthy; do
  eval "${fn}() { echo ${fn} >>\"\${CALLS}\"; return 1; }"
done
assert_eq "Tailscale 待機の失敗で Ansible に進まない" "rc=1 refresh_kubeconfig recreate_node update_mail_dns wait_tailscale_registered " "$(run_main)"
wait_tailscale_registered() { echo wait_tailscale_registered >>"${CALLS}"; }
assert_eq "Ansible の失敗で待機に進まない" "rc=1 refresh_kubeconfig recreate_node update_mail_dns wait_tailscale_registered run_ansible " "$(run_main)"
run_ansible() { echo run_ansible >>"${CALLS}"; }
refresh_kubeconfig() { echo refresh_kubeconfig >>"${CALLS}"; return 1; }
assert_eq "bootstrap 後の kubeconfig 再生成の失敗で Secret 修復に進まない" "rc=1 refresh_kubeconfig recreate_node update_mail_dns wait_tailscale_registered run_ansible refresh_kubeconfig " "$(run_main)"
refresh_kubeconfig() { echo refresh_kubeconfig >>"${CALLS}"; }
assert_eq "Secret 修復の失敗で待機に進まない" "rc=1 refresh_kubeconfig recreate_node update_mail_dns wait_tailscale_registered run_ansible refresh_kubeconfig repair_bootstrap_secrets " "$(run_main)"
repair_bootstrap_secrets() { echo repair_bootstrap_secrets >>"${CALLS}"; }
assert_eq "ArgoCD 待機の失敗で CNPG 待機に進まない" "rc=1 refresh_kubeconfig recreate_node update_mail_dns wait_tailscale_registered run_ansible refresh_kubeconfig repair_bootstrap_secrets wait_argocd_healthy " "$(run_main)"

echo ""
echo "=== 外部コマンドの未スタブ呼び出しがないこと ==="
assert_eq "curl/gh/kubectl/infisical/ansible/terraform の実コマンドが呼ばれていない" "" "$(cat "${STUB_LOG}")"

echo ""
echo "${PASS} passed, ${FAIL} failed"
[[ "${FAIL}" -eq 0 ]]
