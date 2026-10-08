#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# festival-peak-scaleout の増減手順を Hetzner 実機で検証するハーネス
#
# 使い方 (docs/node-scaling-runbook.md の「検証ハーネス」参照):
#   infisical run --env=staging --path=/scaletest -- scripts/scaletest/scaletest.sh <command> [args]
# infisical run は .infisical.json のあるリポジトリ (worktree を含む) の中で実行する。
# 外では接続先プロジェクトを解決できず環境変数が注入されない。
#
# 本番 (--env=prod) では実行しない。検証専用の Hetzner プロジェクト・Tailscale OAuth
# クライアント (tag:scaletest 限定)・K3s トークンだけを使い、本番の資源には到達しない。
#
# 必須の環境変数 (Infisical staging の /scaletest から注入):
#   SCALETEST_HCLOUD_TOKEN SCALETEST_TS_CLIENT_ID SCALETEST_TS_CLIENT_SECRET SCALETEST_TAILNET
#   K3S_TOKEN (bootstrap のみ)
# 任意:
#   SCALETEST_WORKDIR      作業ディレクトリ (既定: mktemp -d。ホーム配下は不可)
#   SCALETEST_SERVER_TYPE  サーバータイプ (既定: cx23)
#
# コマンド:
#   preflight               本番混入ガードのみ実行
#   up [N...]               network / firewall を (無ければ) 作成し、サーバー N (既定 1 2 3) のうち
#                           未作成のものだけ作成して Tailscale 登録を待つ (既存はスキップ)
#   bootstrap [LIMIT]       検証 playbook を実行 (LIMIT は ansible --limit。既定 scaletest-1,scaletest-2,scaletest-3)
#   status                  サーバー・Tailscale デバイス
#   members                 etcd メンバー一覧 (etcdctl)
#   kubectl ARGS...         scaletest-1 上で kubectl を実行
#   isolation               tag:scaletest から本番ノードと他の検証ノードへ tailnet で到達できないことを確認
#   devices                 scaletest-N の Tailscale デバイス一覧
#   purge-devices [N]       scaletest-N (省略時は全て) の Tailscale デバイスを削除
#   delete-server N         サーバー N を削除
#   down                    サーバー・firewall・network・Tailscale デバイスを全て削除し、0 件を確認
#   verify-clean            残存が 0 件であることを確認
# ============================================================

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HAPI=https://api.hetzner.cloud/v1
TS_API=https://api.tailscale.com/api/v2
NET_NAME=scaletest-net
FW_NAME=scaletest-fw
SSH_KEY_NAME=scaletest-key
STYPE="${SCALETEST_SERVER_TYPE:-cx23}"
# recovery.sh と同じく .hostname 基準。重複デバイスは .name だけが -N になり .hostname は変わらない
DEV_RE='^scaletest-[0-9]+$'
PLAYBOOK="${REPO}/ansible/playbooks/scaletest-bootstrap.yml"
INVENTORY="${REPO}/ansible/inventory/scaletest.yml"
# etcd 公式リリースの SHA256SUMS と照合済みの値
ETCD_VERSION=v3.6.15
ETCD_SHA256=b51d86e6bff2168de5ba8cd631c0fe6c785c7a1b32b083079f5677d6b7e2772c

die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo "[$(date +%H:%M:%S)] $*"; }

# --- 作業ディレクトリ ---------------------------------------
if [[ -n "${SCALETEST_WORKDIR:-}" ]]; then
  WORKDIR="${SCALETEST_WORKDIR}"
  mkdir -p "${WORKDIR}"
  KEEP_WORKDIR=true
else
  WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/scaletest.XXXXXX")"
  KEEP_WORKDIR=false
