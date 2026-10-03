#!/usr/bin/env bash
# K3s コールドスタンバイ復旧スクリプト (人の承認付き dr-recovery ワークフローから実行)
#
# 対象は「クラスター唯一のノードを喪失した」単一ノード構成のみ。
# 残存 etcd メンバーが他にいる構成の復旧 (join) は自動化せず docs/dr-runbook.md の手動手順に委ねる。
# etcd スナップショットは取得していない。ノード全喪失時は空の etcd (cluster-init) から
# ArgoCD が GitOps で再構築し、状態は CNPG WAL / VolSync / Infisical から戻す。
#
# 実行フロー:
#   1. 必須環境変数チェック・進捗記録先 (dr-incident Issue) の確定
#   2. 生存確認ゲート (読み取り専用)。生存を示すシグナルが 1 つでもあれば停止 (DR_FORCE=1 でのみ上書き)
#   3. Hetzner のサーバー状態で復旧モードを決定
#        absent  : Tailscale 旧デバイス削除 → Terraform (対象ノードのみ -target, plan 検査後 apply)
#        off     : 電源投入 (デバイスは消さない。削除すると再接続できなくなる)
#        その他  : DR_FORCE 時のみ。インフラ操作はせず Ansible から再実行
#   4. Tailscale 登録待機 → Ansible (対象ノード限定)
#   5. ArgoCD / 稼働中 CNPG クラスターの healthy 待機 (タイムアウトは失敗)
#   6. infisical-auth / Deploy Key 空チェック・mail-tls 自己修復
#   7. mailserver データの VolSync リストア (DR_RESTORE_MAIL=1 の明示 opt-in、未リストアの PVC のみ)
#
# 必須入力: DR_TARGET_NODE (例: prod-node-1)
# 任意入力: DR_FORCE=1 / DR_RESTORE_MAIL=1
#
# ローカルテスト用フラグ:
#   DR_LOCAL_TEST=1  : 生存確認ゲートとインフラ操作 (Tailscale/Terraform/Ansible) を全てスキップし、
#                      既存 k3d クラスター上で手順 5 以降のみ実行する
#   DR_SKIP_INFRA=1  : DR_LOCAL_TEST と同様にゲートとインフラ操作をスキップするが Ansible は実行する
#                      (KVM テスト向け。DR_ANSIBLE_INVENTORY で inventory を差し替える)

set -euo pipefail

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') [recovery] $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
KUBECONFIG_FILE="${KUBECONFIG_FILE:-/tmp/kubeconfig-recovery}"
REPO="${GITHUB_REPOSITORY:-aramakisai/aramakisai-infra}"
INCIDENT_LABEL="dr-incident"

DR_TARGET_NODE="${DR_TARGET_NODE:-}"
DR_FORCE="${DR_FORCE:-0}"
DR_RESTORE_MAIL="${DR_RESTORE_MAIL:-0}"
DR_LOCAL_TEST="${DR_LOCAL_TEST:-0}"
DR_SKIP_INFRA="${DR_SKIP_INFRA:-0}"
DR_ANSIBLE_INVENTORY="${DR_ANSIBLE_INVENTORY:-${REPO_ROOT}/ansible/inventory/tailscale.yml}"
[[ "${DR_LOCAL_TEST}" == "1" ]] && DR_SKIP_INFRA=1

HCLOUD_API="https://api.hetzner.cloud/v1"
TS_API="https://api.tailscale.com/api/v2"
TFC_API="https://app.terraform.io/api/v2"
ENDPOINTS=(
  "https://idp.aramakisai.com"
  "https://argocd.aramakisai.com"
  "https://webmail.aramakisai.com"
)

DR_ISSUE=""

kubectl_r() { kubectl --kubeconfig="${KUBECONFIG_FILE}" "$@"; }

# ============================================================
# 進捗記録 (dr-incident Issue)
# ============================================================

