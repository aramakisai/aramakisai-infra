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
#   3. Hetzner のサーバー状態 (不在は TFC state と突き合わせ) で復旧モードを決定
#        absent  : Terraform (対象サーバーのみ -target, plan 検査後 apply) → メール DNS/rDNS だけの 2 回目の run
#                  → Ansible。Tailscale 旧デバイスは plan 検査後・apply 直前に削除
#        off     : 電源投入 → k3s の Ready 確認のみ (Ansible は流さない。デバイスも消さない)
#        その他  : DR_FORCE 時のみ。インフラ操作なしで Ansible から再実行
#   4. bootstrap Secret の自己修復 → ArgoCD / 稼働中 CNPG の healthy 待機 (タイムアウトは失敗)
#
# Ansible を流す経路は冪等化済みの playbook (ansible/playbooks/tasks/ensure_secret.yml) と、
# HEAD が origin/main と一致し未コミット変更が無いことが前提。満たさなければ破壊的操作の前に停止する。
# サーバーを作り直す経路は、メールの ReplicationSource が spec.paused: true であることも前提とする。
#
# kube-apiserver へは GitHub Actions OIDC (kube-oidc.sh) で認証する。kubeconfig は開始時と、
# クラスター再作成で CA が変わる bootstrap 後の 2 回生成する。
# Infisical は読取用の CI machine identity (INFISICAL_CLIENT_ID/SECRET) だけを使う。
# メールデータのリストアは自動化しない (docs/dr-runbook.md)。
#
# 必須入力: DR_TARGET_NODE (例: prod-node-1)
# 任意入力: DR_FORCE=1
#
# ローカルテスト用フラグ:
#   DR_LOCAL_TEST=1  : 生存確認ゲートとインフラ操作 (Tailscale/Terraform/Ansible) を全てスキップし、
#                      既存 k3d クラスター上で待機・自己修復のみ実行する
#   DR_SKIP_INFRA=1  : DR_LOCAL_TEST と同様にゲートとインフラ操作をスキップするが Ansible は実行する
#                      (KVM テスト向け。DR_ANSIBLE_INVENTORY で inventory を差し替える)

set -euo pipefail

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') [recovery] $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
KUBECONFIG_FILE="${KUBECONFIG_FILE:-/tmp/kubeconfig-recovery}"
KUBE_OIDC="${KUBE_OIDC:-${REPO_ROOT}/.github/scripts/kube-oidc.sh}"
REPO="${GITHUB_REPOSITORY:-aramakisai/aramakisai-infra}"
INCIDENT_LABEL="dr-incident"

DR_TARGET_NODE="${DR_TARGET_NODE:-}"
DR_FORCE="${DR_FORCE:-0}"
DR_LOCAL_TEST="${DR_LOCAL_TEST:-0}"
DR_SKIP_INFRA="${DR_SKIP_INFRA:-0}"
DR_ANSIBLE_INVENTORY="${DR_ANSIBLE_INVENTORY:-${REPO_ROOT}/ansible/inventory/tailscale.yml}"
[[ "${DR_LOCAL_TEST}" == "1" ]] && DR_SKIP_INFRA=1

HCLOUD_API="https://api.hetzner.cloud/v1"
TFC_API="https://app.terraform.io/api/v2"
ENDPOINTS=(
  "https://idp.aramakisai.com"
  "https://argocd.aramakisai.com"
  "https://webmail.aramakisai.com"
)

DR_ISSUE=""
TFC_PENDING_RUN=""
SSH_KEY_FILE=""
TFC_SCOPE_NODE=""
TFC_RESULT=""

# サーバー作成後に追従が必要な (prod-node-1 のアドレスを参照する) メール用リソース
MAIL_DNS_ADDRS=(
  "cloudflare_record.mail_prod_node_1"
  "cloudflare_record.mail_prod_node_1_ipv4"
  "hcloud_rdns.mail_ipv4"
  "hcloud_rdns.mail_ipv6"
)

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
  record "復旧開始: target=${DR_TARGET_NODE} force=${DR_FORCE} run=${GITHUB_RUN_ID:-local}"
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