fi
WORKDIR="$(cd "${WORKDIR}" && pwd)"
case "${WORKDIR}/" in "${HOME}"/*) die "作業ディレクトリはホーム配下に置けません: ${WORKDIR}" ;; esac
cleanup() { [[ "${KEEP_WORKDIR}" == true ]] || rm -rf "${WORKDIR}"; }
trap cleanup EXIT

SSH_OPTS=(-o ConnectTimeout=10 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)

# --- API ヘルパ ---------------------------------------------
# トークンは curl の設定を stdin 相当 (プロセス置換) で渡し、ps に出さない
hc() {
  curl -sS --fail-with-body --config <(printf 'header = "Authorization: Bearer %s"\n' "${SCALETEST_HCLOUD_TOKEN}") \
    -H 'Content-Type: application/json' "$@"
}

ts_token() {
  curl -sf --config <(printf 'data = "client_id=%s&client_secret=%s"\n' "${SCALETEST_TS_CLIENT_ID}" "${SCALETEST_TS_CLIENT_SECRET}") \
    -X POST "${TS_API}/oauth/token" | jq -r '.access_token // empty'
}

ts_api() { # ts_api METHOD PATH [curl args...]
  local method="$1" path="$2" token
  shift 2
  token="$(ts_token)" || die "Tailscale のトークン取得に失敗しました"
  [[ -n "${token}" ]] || die "Tailscale のトークン取得に失敗しました"
  curl -sf --config <(printf 'header = "Authorization: Bearer %s"\n' "${token}") -X "${method}" "${TS_API}${path}" "$@"
}

ts_devices() {
  ts_api GET "/tailnet/${SCALETEST_TAILNET}/devices" |
    jq -c --arg re "${DEV_RE}" '[.devices[] | select((.hostname // "") | test($re))]'
}

# --- 本番混入ガード -----------------------------------------
preflight() {
  local v bad=()
  for v in HCLOUD_TOKEN TAILSCALE_OAUTH_CLIENT_ID TAILSCALE_OAUTH_CLIENT_SECRET TAILSCALE_TAILNET \
    CLOUDFLARE_API_TOKEN CLOUDFLARE_TUNNEL_TOKEN ARGOCD_GITHUB_DEPLOY_KEY DISCORD_OPS_WEBHOOK_URL \
    INFISICAL_CLIENT_ID INFISICAL_CLIENT_SECRET; do
    [[ -z "${!v:-}" ]] || bad+=("${v}")
  done
  while IFS= read -r v; do bad+=("${v}"); done < <(compgen -e | grep -E '^(TF_VAR_|B2_|AWS_)' || true)
  if ((${#bad[@]} > 0)); then
    die "本番用の変数が環境にあります (${bad[*]})。'--env=prod' ではなく '--env=staging --path=/scaletest' で実行してください"
  fi
  for v in SCALETEST_HCLOUD_TOKEN SCALETEST_TS_CLIENT_ID SCALETEST_TS_CLIENT_SECRET SCALETEST_TAILNET; do
    [[ -n "${!v:-}" ]] || die "${v} が未設定です"
  done

  # kubectl はノード上の k3s を SSH 経由で使い、手元の kubeconfig / context は参照しない
  [[ -z "${KUBECONFIG:-}" ]] || die "KUBECONFIG が設定されています。unset してください"

  grep -q 'prod-node' "${INVENTORY}" && die "検証用 inventory に prod-node が含まれています"
  local servers
  servers="$(hc "${HAPI}/servers" | jq -r '.servers[].name')" || die "Hetzner API に接続できません (トークンを確認)"
  if grep -qvE '^scaletest-[0-9]+$' <<<"${servers}" && [[ -n "${servers}" ]]; then
    die "Hetzner プロジェクトに scaletest-N 以外のサーバーがあります。検証専用プロジェクトのトークンか確認してください"
  fi
  log "preflight OK"
}

# --- Hetzner ------------------------------------------------
lookup_id() { # lookup_id <collection> <name> -> id (無ければ空)
  hc "${HAPI}/$1?name=$2" | jq -r --arg c "$1" '.[$c][0].id // empty'
}

ensure_network() {
  NET="$(lookup_id networks "${NET_NAME}")"
  if [[ -z "${NET}" ]]; then
    NET="$(hc -X POST "${HAPI}/networks" -d "{\"name\":\"${NET_NAME}\",\"ip_range\":\"10.250.0.0/16\",\"labels\":{\"purpose\":\"scaletest\"}}" | jq -r .network.id)"
    hc -X POST "${HAPI}/networks/${NET}/actions/add_subnet" \
      -d '{"type":"cloud","network_zone":"eu-central","ip_range":"10.250.1.0/24"}' >/dev/null
    log "network 作成: ${NET_NAME}"
  fi
  FW="$(lookup_id firewalls "${FW_NAME}")"
  if [[ -z "${FW}" ]]; then
    # public SSH と K3s のポートは開けない。ノード間は private network、管理は Tailscale のみ
    FW="$(hc -X POST "${HAPI}/firewalls" -d '{"name":"'"${FW_NAME}"'","labels":{"purpose":"scaletest"},"rules":[
      {"direction":"in","protocol":"udp","port":"41641","source_ips":["0.0.0.0/0","::/0"],"description":"tailscale"},
      {"direction":"in","protocol":"icmp","source_ips":["0.0.0.0/0","::/0"]}]}' | jq -r .firewall.id)"
    log "firewall 作成: ${FW_NAME}"
  fi
  SSHK="$(lookup_id ssh_keys "${SSH_KEY_NAME}")"
  [[ -n "${SSHK}" ]] || die "Hetzner プロジェクトに SSH 鍵 ${SSH_KEY_NAME} がありません"
}

issue_ts_key() {
  # 本番 tailscale.tf と同条件 (reusable / 非 ephemeral / preauthorized / 1h)。タグだけ tag:scaletest。
  # 非 ephemeral なのは、サーバー削除後にデバイスが残る挙動を検証対象に含めるため
  ts_api POST "/tailnet/${SCALETEST_TAILNET}/keys" -H 'Content-Type: application/json' \
    -d '{"capabilities":{"devices":{"create":{"reusable":true,"ephemeral":false,"preauthorized":true,"tags":["tag:scaletest"]}}},"expirySeconds":3600,"description":"scaletest"}' |
    jq -r .key
}

create_server() { # create_server N TS_KEY
  local n="$1" key="$2" name="scaletest-$1" tpl user_data
  tpl="$(<"${REPO}/terraform/templates/cloud-init.yaml.tpl")"
  # shellcheck disable=SC2016 # テンプレートのプレースホルダ ${...} は展開しない
  user_data="${tpl//'${hostname}'/${name}}"
  # shellcheck disable=SC2016
  user_data="${user_data//'${tailscale_auth_key}'/${key}}"
  # auth key を含むため --arg ではなくプロセス置換で渡し、ps に出さない
  local sid
  sid="$(jq -n --arg n "${name}" --arg t "${STYPE}" --argjson k "${SSHK}" --argjson f "${FW}" --rawfile u <(printf '%s' "${user_data}") \
    '{name:$n,server_type:$t,image:"debian-13",location:"fsn1",ssh_keys:[$k],firewalls:[{firewall:$f}],user_data:$u,
      public_net:{enable_ipv4:true,enable_ipv6:true},labels:{purpose:"scaletest"}}' |
    hc -X POST "${HAPI}/servers" -d @- | jq -r .server.id)"
  # 本番 terraform と同じ固定 IP。起動中でも attach できる
  hc -X POST "${HAPI}/servers/${sid}/actions/attach_to_network" -d "{\"network\":${NET},\"ip\":\"10.250.1.${n}\"}" >/dev/null
  log "${name} 作成 (id=${sid}, ip=10.250.1.${n})"
}

wait_registered() { # wait_registered N...
  local n elapsed=0 timeout=600 devs missing
  while true; do
    devs="$(ts_devices)"
    missing=()
    for n in "$@"; do
      jq -e --arg h "scaletest-${n}" '[.[] | select(.hostname == $h and .connectedToControl == true)] | length > 0' <<<"${devs}" >/dev/null ||
        missing+=("scaletest-${n}")
    done
    if ((${#missing[@]} == 0)); then
      jq -r '.[] | select(.connectedToControl == true) | "  hostname=\(.hostname) name=\(.name) tags=\(.tags // [] | join(","))"' <<<"${devs}"
      return 0
    fi
    ((elapsed < timeout)) || die "Tailscale 登録がタイムアウトしました (${timeout}s): ${missing[*]}"
    log "未登録: ${missing[*]} (${elapsed}s)"
    sleep 15
    elapsed=$((elapsed + 15))
  done
}

cmd_up() {
  local nodes=("$@") todo=() n key=""
  ((${#nodes[@]} > 0)) || nodes=(1 2 3)
  ensure_network
  for n in "${nodes[@]}"; do
    if [[ -n "$(lookup_id servers "scaletest-${n}")" ]]; then log "scaletest-${n} は既に存在するためスキップ"; else todo+=("${n}"); fi
  done
  # 作成対象があるときだけ auth key を発行する (不要な key を残さない)
  if ((${#todo[@]} > 0)); then
    key="$(issue_ts_key)"
    [[ -n "${key}" ]] || die "Tailscale auth key の発行に失敗しました (OAuth クライアントのスコープとタグを確認)"
    for n in "${todo[@]}"; do create_server "${n}" "${key}"; done
    key=""
  fi
  wait_registered "${nodes[@]}"
}

cmd_delete_server() {
  local id
  id="$(lookup_id servers "scaletest-$1")"
  [[ -n "${id}" ]] || die "scaletest-$1 がありません"
  hc -X DELETE "${HAPI}/servers/${id}" >/dev/null
  log "scaletest-$1 を削除しました"
}

# --- Ansible ------------------------------------------------
cmd_bootstrap() {
  local limit="${1:-scaletest-1,scaletest-2,scaletest-3}" log_file rc=0 start
  [[ -n "${K3S_TOKEN:-}" ]] || die "K3S_TOKEN が未設定です"
  [[ "${limit}" =~ ^(scaletest-[0-9]+)(,scaletest-[0-9]+)*$ ]] || die "LIMIT は scaletest-N のカンマ区切りで指定してください: ${limit}"
  # ansible.cfg が repo 直下・ansible/ にあり、既定 inventory が本番を指すため、空の設定を明示して拾わせない
  printf '[defaults]\nhost_key_checking = False\n' >"${WORKDIR}/ansible.cfg"
  export ANSIBLE_CONFIG="${WORKDIR}/ansible.cfg" ANSIBLE_ROLES_PATH="${REPO}/ansible/roles" ANSIBLE_HOST_KEY_CHECKING=False

  # 本番側の更新に追従させるため、バージョンと values は本番の playbook・inventory から読む
  python3 - "${REPO}/ansible/playbooks/k3s-bootstrap.yml" "${REPO}/ansible/inventory/tailscale.yml" >"${WORKDIR}/prod-vars.json" <<'PY'
import json, sys, yaml
with open(sys.argv[1]) as f:
    plays = yaml.safe_load(f)
with open(sys.argv[2]) as f:
    inv = yaml.safe_load(f)
v = next(p["vars"] for p in plays if "cilium_version" in p.get("vars", {}))
print(json.dumps({
    "helm_version": v["helm_version"],
    "cilium_version": v["cilium_version"],
    "cilium_values": v["cilium_values"],
    "k3s_version": inv["all"]["vars"]["k3s_version"],
}))
PY

  local listed
  listed="$(ansible-inventory -i "${INVENTORY}" --list </dev/null)"
  ! grep -q 'prod-node' <<<"${listed}" || die "検証用 inventory に prod-node が解決されました"
  listed="$(ansible-playbook -i "${INVENTORY}" "${PLAYBOOK}" --limit "${limit}" -e "@${WORKDIR}/prod-vars.json" --list-hosts </dev/null)"
  # --list-hosts のホスト行は 6 桁インデント。assert 用の localhost Play は除く
  ! grep -E '^ {6}[^ ]' <<<"${listed}" | grep -v 'localhost' | grep -qv 'scaletest-' || die "対象に scaletest- 以外が含まれます"

  # 数分〜十数分かかる。標準入力を閉じ、出力はファイルへ (端末にはサマリだけ出す)
  log_file="${WORKDIR}/ansible.log"
  start=$(date +%s)
  log "ansible-playbook 開始 (limit=${limit})"
  ansible-playbook -i "${INVENTORY}" "${PLAYBOOK}" --limit "${limit}" -e "@${WORKDIR}/prod-vars.json" \
    </dev/null >"${log_file}" 2>&1 || rc=$?
  log "ansible-playbook 終了 rc=${rc} ($(($(date +%s) - start))s)"
  grep -A6 'PLAY RECAP' "${log_file}" | tail -7 || true
  if ((rc != 0)); then tail -40 "${log_file}"; fi
  return "${rc}"
}

# --- ノード操作 ---------------------------------------------
# shellcheck disable=SC2029 # コマンドはクライアント側で展開してよい
node_ssh() { local host="$1"; shift; ssh "${SSH_OPTS[@]}" "root@${host}" "$@" </dev/null; }

cmd_kubectl() {
  # 引数は repo 管理の操作者入力。リモートシェルで解釈される点に注意
  node_ssh scaletest-1 "KUBECONFIG=/etc/rancher/k3s/k3s.yaml k3s kubectl $*"
}

cmd_members() {
  # K3s は etcdctl を同梱しない。HTTP ゲートウェイは使えないため etcdctl を取得して配置する
  local tgz="${WORKDIR}/etcd.tgz"
  if ! node_ssh scaletest-1 'test -x /tmp/etcdctl'; then
    curl -fsSL -o "${tgz}" "https://github.com/etcd-io/etcd/releases/download/${ETCD_VERSION}/etcd-${ETCD_VERSION}-linux-amd64.tar.gz"
    echo "${ETCD_SHA256}  ${tgz}" | sha256sum -c --status || die "etcd のチェックサムが一致しません"
    tar -xzf "${tgz}" -C "${WORKDIR}" --strip-components=1 "etcd-${ETCD_VERSION}-linux-amd64/etcdctl"
    scp "${SSH_OPTS[@]}" "${WORKDIR}/etcdctl" root@scaletest-1:/tmp/etcdctl
  fi
  # shellcheck disable=SC2016 # $D はリモート側で展開する
  node_ssh scaletest-1 'D=/var/lib/rancher/k3s/server/tls/etcd
    /tmp/etcdctl --endpoints=https://127.0.0.1:2379 --cacert=$D/server-ca.crt --cert=$D/client.crt --key=$D/client.key member list -w simple'
}

cmd_isolation() {
  # tailscale ping は --c=N 形式で、名前でなく IP を渡す (名前だと解決に失敗して判定にならない)
  local prod_ip peer_ip
  prod_ip="$(tailscale ip -4 prod-node-1 2>/dev/null || true)"
  if [[ -z "${prod_ip}" ]]; then
    log "SKIP: 手元から prod-node-1 の tailnet IP を解決できないため、本番ノードへの遮断は確認できません"
  elif node_ssh scaletest-1 "tailscale ping --c=1 --timeout=5s ${prod_ip}" >/dev/null 2>&1; then
    die "scaletest-1 から本番ノードへ到達できました。ACL を確認し、検証を中止してください"
  else
    log "OK: scaletest-1 から prod-node-1 へ tailnet で到達できない"
  fi
  peer_ip="$(tailscale ip -4 scaletest-2 2>/dev/null || true)"
  if [[ -n "${peer_ip}" ]]; then
    if node_ssh scaletest-1 "tailscale ping --c=1 --timeout=5s ${peer_ip}" >/dev/null 2>&1; then
      die "scaletest-1 から scaletest-2 へ tailnet で到達できました (tag:scaletest 同士は不可のはず)"
    fi
    log "OK: scaletest-1 から scaletest-2 へ tailnet で到達できない"
  fi
  node_ssh scaletest-1 'ping -c1 -W3 10.250.1.2' >/dev/null && log "OK: private IP (10.250.1.2) は到達できる"
}

# --- Tailscale デバイス -------------------------------------
cmd_devices() {
  ts_devices | jq -r '.[] | [.id, .hostname, .name, (.connectedToControl|tostring), (.lastSeen // "")] | @tsv'
}

cmd_purge_devices() {
  local only="${1:-}" ids id
  ids="$(ts_devices | jq -r --arg o "${only}" '.[] | select($o == "" or .hostname == ("scaletest-" + $o)) | .id')"
  if [[ -z "${ids}" ]]; then log "削除対象のデバイスはありません"; return 0; fi
  cmd_devices
  # 一覧は DEV_RE (scaletest-N) に一致したものだけ。本番デバイスは構造上含まれない
  for id in ${ids}; do
    ts_api DELETE "/device/${id}" >/dev/null && log "device ${id} を削除"
  done
}

# --- 後片付け -----------------------------------------------
delete_all() { # delete_all <collection> -> 専用プロジェクト内で purpose=scaletest のものを全削除
  local id
  for id in $(hc "${HAPI}/$1?label_selector=purpose=scaletest" | jq -r --arg c "$1" '.[$c][].id'); do
    hc -X DELETE "${HAPI}/$1/${id}" >/dev/null && log "$1/${id} を削除"
  done
}

cmd_down() {
  local id
  for id in $(hc "${HAPI}/servers?label_selector=purpose=scaletest" | jq -r '.servers[].id'); do
    hc -X DELETE "${HAPI}/servers/${id}" >/dev/null && log "servers/${id} を削除"
  done
  # サーバーの削除完了前は firewall / network を外せない
  local waited=0
  while [[ "$(hc "${HAPI}/servers" | jq '.servers | length')" != 0 ]]; do
    ((waited < 120)) || die "サーバーの削除が完了しません"
    sleep 5
    waited=$((waited + 5))
  done
  delete_all firewalls
  delete_all networks
  # primary IP はサーバー削除の後に非同期で消える
  waited=0
  while [[ "$(hc "${HAPI}/primary_ips" | jq '.primary_ips | length')" != 0 ]]; do
    ((waited < 120)) || die "primary IP の削除が完了しません"
    sleep 5
    waited=$((waited + 5))
  done
  cmd_purge_devices
  cmd_verify_clean
}

cmd_verify_clean() {
  local c leftover=0 n
  for c in servers networks firewalls volumes primary_ips; do
    n="$(hc "${HAPI}/${c}" | jq --arg c "${c}" '.[$c] | length')"
    echo "hetzner ${c}: ${n}"
    [[ "${n}" == 0 ]] || leftover=1
  done
  n="$(ts_devices | jq 'length')"
  echo "tailscale scaletest devices: ${n}"
  [[ "${n}" == 0 ]] || leftover=1
  ((leftover == 0)) || die "検証用リソースが残っています"
  log "残存なし"
}

cmd_status() {
  hc "${HAPI}/servers" | jq -r '.servers[] | [.name, .status, (.private_net[0].ip // "-")] | @tsv'
  cmd_devices
}

main() {
  local cmd="${1:-}"
  [[ -n "${cmd}" ]] || die "コマンドを指定してください (先頭のコメント参照)"
  shift
  preflight
  case "${cmd}" in
    preflight) ;;
    up) cmd_up "$@" ;;
    bootstrap) cmd_bootstrap "$@" ;;
    status) cmd_status ;;
    members) cmd_members ;;
    kubectl) cmd_kubectl "$@" ;;
    isolation) cmd_isolation ;;
    devices) cmd_devices ;;
    purge-devices) cmd_purge_devices "$@" ;;
    delete-server) cmd_delete_server "${1:?サーバー番号を指定してください}" ;;
    down) cmd_down ;;
    verify-clean) cmd_verify_clean ;;
    *) die "未知のコマンド: ${cmd}" ;;
  esac
}

main "$@"