# 再実行時に TFC run ID など進行状況を人が追えるよう Issue に残す。
# 記録失敗で復旧自体は止めない。
init_record() {
  [[ -n "${GH_TOKEN:-}" ]] || { log "GH_TOKEN 未設定のため進捗は Issue に記録しません"; return 0; }
  DR_ISSUE=$(gh issue list --repo "${REPO}" --label "${INCIDENT_LABEL}" --state open \
    --json number --jq '.[0].number // empty' 2>/dev/null || true)
  if [[ -z "${DR_ISSUE}" ]]; then
    gh api "repos/${REPO}/labels/${INCIDENT_LABEL}" >/dev/null 2>&1 \
      || gh api "repos/${REPO}/labels" -f name="${INCIDENT_LABEL}" -f color="d73a4a" \
        -f description="DR: ノード障害疑い・復旧の進捗記録" >/dev/null 2>&1 || true
    DR_ISSUE=$(gh issue create --repo "${REPO}" --label "${INCIDENT_LABEL}" \
      --title "DR: ${DR_TARGET_NODE} 復旧実行 ($(date -u '+%Y-%m-%dT%H:%M:%SZ'))" \
      --body "dr-recovery の実行記録です。run: ${GITHUB_SERVER_URL:-https://github.com}/${REPO}/actions/runs/${GITHUB_RUN_ID:-unknown}" \
      2>/dev/null | grep -oE '[0-9]+$' || true)
  fi
  record "復旧開始: target=${DR_TARGET_NODE} force=${DR_FORCE} restore_mail=${DR_RESTORE_MAIL} run=${GITHUB_RUN_ID:-local}"
}

record() {
  log "$*"
  [[ -n "${DR_ISSUE}" ]] || return 0
  gh issue comment "${DR_ISSUE}" --repo "${REPO}" --body "$*" >/dev/null 2>&1 || true
}

# ============================================================
# 入力検証
# ============================================================

# Terraform local.nodes に定義済みのノードだけを受け付ける。
validate_target_node() {
  local node="$1"
  [[ "${node}" =~ ^prod-node-[0-9]+$ ]] || return 1
  grep -qE "^[[:space:]]+\"${node}\"[[:space:]]*=[[:space:]]*\{" "${REPO_ROOT}/terraform/main.tf"
}

# inventory 上で k3s_cluster_init: true が付いたホスト名を返す。
inventory_init_host() {
  awk '
    /^        [A-Za-z0-9_.-]+:[[:space:]]*$/ { gsub(/[: ]/, "", $1); host=$1 }
    /k3s_cluster_init:[[:space:]]*true/ { print host; exit }
  ' "$1"
}

# ============================================================
# Tailscale (OAuth クライアント。devices:core の書込スコープが必要)
# ============================================================

ts_token() {
  local response
  response=$(curl -sf -X POST "${TS_API}/oauth/token" \
    -d "client_id=${TAILSCALE_OAUTH_CLIENT_ID}" \
    -d "client_secret=${TAILSCALE_OAUTH_CLIENT_SECRET}") || return 1
  echo "${response}" | jq -r '.access_token // empty'
}

ts_devices() {
  local token="$1"
  curl -sf -H "Authorization: Bearer ${token}" "${TS_API}/tailnet/${TAILSCALE_TAILNET}/devices"
}

# 非 ephemeral のため再作成すると旧デバイスと `<name>-N` で重複する。両方を対象にする。
# 引数: devices JSON, ノード名, 状態 (online|offline|any)
ts_device_ids() {
  local json="$1" node="$2" state="${3:-any}"
  echo "${json}" | jq -r --arg n "${node}" --arg s "${state}" '
    .devices[]
    | select(.hostname | test("^" + $n + "(-[0-9]+)?$"))
    | select($s == "any" or ($s == "online" and .connectedToControl == true)
                         or ($s == "offline" and .connectedToControl != true))
    | .id'
}

# hostname が完全一致で接続中のデバイスがあるか (新ノードの登録確認)
ts_node_registered() {
  echo "$1" | jq -e --arg n "$2" \
    '[.devices[] | select(.hostname == $n and .connectedToControl == true)] | length > 0' >/dev/null
}