# shellcheck source=/dev/null
source "$(dirname "${BASH_SOURCE[0]}")/tailscale-devices.sh"

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

# TFC の state に対象サーバーが記録されているか。戻り値: 0=ある / 1=ない / 2=取得失敗
# ノード名や API 障害の取り違えで「不在」と誤判定しないための突き合わせに使う。
tfc_server_in_state() {
  local node="$1" json
  json=$(tfc_api GET "/workspaces/${TFC_WORKSPACE_ID}/resources?page%5Bsize%5D=100") || return 2
  echo "${json}" | jq -e --arg n "${node}" --arg addr "hcloud_server.nodes[\"${node}\"]" '
    any(.data[]?.attributes;
      (.address // "") == $addr
      or ((.name // "") == "nodes" and (.["name-index"] // "") == $n
          and ((.["provider-type"] // .type // "") | test("hcloud"))))' >/dev/null
  case $? in 0) return 0 ;; 1) return 1 ;; *) return 2 ;; esac
}

# Hetzner API の状態に TFC state との整合を加えた判定。
# 出力: running 等 / off / absent (Hetzner 不在かつ state にある) / unknown (取得失敗・不整合)
server_state() {
  local node="$1" status rc=0
  status=$(hcloud_server_status "${node}")
  if [[ "${status}" == "absent" ]]; then
    tfc_server_in_state "${node}" || rc=$?
    if [[ "${rc}" != "0" ]]; then
      log "Hetzner 上に ${node} は無いが TFC state で確認できません (rc=${rc})。判定不能として扱います"
      status="unknown"
    fi
  fi
  echo "${status}"
}

endpoint_up() { curl -sf -o /dev/null --max-time 10 "$1"; }

# 各シグナルを "名前=alive|dead|unknown" で出力する。
# unknown (API 失敗) は生存の否定にならないため、ゲートでは alive と同様に停止要因として扱う。
collect_signals() {
  local node="$1" status ts_json ts_tok online_ids url any_up

  status=$(server_state "${node}")
  case "${status}" in
    off | absent) echo "hetzner=dead (${status})" ;;
    unknown) echo "hetzner=unknown" ;;
    *) echo "hetzner=alive (${status})" ;;
  esac

  if ts_tok=$(ts_token) && [[ -n "${ts_tok}" ]] && ts_json=$(ts_devices "${ts_tok}") \
    && online_ids=$(ts_device_ids "${ts_json}" "${node}" online); then
    if [[ -n "${online_ids}" ]]; then
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

  # CA を取得できない (到達不能) か kubectl が失敗すれば dead。復旧は旧クラスターが無いことを前提に進む
  if refresh_kubeconfig 2>/dev/null && kubectl_r --request-timeout=10s get nodes >/dev/null 2>&1; then
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
  local message="$1" payload
  shift
  payload=$(jq -n --arg ws "${TFC_WORKSPACE_ID}" --arg msg "${message}" '{
    data: {
      type: "runs",
      attributes: {
        "is-destroy": false,
        "auto-apply": false,
        "target-addrs": $ARGS.positional,
        message: $msg
      },
      relationships: { workspace: { data: { type: "workspaces", id: $ws } } }
    }
  }' --args "$@")
  tfc_api POST /runs "${payload}" | jq -r '.data.id // empty'
}

# apply 前に異常終了したとき planned のまま残る run がワークスペースをロックし続けないよう破棄する
tfc_discard_pending() {
  [[ -n "${TFC_PENDING_RUN}" ]] || return 0
  tfc_api POST "/runs/${TFC_PENDING_RUN}/actions/discard" '{"comment":"recovery.sh stopped before apply"}' >/dev/null 2>&1 || true
  log "未 apply の TFC run ${TFC_PENDING_RUN} を discard しました"
  TFC_PENDING_RUN=""
}