# ============================================================
# 生存確認ゲート (読み取り専用)
# ============================================================

# 出力: Hetzner サーバー状態 (running/off/... | absent | unknown)
hcloud_server_status() {
  local node="$1" response
  response=$(curl -sf -H "Authorization: Bearer ${HCLOUD_TOKEN}" \
    "${HCLOUD_API}/servers?name=${node}") || { echo unknown; return; }
  echo "${response}" | jq -r '.servers[0].status // "absent"'
}

# 対象以外の k8s ノード用サーバー (残存 etcd メンバー候補) の名前一覧
hcloud_peer_servers() {
  local node="$1" response
  response=$(curl -sf -H "Authorization: Bearer ${HCLOUD_TOKEN}" \
    "${HCLOUD_API}/servers?label_selector=role%3Dserver") || return 1
  echo "${response}" | jq -r --arg n "${node}" '.servers[] | select(.name != $n) | .name'
}

endpoint_up() { curl -sf -o /dev/null --max-time 10 "$1"; }

# 各シグナルを "名前=alive|dead|unknown" で出力する。
# unknown (API 失敗) は生存の否定にならないため、ゲートでは alive と同様に停止要因として扱う。
collect_signals() {
  local node="$1" status ts_json ts_tok url any_up

  status=$(hcloud_server_status "${node}")
  case "${status}" in
    off | absent) echo "hetzner=dead (${status})" ;;
    unknown) echo "hetzner=unknown" ;;
    *) echo "hetzner=alive (${status})" ;;
  esac

  if ts_tok=$(ts_token) && [[ -n "${ts_tok}" ]] && ts_json=$(ts_devices "${ts_tok}"); then
    if [[ -n "$(ts_device_ids "${ts_json}" "${node}" online)" ]]; then
      echo "tailscale=alive"
    else
      echo "tailscale=dead"
    fi
  else
    echo "tailscale=unknown"
  fi

  any_up=0
  for url in "${ENDPOINTS[@]}"; do
    if endpoint_up "${url}"; then any_up=1; fi
  done
  if [[ "${any_up}" == "1" ]]; then echo "endpoints=alive"; else echo "endpoints=dead"; fi

  if [[ -f "${KUBECONFIG_FILE}" ]] && kubectl_r --request-timeout=10s get nodes >/dev/null 2>&1; then
    echo "kubectl=alive"
  else
    echo "kubectl=dead"
  fi
}

# 戻り値: 0=復旧に進んでよい / 1=停止
liveness_gate() {
  local signals="$1" blocking
  blocking=$(echo "${signals}" | grep -E '=(alive|unknown)' || true)
  [[ -z "${blocking}" ]] && return 0
  log "生存確認ゲート: 以下のシグナルが生存または判定不能を示しています"
  echo "${blocking}" >&2
  [[ "${DR_FORCE}" == "1" ]] && { log "DR_FORCE=1 のためゲートを上書きして続行します"; return 0; }
  return 1
}

# ============================================================
# Terraform Cloud (対象ノードのみ -target、plan 検査後に apply)
# ============================================================

tfc_api() {
  local method="$1" path="$2" body="${3:-}"
  local args=(-sf -X "${method}" -H "Authorization: Bearer ${TFC_API_TOKEN}" -H "Content-Type: application/vnd.api+json")
  [[ -n "${body}" ]] && args+=(-d "${body}")
  curl "${args[@]}" "${TFC_API}${path}"
}

tfc_create_run() {
  local node="$1" payload
  payload=$(jq -n --arg ws "${TFC_WORKSPACE_ID}" --arg addr "hcloud_server.nodes[\"${node}\"]" '{
    data: {
      type: "runs",
      attributes: {
        "is-destroy": false,
        "auto-apply": false,
        "target-addrs": [$addr],
        message: "DR recovery (target only, apply after plan scope check)"
      },
      relationships: { workspace: { data: { type: "workspaces", id: $ws } } }
    }
  }')
  tfc_api POST /runs "${payload}" | jq -r '.data.id // empty'
}

# plan が確認可能 (または変更なし) になるまで待つ。出力: confirmable | no-changes
tfc_wait_plan() {
  local run_id="$1" elapsed=0 timeout=900 json status confirmable
  while true; do
    json=$(tfc_api GET "/runs/${run_id}") || die "TFC run の取得に失敗しました"
    status=$(echo "${json}" | jq -r '.data.attributes.status')
    confirmable=$(echo "${json}" | jq -r '.data.attributes.actions["is-confirmable"] // false')
    log "TFC run ${run_id}: ${status} (${elapsed}s)"
    case "${status}" in
      planned_and_finished) echo no-changes; return ;;
      errored | canceled | force_canceled | discarded) die "TFC run が失敗しました (status: ${status})" ;;
    esac
    [[ "${confirmable}" == "true" ]] && { echo confirmable; return; }
    ((elapsed >= timeout)) && die "TFC plan がタイムアウトしました (${timeout}s)"
    sleep 15
    elapsed=$((elapsed + 15))
  done
}

# plan に含まれる変更が「対象サーバーの新規作成 + 依存する Tailscale auth key」だけかを機械検査する。
# placement group 所属変更や他ノード作成、DNS/RDNS 変更が混入していれば 1 を返す。
# 引数: plan JSON, ノード名
plan_scope_ok() {
  local json="$1" node="$2"
  echo "${json}" | jq -e --arg addr "hcloud_server.nodes[\"${node}\"]" '
    [.resource_changes[] | select(.change.actions != ["no-op"] and .change.actions != ["read"])] as $c
    | ($c | length > 0)
      and ([$c[] | select(.address == $addr and .change.actions == ["create"])] | length == 1)
      and ([$c[] | select(.address != $addr and .address != "tailscale_tailnet_key.k3s_nodes")] | length == 0)
  ' >/dev/null
}

tfc_plan_json() {
  local run_id="$1" plan_id
  plan_id=$(tfc_api GET "/runs/${run_id}" | jq -r '.data.relationships.plan.data.id')
  curl -sfL -H "Authorization: Bearer ${TFC_API_TOKEN}" "${TFC_API}/plans/${plan_id}/json-output"
}

tfc_wait_applied() {
  local run_id="$1" elapsed=0 timeout=900 status
  while true; do
    status=$(tfc_api GET "/runs/${run_id}" | jq -r '.data.attributes.status')
    log "TFC run ${run_id}: ${status} (${elapsed}s)"
    case "${status}" in
      applied) return 0 ;;
      errored | canceled | force_canceled | discarded) die "TFC apply が失敗しました (status: ${status})" ;;
    esac
    ((elapsed >= timeout)) && die "TFC apply がタイムアウトしました (${timeout}s)"
    sleep 15
    elapsed=$((elapsed + 15))
  done
}

recreate_node() {
  local node="$1" run_id plan_kind plan_json token devices id

  run_id=$(tfc_create_run "${node}")
  [[ -n "${run_id}" ]] || die "TFC run の作成に失敗しました"
  record "TFC run 作成 (-target=${node}, auto-apply 無効): ${run_id}"

  plan_kind=$(tfc_wait_plan "${run_id}")
  [[ "${plan_kind}" == "confirmable" ]] || die "plan に変更がありません。サーバーは存在するはずです (run: ${run_id})"

  plan_json=$(tfc_plan_json "${run_id}")
  if ! plan_scope_ok "${plan_json}" "${node}"; then
    tfc_api POST "/runs/${run_id}/actions/discard" '{"comment":"plan scope check failed"}' >/dev/null || true
    die "plan に ${node} の作成以外の変更が含まれるため run を破棄しました (run: ${run_id})。手動で plan を確認してください"
  fi
  record "plan 検査 OK (${node} の作成のみ)。Tailscale 旧デバイスを削除して apply します"

  token=$(ts_token) || die "Tailscale OAuth token の取得に失敗しました"
  devices=$(ts_devices "${token}") || die "Tailscale デバイス一覧の取得に失敗しました"
  for id in $(ts_device_ids "${devices}" "${node}" offline); do
    log "Tailscale 旧デバイス削除: ${id}"
    curl -sf -X DELETE -H "Authorization: Bearer ${token}" "${TS_API}/device/${id}" >/dev/null \
      || die "デバイス削除に失敗しました (${id})。OAuth クライアントに devices:core の書込スコープが必要です"
  done

  tfc_api POST "/runs/${run_id}/actions/apply" '{"comment":"DR recovery apply"}' >/dev/null \
    || die "apply の開始に失敗しました (run: ${run_id})"
  tfc_wait_applied "${run_id}"
  record "Terraform apply 完了 (run: ${run_id})"
}