# 異常終了時に、未 apply の run の破棄と SSH 鍵の一時ファイル削除を行う
cleanup_all() {
  tfc_discard_pending
  [[ -z "${SSH_KEY_FILE}" ]] || rm -f "${SSH_KEY_FILE}"
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

# plan の変更が指定アドレス (と依存する auth key) の create/update/置換だけで、純粋な削除を含まないかを検査する。
# 引数: plan JSON, 許可アドレス...
plan_scope_addrs() {
  local json="$1" allowed
  shift
  allowed=$(printf '%s\n' "$@" | jq -R . | jq -cs .)
  echo "${json}" | jq -e --argjson allowed "${allowed}" '
    [.resource_changes[] | select(.change.actions != ["no-op"] and .change.actions != ["read"])] as $c
    | ([$c[] | select(((.address as $a | $allowed | index($a)) == null)
                      and .address != "tailscale_tailnet_key.k3s_nodes")] | length == 0)
      and ([$c[] | select(.change.actions == ["delete"])] | length == 0)
  ' >/dev/null
}

scope_server_create() { plan_scope_ok "$1" "${TFC_SCOPE_NODE}"; }
scope_mail_dns() { plan_scope_addrs "$1" "${MAIL_DNS_ADDRS[@]}"; }

# redacted 版でも resource_changes のアドレスと actions は含まれ、機微な値は含まれない
tfc_plan_json() {
  local run_id="$1" plan_id
  plan_id=$(tfc_api GET "/runs/${run_id}" | jq -r '.data.relationships.plan.data.id')
  curl -sfL -H "Authorization: Bearer ${TFC_API_TOKEN}" "${TFC_API}/plans/${plan_id}/json-output-redacted"
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

# 対象限定の run を作り、plan がスコープ内であることを検査してから apply する。
# 引数: ラベル, スコープ検査関数, apply 直前フック (空可), 対象アドレス...
# 結果: TFC_RESULT=applied | no-changes
tfc_target_apply() {
  local label="$1" scope_fn="$2" pre_apply="$3" run_id plan_kind plan_json
  shift 3

  trap cleanup_all EXIT
  run_id=$(tfc_create_run "DR recovery: ${label}" "$@")
  [[ -n "${run_id}" ]] || die "TFC run の作成に失敗しました"
  TFC_PENDING_RUN="${run_id}"
  record "TFC run 作成 (${label}, target=$*, auto-apply 無効): ${run_id}"

  plan_kind=$(tfc_wait_plan "${run_id}")
  if [[ "${plan_kind}" == "no-changes" ]]; then
    TFC_PENDING_RUN=""
    TFC_RESULT="no-changes"
    record "plan に変更なし (${label}, run: ${run_id})"
    return 0
  fi

  plan_json=$(tfc_plan_json "${run_id}")
  "${scope_fn}" "${plan_json}" \
    || die "plan にスコープ外の変更が含まれるため停止します (${label}, run: ${run_id} は discard)。手動で plan を確認してください"
  record "plan 検査 OK (${label})"

  [[ -z "${pre_apply}" ]] || "${pre_apply}"

  tfc_api POST "/runs/${run_id}/actions/apply" '{"comment":"DR recovery apply"}' >/dev/null \
    || die "apply の開始に失敗しました (run: ${run_id})"
  TFC_PENDING_RUN=""
  tfc_wait_applied "${run_id}"
  TFC_RESULT="applied"
  record "Terraform apply 完了 (${label}, run: ${run_id})"
}

# 非 ephemeral のため再作成すると旧デバイスと重複し、MagicDNS 名が旧デバイスに解決される。
# plan 検査を通った後・apply の直前に、対象名一致 かつ offline のものだけ ID 指定で削除する。
delete_stale_tailscale_devices() {
  ts_purge_stale "${TFC_SCOPE_NODE}" || die "Tailscale 旧デバイスの削除に失敗しました"
}

recreate_node() {
  TFC_SCOPE_NODE="$1"
  tfc_target_apply "server ${TFC_SCOPE_NODE}" scope_server_create delete_stale_tailscale_devices \
    "hcloud_server.nodes[\"${TFC_SCOPE_NODE}\"]"
  [[ "${TFC_RESULT}" == "applied" ]] || die "plan に変更がありません。サーバーは存在するはずです"
}

# 新サーバーの IP に A/AAAA と rDNS を追従させる。メール用リソースだけを対象にした別 run にする。
update_mail_dns() {
  local node="$1"
  if [[ "${node}" != "prod-node-1" ]]; then
    log "${node} はメール用アドレスの対象ではないため DNS/rDNS 更新をスキップします"
    return 0
  fi
  tfc_target_apply "mail DNS/rDNS" scope_mail_dns "" "${MAIL_DNS_ADDRS[@]}"
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

# 登録済みの判定は .name (MagicDNS 名) が <node> のまま、かつサーバー作成後に登録されたデバイスに限る。
# 旧デバイスが残ると新デバイスの名前は <node>-N になり、Ansible は旧デバイスへ接続して失敗する。
wait_tailscale_registered() {
  local node="$1" elapsed=0 timeout=600 token devices since
  since=$(curl -sf -H "Authorization: Bearer ${HCLOUD_TOKEN}" "${HCLOUD_API}/servers?name=${node}" \
    | jq -r '.servers[0].created // empty') || since=""
  while true; do
    if token=$(ts_token) && devices=$(ts_devices "${token}") && ts_node_registered "${devices}" "${node}" "${since}"; then
      record "${node} が Tailscale に接続しました"
      return 0
    fi
    if ((elapsed >= timeout)); then
      [[ -z "${devices:-}" ]] || ts_node_diag "${devices}" "${node}" | while read -r line; do log "  ${line}"; done
      die "${node} の Tailscale 接続がタイムアウトしました (${timeout}s)。旧デバイスが残ると MagicDNS 名が <node>-N になります"
    fi
    log "未接続 (${elapsed}s)"
    sleep 15
    elapsed=$((elapsed + 15))
  done
}

# 承認待ちや Terraform 実行中に main が進むと、Ansible 側の「HEAD が origin/main と一致」検査で
# 作り直し後に止まる。破壊的操作の前に同じ条件を確認して早期に止める。
# 戻り値: 0=一致・未コミット変更なし / 1=不一致または変更あり / 2=fetch 失敗
git_in_sync() {
  git -C "${REPO_ROOT}" fetch -q origin main || return 2
  [[ "$(git -C "${REPO_ROOT}" rev-parse HEAD)" == "$(git -C "${REPO_ROOT}" rev-parse origin/main)" ]] || return 1
  [[ -z "$(git -C "${REPO_ROOT}" status --porcelain)" ]]
}

# 新クラスターでは空の mailserver-data を sourcePVC とする ReplicationSource が最初に同期し、
# 同じ restic リポジトリの最新スナップショットが空になり、保持ポリシーの prune で障害前のものも消える。
# 作成直後に backup Job が走らないよう、DR の前にコミットで spec.paused: true にしておく必要がある。
mail_rs_paused() {
  grep -qE '^[[:space:]]+paused:[[:space:]]*true' "${REPO_ROOT}/gitops/manifests/prod/mailserver/replication-source.yaml"
}

# 冪等化済みの playbook でないと、再作成後の bootstrap が稼働中 Secret の空上書きや
# 入力不足のまま進みうる。破壊的操作の前に存在で検知する。
ansible_ready() {
  [[ -f "${REPO_ROOT}/ansible/playbooks/tasks/ensure_secret.yml" ]]
}

# OIDC kubeconfig を生成する。失敗時は旧ファイルを残して return 1 (生存確認では dead として扱うため die しない)。
refresh_kubeconfig() {
  bash "${KUBE_OIDC}" kubeconfig "${KUBECONFIG_FILE}.new" || { rm -f "${KUBECONFIG_FILE}.new"; return 1; }
  mv "${KUBECONFIG_FILE}.new" "${KUBECONFIG_FILE}"
}

run_ansible() {
  local node="$1"
  record "Ansible k3s-bootstrap を ${node} に限定して実行します"
  # cloud-init は tailscale up に --ssh を付けないため、k3s-upgrade.yml と同じ CI 専用デプロイ鍵で接続する。
  # 鍵は 0600 の一時ファイルに書き出し、終了時に削除する。
  SSH_KEY_FILE=$(mktemp)
  trap cleanup_all EXIT
  chmod 600 "${SSH_KEY_FILE}"
  printf '%s\n' "${CI_SSH_PRIVATE_KEY}" > "${SSH_KEY_FILE}"

  ANSIBLE_HOST_KEY_CHECKING=False \
    ANSIBLE_PRIVATE_KEY_FILE="${SSH_KEY_FILE}" \
    K3S_TOKEN="${K3S_TOKEN}" \
    CLOUDFLARE_TUNNEL_TOKEN="${CLOUDFLARE_TUNNEL_TOKEN}" \
    CLOUDFLARE_TUNNEL_ID="${CLOUDFLARE_TUNNEL_ID}" \
    INFISICAL_CLIENT_ID="${INFISICAL_CLIENT_ID}" \
    INFISICAL_CLIENT_SECRET="${INFISICAL_CLIENT_SECRET}" \
    INFISICAL_PROJECT_ID="${INFISICAL_PROJECT_ID}" \
    ARGOCD_GITHUB_DEPLOY_KEY="${ARGOCD_GITHUB_DEPLOY_KEY}" \
    timeout 2400 ansible-playbook -i "${DR_ANSIBLE_INVENTORY}" --limit "${node}" \
    "${REPO_ROOT}/ansible/playbooks/k3s-bootstrap.yml"
}

# 電源投入のみの経路。k3s と etcd のデータはディスクに残っているため bootstrap は流さず、
# ノードが Ready に戻るかだけを確認する。戻らなければ人が force で Ansible 再実行を判断する。
wait_k3s_ready() {
  local node="$1" elapsed=0 timeout=600 ready
  while true; do
    ready=$(kubectl_r get node "${node}" \
      -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)
    [[ "${ready}" == "True" ]] && { record "${node} が Ready に戻りました"; return 0; }
    ((elapsed >= timeout)) && die "${node} が Ready に戻りません (${timeout}s)。状態を確認し、必要なら force で Ansible から再実行してください"
    log "${node} Ready 待機中 (${elapsed}s)"
    sleep 15
    elapsed=$((elapsed + 15))
  done
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

# 引数: Application 一覧 JSON。出力: 未 Healthy な Application 名
argocd_unhealthy_apps() {
  echo "$1" | jq -r '.items[] | select(.status.health.status != "Healthy") | .metadata.name'
}

# 引数: Application 一覧 JSON, Application 名。出力: 管理下の Deployment/StatefulSet ("kind ns name")
app_workloads() {
  echo "$1" | jq -r --arg a "$2" '
    .items[] | select(.metadata.name == $a) | .status.resources[]?
    | select(.kind == "Deployment" or .kind == "StatefulSet")
    | "\(.kind) \(.namespace) \(.name)"'
}

# 管理下のワークロードが全て replicas=0 なら凍結中とみなす (ワークロードの無い Application は凍結扱いにしない)
app_is_frozen() {
  local json="$1" app="$2" kind ns name replicas found=0
  while read -r kind ns name; do
    [[ -n "${kind}" ]] || continue
    found=1
    replicas=$(kubectl_r get "${kind}" "${name}" -n "${ns}" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo "?")
    [[ "${replicas}" == "0" ]] || return 1
  done < <(app_workloads "${json}" "${app}")
  [[ "${found}" == "1" ]]
}

wait_argocd_healthy() {
  local elapsed=0 timeout=1200 json app pending
  while true; do
    json=$(kubectl_r get applications -n argocd -o json 2>/dev/null || echo '{"items":[]}')
    pending=()
    for app in $(argocd_unhealthy_apps "${json}"); do
      app_is_frozen "${json}" "${app}" || pending+=("${app}")
    done
    [[ "${#pending[@]}" -eq 0 && "$(echo "${json}" | jq '.items | length')" -gt 0 ]] \
      && { log "凍結中を除く全 ArgoCD Application が Healthy です"; return 0; }
    # mail-tls は cert-manager の sync 順に依存して欠け、mailserver が止まることがある
    repair_mail_tls
    ((elapsed >= timeout)) && die "ArgoCD の Healthy 待機がタイムアウトしました (未 Healthy: ${pending[*]:-なし})"
    log "Healthy でない Application: ${pending[*]:-(一覧取得待ち)} (${elapsed}s)"
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

# 待機より前に実行する。ESO が動かないと ArgoCD/CNPG の healthy 待機自体が成立しないため。
repair_bootstrap_secrets() {
  local client_id key_len
  client_id=$(kubectl_r get secret infisical-auth -n argocd -o jsonpath='{.data.clientId}' 2>/dev/null | base64 -d || true)
  key_len=$(kubectl_r get secret aramakisai-infra-repo -n argocd -o jsonpath='{.data.sshPrivateKey}' 2>/dev/null | base64 -d | wc -c || echo 0)

  if [[ -z "${client_id}" ]]; then
    record "infisical-auth が空のため修復します"
    kubectl_r create secret generic infisical-auth \
      --from-literal=clientId="${INFISICAL_CLIENT_ID}" \
      --from-literal=clientSecret="${INFISICAL_CLIENT_SECRET}" \
      -n argocd --dry-run=client -o yaml | kubectl_r apply -f -
    kubectl_r annotate externalsecret --all -A "force-sync=$(date +%s)" --overwrite || true
  fi

  if [[ "${key_len}" -lt 100 ]]; then
    [[ -n "${ARGOCD_GITHUB_DEPLOY_KEY:-}" ]] || die "Deploy Key が空ですが ARGOCD_GITHUB_DEPLOY_KEY が未設定のため修復できません"
    record "ArgoCD の Deploy Key が空のため修復します"
    kubectl_r create secret generic aramakisai-infra-repo \
      --from-literal=type=git \
      --from-literal=url=git@github.com:aramakisai/aramakisai-infra.git \
      --from-file=sshPrivateKey=<(printf '%s\n' "${ARGOCD_GITHUB_DEPLOY_KEY}") \
      -n argocd --dry-run=client -o yaml \
      | kubectl_r label --local -f - argocd.argoproj.io/secret-type=repository -o yaml \
      | kubectl_r apply -f -
  fi
}

# cert-manager の sync タイミングで mail-tls が作られず mailserver が ContainerCreating で止まることがある。
repair_mail_tls() {
  local reason start_epoch stuck f
  reason=$(kubectl_r get pod -n prod -l app=mailserver \
    -o jsonpath='{.items[0].status.containerStatuses[0].state.waiting.reason}' 2>/dev/null || true)
  [[ "${reason}" == "ContainerCreating" ]] || return 0

  start_epoch=$(date -d "$(kubectl_r get pod -n prod -l app=mailserver -o jsonpath='{.items[0].status.startTime}' 2>/dev/null)" +%s 2>/dev/null || date +%s)
  stuck=$(($(date +%s) - start_epoch))
  ((stuck >= 120)) || return 0

  if [[ -z "$(kubectl_r get secret mail-tls -n prod --ignore-not-found 2>/dev/null)" ]]; then
    record "mail-tls が無く mailserver が ${stuck}s 停止しているため certificate 関連を apply します"
    for f in certificate external-secret restic-external-secret; do
      kubectl_r apply -f "${REPO_ROOT}/gitops/manifests/prod/mailserver/${f}.yaml"
    done
  fi
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
  trap cleanup_all EXIT
  local vars=()
  if [[ "${DR_LOCAL_TEST}" != "1" ]]; then
    vars+=(DR_TARGET_NODE K3S_TOKEN ARGOCD_GITHUB_DEPLOY_KEY CLOUDFLARE_TUNNEL_TOKEN CLOUDFLARE_TUNNEL_ID
      INFISICAL_CLIENT_ID INFISICAL_CLIENT_SECRET INFISICAL_PROJECT_ID CI_SSH_PRIVATE_KEY)
  fi
  if [[ "${DR_SKIP_INFRA}" != "1" ]]; then
    vars+=(HCLOUD_TOKEN TAILSCALE_OAUTH_CLIENT_ID TAILSCALE_OAUTH_CLIENT_SECRET TAILSCALE_TAILNET TFC_API_TOKEN TFC_WORKSPACE_ID)
  fi
  local v
  for v in "${vars[@]}"; do
    [[ -n "${!v:-}" ]] || die "必須環境変数が未設定です: ${v}"
  done

  if [[ "${DR_SKIP_INFRA}" != "1" ]]; then
    validate_target_node "${DR_TARGET_NODE}" || die "DR_TARGET_NODE が不正です: ${DR_TARGET_NODE}"
  fi

  # ローカルテスト (k3d・KVM) は事前に用意した kubeconfig を使う。実運用は collect_signals と bootstrap 後に生成する
  if [[ "${DR_SKIP_INFRA}" == "1" ]]; then
    [[ -f "${KUBECONFIG_FILE}" ]] || die "KUBECONFIG_FILE が必要です"
  fi

  local need_ansible=0
  [[ "${DR_LOCAL_TEST}" == "1" ]] || need_ansible=1

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

    status=$(server_state "${DR_TARGET_NODE}")
    [[ "${status}" != "unknown" ]] || die "サーバー状態を確認できません (Hetzner / TFC state の取得失敗または不整合)"
    [[ "${status}" != "off" ]] && need_ansible=1 || need_ansible=0

    if [[ "${need_ansible}" == "1" ]]; then
      local sync_rc=0
      git_in_sync || sync_rc=$?
      [[ "${sync_rc}" == "0" ]] \
        || die "リポジトリが origin/main と一致しない、または未コミットの変更があります (rc=${sync_rc})。最新の main でワークフローを起動し直してください"
      ansible_ready || die "冪等化済みの k3s-bootstrap.yml (ansible/playbooks/tasks/ensure_secret.yml) がありません。Ansible を流す経路は停止します"
    fi

    if [[ "${status}" == "absent" ]]; then
      mail_rs_paused || die "gitops/manifests/prod/mailserver/replication-source.yaml が spec.paused: true ではありません。新クラスターの最初のバックアップで restic の最新・過去スナップショットを失わないよう、先に paused: true をコミットしてから起動し直してください (docs/dr-runbook.md)"
    fi

    case "${status}" in
      absent)
        recreate_node "${DR_TARGET_NODE}"
        update_mail_dns "${DR_TARGET_NODE}"
        ;;
      off) poweron_node "${DR_TARGET_NODE}" ;;
      *) log "サーバー状態 ${status}: DR_FORCE のためインフラ操作なしで Ansible から再実行します" ;;
    esac
    wait_tailscale_registered "${DR_TARGET_NODE}"
    [[ "${status}" != "off" ]] || wait_k3s_ready "${DR_TARGET_NODE}"
  fi

  if [[ "${need_ansible}" == "1" ]]; then
    run_ansible "${DR_TARGET_NODE}"
    # クラスター再作成で CA が変わるため作り直す
    [[ "${DR_SKIP_INFRA}" == "1" ]] || refresh_kubeconfig || die "bootstrap 後の OIDC kubeconfig の生成に失敗しました"
  fi

  repair_bootstrap_secrets
  wait_argocd_healthy
  wait_cnpg_healthy

  report_cnpg_recovery_points
  record "復旧完了"
}

if [[ "${BASH_SOURCE[0]:-$0}" == "${0}" ]]; then
  main "$@"
fi