poweron_node() {
  local node="$1" server_id
  server_id=$(curl -sf -H "Authorization: Bearer ${HCLOUD_TOKEN}" "${HCLOUD_API}/servers?name=${node}" \
    | jq -r '.servers[0].id // empty')
  [[ -n "${server_id}" ]] || die "サーバー ID を取得できませんでした (${node})"
  curl -sf -X POST -H "Authorization: Bearer ${HCLOUD_TOKEN}" \
    "${HCLOUD_API}/servers/${server_id}/actions/poweron" >/dev/null || die "電源投入に失敗しました (${node})"
  record "${node} を電源投入しました (Tailscale デバイスは保持)"
}

wait_tailscale_registered() {
  local node="$1" elapsed=0 timeout=600 token devices
  while true; do
    if token=$(ts_token) && devices=$(ts_devices "${token}") && ts_node_registered "${devices}" "${node}"; then
      record "${node} が Tailscale に接続しました"
      return 0
    fi
    ((elapsed >= timeout)) && die "${node} の Tailscale 接続がタイムアウトしました (${timeout}s)"
    log "未接続 (${elapsed}s)"
    sleep 15
    elapsed=$((elapsed + 15))
  done
}

run_ansible() {
  local node="$1"
  record "Ansible k3s-bootstrap を ${node} に限定して実行します"
  # cluster-init を含む bootstrap は空の etcd から作り直す。残存メンバーがいないことは呼び出し側で確認済み。
  ANSIBLE_HOST_KEY_CHECKING=False \
    K3S_TOKEN="${K3S_TOKEN}" \
    CLOUDFLARE_TUNNEL_TOKEN="${CLOUDFLARE_TUNNEL_TOKEN}" \
    CLOUDFLARE_TUNNEL_ID="${CLOUDFLARE_TUNNEL_ID}" \
    INFISICAL_CLIENT_ID="${INFISICAL_CLIENT_ID}" \
    INFISICAL_CLIENT_SECRET="${INFISICAL_CLIENT_SECRET}" \
    ARGOCD_GITHUB_DEPLOY_KEY="${ARGOCD_GITHUB_DEPLOY_KEY}" \
    ansible-playbook -i "${DR_ANSIBLE_INVENTORY}" --limit "${node}" \
    "${REPO_ROOT}/ansible/playbooks/k3s-bootstrap.yml"

  # bootstrap が新しい kubeconfig を Infisical に登録するため、手元の kubeconfig を差し替える。
  # 値はシェル変数に受けるだけで出力しない。
  if [[ "${DR_SKIP_INFRA}" != "1" ]]; then
    local new_kubeconfig
    new_kubeconfig=$(infisical secrets get KUBECONFIG --env=prod --plain 2>/dev/null || true)
    [[ -n "${new_kubeconfig}" ]] && echo "${new_kubeconfig}" > "${KUBECONFIG_FILE}"
  fi
}

# ============================================================
# クラスター状態の待機
# ============================================================

# 稼働中の CNPG クラスター ("ns/name") を列挙する。instances=0 と hibernation 中は凍結扱いで除外。
cnpg_active_clusters() {
  echo "$1" | jq -r '.items[]
    | select((.spec.instances // 1) > 0)
    | select((.metadata.annotations["cnpg.io/hibernation"] // "off") != "on")
    | "\(.metadata.namespace)/\(.metadata.name)"'
}

# 引数: cluster 一覧 JSON。出力: healthy でないクラスター ("ns/name")
cnpg_unhealthy() {
  echo "$1" | jq -r '.items[]
    | select((.spec.instances // 1) > 0)
    | select((.metadata.annotations["cnpg.io/hibernation"] // "off") != "on")
    | select(.status.phase != "Cluster in healthy state")
    | "\(.metadata.namespace)/\(.metadata.name)"'
}

wait_argocd_healthy() {
  local elapsed=0 timeout=1200 not_healthy
  while true; do
    not_healthy=$(kubectl_r get applications -n argocd -o json 2>/dev/null \
      | jq '[.items[] | select(.status.health.status != "Healthy")] | length' 2>/dev/null || echo 99)
    [[ "${not_healthy}" -eq 0 ]] && { log "全 ArgoCD Application が Healthy です"; return 0; }
    ((elapsed >= timeout)) && die "ArgoCD の Healthy 待機がタイムアウトしました (未 Healthy: ${not_healthy} 件)"
    log "Healthy でない Application: ${not_healthy} 件 (${elapsed}s)"
    sleep 30
    elapsed=$((elapsed + 30))
  done
}

wait_cnpg_healthy() {
  local elapsed=0 timeout=900 json active unhealthy
  while true; do
    json=$(kubectl_r get clusters.postgresql.cnpg.io -A -o json 2>/dev/null || echo '{"items":[]}')
    active=$(cnpg_active_clusters "${json}")
    if [[ -z "${active}" ]]; then
      log "稼働中の CNPG クラスターが見つかりません (${elapsed}s)"
    else
      unhealthy=$(cnpg_unhealthy "${json}")
      [[ -z "${unhealthy}" ]] && { log "稼働中の CNPG クラスターは全て healthy です: $(echo "${active}" | tr '\n' ' ')"; return 0; }
      log "healthy でない CNPG: $(echo "${unhealthy}" | tr '\n' ' ') (${elapsed}s)"
    fi
    ((elapsed >= timeout)) && die "CNPG の healthy 待機がタイムアウトしました"
    sleep 30
    elapsed=$((elapsed + 30))
  done
}

repair_bootstrap_secrets() {
  local client_id key_len needs_repair=false
  client_id=$(kubectl_r get secret infisical-auth -n argocd -o jsonpath='{.data.clientId}' 2>/dev/null | base64 -d || true)
  key_len=$(kubectl_r get secret aramakisai-infra-repo -n argocd -o jsonpath='{.data.sshPrivateKey}' 2>/dev/null | base64 -d | wc -c || echo 0)

  [[ -z "${client_id}" ]] && { log "警告: infisical-auth.clientId が空です"; needs_repair=true; }
  [[ "${key_len}" -lt 100 ]] && { log "警告: aramakisai-infra-repo.sshPrivateKey が空または短すぎます"; needs_repair=true; }

  if [[ "${needs_repair}" == "true" ]]; then
    record "infisical-auth を Infisical の認証情報から修復します"
    kubectl_r create secret generic infisical-auth \
      --from-literal=clientId="${INFISICAL_CLIENT_ID}" \
      --from-literal=clientSecret="${INFISICAL_CLIENT_SECRET}" \
      -n argocd --dry-run=client -o yaml | kubectl_r apply -f -
    kubectl_r annotate externalsecret --all -A "force-sync=$(date +%s)" --overwrite
  else
    log "infisical-auth と Deploy Key は正常です"
  fi
}

# cert-manager の sync タイミングで mail-tls が作られず mailserver が ContainerCreating で止まることがある。
repair_mail_tls() {
  local reason start_epoch stuck
  reason=$(kubectl_r get pod -n prod -l app=mailserver \
    -o jsonpath='{.items[0].status.containerStatuses[0].state.waiting.reason}' 2>/dev/null || true)
  [[ "${reason}" == "ContainerCreating" ]] || return 0

  start_epoch=$(date -d "$(kubectl_r get pod -n prod -l app=mailserver -o jsonpath='{.items[0].status.startTime}' 2>/dev/null)" +%s 2>/dev/null || date +%s)
  stuck=$(($(date +%s) - start_epoch))
  ((stuck >= 120)) || return 0

  if [[ -z "$(kubectl_r get secret mail-tls -n prod --ignore-not-found 2>/dev/null)" ]]; then
    record "mail-tls が無く mailserver が ${stuck}s 停止しているため certificate 関連を apply します"
    local f
    for f in certificate external-secret restic-external-secret; do
      kubectl_r apply -f "${REPO_ROOT}/gitops/manifests/prod/mailserver/${f}.yaml"
    done
  fi
}

# ============================================================
# mailserver リストア (opt-in)
# ============================================================

MAIL_RESTORED_ANNOTATION="dr.aramakisai.com/restored-at"

# 再実行で最新のメールを古いスナップショットで上書きしないよう、リストア済み PVC は対象外にする。
# 戻り値: 0=リストアしてよい / 1=スキップ
mail_restore_needed() {
  local restored
  [[ "${DR_RESTORE_MAIL}" == "1" ]] || { log "DR_RESTORE_MAIL が未指定のため mailserver のリストアをスキップします"; return 1; }
  restored=$(kubectl_r get pvc mailserver-data -n prod \
    -o jsonpath="{.metadata.annotations.dr\.aramakisai\.com/restored-at}" 2>/dev/null || true)
  if [[ -n "${restored}" ]]; then
    log "mailserver-data は ${restored} にリストア済みのためスキップします"
    return 1
  fi
  return 0
}

restore_mailserver() {
  local trigger result elapsed=0 timeout=1800
  trigger="dr-$(date +%Y%m%dT%H%M%S)"
  record "mailserver を停止して VolSync リストアを開始します"
  kubectl_r scale statefulset mailserver -n prod --replicas=0
  kubectl_r wait pod -n prod -l app=mailserver --for=delete --timeout=60s || true

  kubectl_r apply -f - <<EOF
apiVersion: volsync.backube/v1alpha1
kind: ReplicationDestination
metadata:
  name: mailserver-restore
  namespace: prod
spec:
  trigger:
    manual: "${trigger}"
  restic:
    repository: mailserver-restic-secret
    destinationPVC: mailserver-data
    copyMethod: Direct
    moverSecurityContext:
      runAsUser: 0
      runAsGroup: 0
      fsGroup: 0
EOF

  while true; do
    result=$(kubectl_r get replicationdestination/mailserver-restore -n prod \
      -o jsonpath='{.status.latestMoverStatus.result}' 2>/dev/null || true)
    [[ "${result}" == "Successful" ]] && break
    [[ "${result}" == "Failed" ]] && die "VolSync リストアが失敗しました"
    ((elapsed >= timeout)) && die "VolSync リストアがタイムアウトしました (${timeout}s)"
    sleep 15
    elapsed=$((elapsed + 15))
  done

  kubectl_r annotate pvc mailserver-data -n prod "${MAIL_RESTORED_ANNOTATION}=$(date -u '+%Y-%m-%dT%H:%M:%SZ')" --overwrite
  kubectl_r delete replicationdestination mailserver-restore -n prod
  kubectl_r scale statefulset mailserver -n prod --replicas=1
  kubectl_r wait pod -n prod -l app=mailserver --for=condition=Ready --timeout=120s
  record "mailserver のリストア完了"
}

report_cnpg_recovery_points() {
  local json ref ns name point
  json=$(kubectl_r get clusters.postgresql.cnpg.io -A -o json)
  for ref in $(cnpg_active_clusters "${json}"); do
    ns="${ref%%/*}"; name="${ref##*/}"
    point=$(echo "${json}" | jq -r --arg ns "${ns}" --arg n "${name}" \
      '.items[] | select(.metadata.namespace == $ns and .metadata.name == $n) | .status.firstRecoverabilityPoint // "未取得"')
    log "CNPG ${ref}: firstRecoverabilityPoint=${point}"
  done
}

# ============================================================
# メイン
# ============================================================

main() {
  local vars=(INFISICAL_CLIENT_ID INFISICAL_CLIENT_SECRET)
  if [[ "${DR_LOCAL_TEST}" != "1" ]]; then
    vars+=(DR_TARGET_NODE K3S_TOKEN ARGOCD_GITHUB_DEPLOY_KEY CLOUDFLARE_TUNNEL_TOKEN CLOUDFLARE_TUNNEL_ID)
  fi
  if [[ "${DR_SKIP_INFRA}" != "1" ]]; then
    vars+=(HCLOUD_TOKEN TAILSCALE_OAUTH_CLIENT_ID TAILSCALE_OAUTH_CLIENT_SECRET TAILSCALE_TAILNET TFC_API_TOKEN TFC_WORKSPACE_ID KUBECONFIG)
  fi
  local v
  for v in "${vars[@]}"; do
    [[ -n "${!v:-}" ]] || die "必須環境変数が未設定です: ${v}"
  done

  if [[ "${DR_SKIP_INFRA}" != "1" ]]; then
    validate_target_node "${DR_TARGET_NODE}" || die "DR_TARGET_NODE が不正です: ${DR_TARGET_NODE}"
  fi

  if [[ -n "${KUBECONFIG:-}" && ( "${DR_SKIP_INFRA}" != "1" || ! -f "${KUBECONFIG_FILE}" ) ]]; then
    echo "${KUBECONFIG}" > "${KUBECONFIG_FILE}"
    chmod 600 "${KUBECONFIG_FILE}"
  fi
  [[ -f "${KUBECONFIG_FILE}" ]] || die "KUBECONFIG または KUBECONFIG_FILE が必要です"

  if [[ "${DR_SKIP_INFRA}" != "1" ]]; then
    init_record

    local signals status peers init_host
    signals=$(collect_signals "${DR_TARGET_NODE}")
    record "生存確認: $(echo "${signals}" | tr '\n' ' ')"
    liveness_gate "${signals}" || die "ノードの生存を示すシグナルがあるため復旧を中止しました (上書きは force=true)"

    init_host=$(inventory_init_host "${DR_ANSIBLE_INVENTORY}")
    [[ "${DR_TARGET_NODE}" == "${init_host}" ]] \
      || die "${DR_TARGET_NODE} は cluster-init ホスト (${init_host}) ではないため自動復旧の対象外です (docs/dr-runbook.md)"

    peers=$(hcloud_peer_servers "${DR_TARGET_NODE}") || die "Hetzner のサーバー一覧を取得できません"
    [[ -z "${peers}" ]] || die "残存サーバーがあり etcd が分断されるおそれがあるため中止しました: $(echo "${peers}" | tr '\n' ' ') (docs/dr-runbook.md)"

    status=$(hcloud_server_status "${DR_TARGET_NODE}")
    case "${status}" in
      absent) recreate_node "${DR_TARGET_NODE}" ;;
      off) poweron_node "${DR_TARGET_NODE}" ;;
      unknown) die "Hetzner のサーバー状態を取得できません" ;;
      *) log "サーバー状態 ${status}: DR_FORCE のためインフラ操作なしで Ansible から再実行します" ;;
    esac
    wait_tailscale_registered "${DR_TARGET_NODE}"
  fi

  if [[ "${DR_LOCAL_TEST}" != "1" ]]; then
    run_ansible "${DR_TARGET_NODE}"
  fi

  wait_argocd_healthy
  wait_cnpg_healthy
  repair_bootstrap_secrets
  repair_mail_tls

  if mail_restore_needed; then
    restore_mailserver
  fi

  report_cnpg_recovery_points
  record "復旧完了"
}

if [[ "${BASH_SOURCE[0]:-$0}" == "${0}" ]]; then
  main "$@"
fi
