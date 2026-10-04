#!/usr/bin/env bash
# k3s-server ロールが描画した認証設定・監査ポリシー・起動引数を、本番と同じ版の使い捨て k3s (docker) に
# 入れ、OIDC 判定・署名期間の上限・監査ログを検証する (kube-github-auth tasks 3.5・3.6)。
# 本番・Infisical・make kubectl には触れない。kubectl はコンテナ内の kubectl だけを使う。
#
# 使い方:
#   ./scripts/verify-k3s-authn-disposable.sh
# 前提: docker (sudo 不要)、ansible-playbook、python3 (PyYAML・PyJWT・cryptography)、openssl、jq、curl

# ok/ng は常に成功するため A && ok || ng は if-else として安全。jq の式は意図して単一引用符で渡す
# shellcheck disable=SC2015,SC2016
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROLE="${ROOT}/ansible/roles/k3s-server"
PY="${ROOT}/scripts/verify-k3s-authn-disposable.py"
K3S_VERSION="$(sed -n 's/^ *k3s_version: *//p' "${ROOT}/ansible/inventory/tailscale.yml" | head -1)"
IMAGE="${K3S_IMAGE:-rancher/k3s:${K3S_VERSION//+/-}}"
READY_TIMEOUT="${K3S_APISERVER_READY_TIMEOUT:-120}"
# 総量の頭打ちを短時間で確認するため、ローテーション上限だけ小さく上書きする
AUDIT_MAX_MB=1
AUDIT_MAX_BACKUP=2

WORK="$(mktemp -d)"
TAG="k3s-authn-verify-$$"
NET="${TAG}-net"
CONTAINERS=()
MOCK_PID=""

cleanup() {
  [[ -n "${MOCK_PID}" ]] && kill "${MOCK_PID}" 2>/dev/null
  for c in "${CONTAINERS[@]}"; do docker rm -fv "${c}" >/dev/null 2>&1; done
  docker network rm "${NET}" >/dev/null 2>&1
  rm -rf "${WORK}"
}
trap cleanup EXIT

PASS=0
FAIL=0
ok() { echo "  PASS $1"; PASS=$((PASS + 1)); }
ng() { echo "  FAIL $1"; FAIL=$((FAIL + 1)); }
check() { # check <desc> <cmd...>
  local d="$1"; shift
  if "$@" >/dev/null 2>&1; then ok "${d}"; else ng "${d}"; fi
}

# --- 描画 (ansible は stdout が非ブロッキングだと起動しないため cat 経由) ---
render() { # render <outdir> <private_ip>
  mkdir -p "$1"
  cat >"${WORK}/render.yml" <<PLAY
- hosts: localhost
  gather_facts: false
  connection: local
  vars_files: [${ROLE}/defaults/main.yml]
  vars:
    ansible_host: k3s.test
    k3s_private_ip: $2
    k3s_cluster_init: true
  tasks:
    - ansible.builtin.template: {src: "${ROLE}/templates/authentication-config.yaml.j2", dest: "$1/authentication-config.yaml", mode: "0600"}
    - ansible.builtin.template: {src: "${ROLE}/templates/audit-policy.yaml.j2", dest: "$1/audit-policy.yaml", mode: "0600"}
    - ansible.builtin.template: {src: "${ROLE}/templates/config.yaml.j2", dest: "$1/config.yaml", mode: "0600"}
PLAY
  ANSIBLE_LOCALHOST_WARNING=False ANSIBLE_INVENTORY_UNPARSED_WARNING=False \
    ansible-playbook -i localhost, -e k3s_audit_log_max_size_mb="${AUDIT_MAX_MB}" -e k3s_audit_log_max_backup="${AUDIT_MAX_BACKUP}" "${WORK}/render.yml" </dev/null 2>&1 | cat >"${WORK}/render.log"
  [[ "${PIPESTATUS[0]}" -eq 0 ]] || { cat "${WORK}/render.log"; return 1; }
}

# --- コンテナ ---
start() { # start <name> <confdir> [extra docker args...]
  local name="${TAG}-$1" conf="$2"; shift 2
  CONTAINERS+=("${name}")
  docker create --name "${name}" --privileged --network "${NET}" --tmpfs /run --tmpfs /var/run \
    -p 127.0.0.1::6443 "$@" "${IMAGE}" server >/dev/null || return 1
  # イメージに /etc/rancher がなく、docker cp は親ディレクトリを作らないため、k3s/ を内包する木ごと渡す
  rm -rf "${WORK}/tree-${name}" && mkdir -p "${WORK}/tree-${name}" && cp -r "${conf}" "${WORK}/tree-${name}/k3s" || return 1
  docker cp "${WORK}/tree-${name}" "${name}:/etc/rancher" || return 1
  docker start "${name}" >/dev/null
}
cname() { echo "${TAG}-$1"; }
kc() { docker exec -i -e KUBECONFIG=/etc/rancher/k3s/k3s.yaml "$(cname "$C")" kubectl "$@"; }
api_port() { docker port "$(cname "$1")" 6443/tcp | head -1 | sed 's/.*://'; }

# ロールの起動確認 (tasks/main.yml) と同じ判定 (イメージの k3s kubectl は "unknown command" になるため同じバイナリの kubectl リンクを使う): ローカル admin の /readyz が期限内に ok、匿名の /version が 401
role_check() { # role_check <name> <timeout_s>
  local n="$1" t="$2" end out code
  end=$((SECONDS + t))
  until out="$(docker exec -e KUBECONFIG=/etc/rancher/k3s/k3s.yaml "$(cname "$n")" kubectl get --raw=/readyz 2>/dev/null)" && [[ "${out}" == ok ]]; do
    ((SECONDS >= end)) && return 1
    sleep 5
  done
  code="$(curl -sk -o /dev/null -w '%{http_code}' "https://127.0.0.1:$(api_port "$n")/version")"
  [[ "${code}" == 401 ]]
}

# --- 準備: ネットワーク・モック発行者 ---
docker network create "${NET}" >/dev/null || exit 1
GW="$(docker network inspect "${NET}" -f '{{(index .IPAM.Config 0).Gateway}}')"
PREFIX="${GW%.*}"
IP_A="${PREFIX}.10"
MOCK_PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("",0));print(s.getsockname()[1])')"
DEAD_PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("",0));print(s.getsockname()[1])')"
ISSUER="https://${GW}:${MOCK_PORT}"

openssl req -x509 -newkey rsa:2048 -nodes -days 1 -keyout "${WORK}/ca.key" -out "${WORK}/ca.pem" -subj "/CN=verify-mock-ca" 2>/dev/null
openssl req -newkey rsa:2048 -nodes -keyout "${WORK}/srv.key" -out "${WORK}/srv.csr" -subj "/CN=mock-issuer" 2>/dev/null
openssl x509 -req -in "${WORK}/srv.csr" -CA "${WORK}/ca.pem" -CAkey "${WORK}/ca.key" -CAcreateserial -days 1 \
  -extfile <(echo "subjectAltName=IP:${GW}") -out "${WORK}/srv.pem" 2>/dev/null
openssl genrsa -out "${WORK}/sign.key" 2048 2>/dev/null
python3 "${PY}" serve --issuer "${ISSUER}" --bind "${GW}" --port "${MOCK_PORT}" --cert "${WORK}/srv.pem" \
  --tls-key "${WORK}/srv.key" --key "${WORK}/sign.key" &
MOCK_PID=$!

render "${WORK}/base" "${IP_A}" || exit 1
python3 "${PY}" patch --inp "${WORK}/base/authentication-config.yaml" --out "${WORK}/base/authentication-config.yaml.new" \
  --issuer "${ISSUER}" --ca "${WORK}/ca.pem"
mkdir -p "${WORK}/conf-a"
cp "${WORK}/base/audit-policy.yaml" "${WORK}/base/config.yaml" "${WORK}/conf-a/"
mv "${WORK}/base/authentication-config.yaml.new" "${WORK}/conf-a/authentication-config.yaml"

JUDGE_ARGS=(--defaults "${ROLE}/defaults/main.yml" --key "${WORK}/sign.key")

echo "=== 3.5 / 3.6 本体 (発行者に到達できる k3s ${K3S_VERSION}) ==="
C=a
start a "${WORK}/conf-a" --ip "${IP_A}" || { echo "コンテナ起動に失敗"; exit 1; }
if role_check a "${READY_TIMEOUT}"; then ok "起動し、ローカル admin の /readyz が ok・匿名 /version が 401 (ロールの起動確認と同じ判定)"
else
  ng "起動確認"
  kc get --raw '/readyz?verbose' 2>&1 | grep -v ' ok$' | head -20
  exit 1
fi
PORT_A="$(api_port a)"

check "ローカル admin (x509) で API を呼べる" kc get ns
for p in /healthz /readyz /api/v1/namespaces; do
  [[ "$(curl -sk -o /dev/null -w '%{http_code}' "https://127.0.0.1:${PORT_A}${p}")" == 401 ]] && ok "匿名 ${p} が 401" || ng "匿名 ${p} が 401"
done

echo "--- OIDC 判定 (許可と違反の全ケース) ---"
if python3 "${PY}" judge "${JUDGE_ARGS[@]}" --issuer "${ISSUER}" --api "127.0.0.1:${PORT_A}" | tee "${WORK}/judge.log"; then
  ok "OIDC 判定がすべて期待どおり ($(tail -1 "${WORK}/judge.log"))"
else ng "OIDC 判定に不一致あり"; fi

echo "--- 3.6 CSR の署名期間 ---"
mkreq() { # mkreq <name> <subj> <expirationSeconds|""> -> stdout: CSR manifest。鍵は ${WORK}/<name>.key
  openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -keyout "${WORK}/$1.key" \
    -subj "$2" -out "${WORK}/$1.csr" 2>/dev/null
  cat <<YAML
apiVersion: certificates.k8s.io/v1
kind: CertificateSigningRequest
metadata: {name: $1}
spec:
  request: $(base64 -w0 "${WORK}/$1.csr")
  signerName: kubernetes.io/kube-apiserver-client
  usages: [client auth]
$([[ -n "$3" ]] && echo "  expirationSeconds: $3")
YAML
}
issue() { # issue <name> <subj> <expirationSeconds|""> -> ${WORK}/<name>.crt
  mkreq "$1" "$2" "$3" | kc create -f - >/dev/null || return 1
  kc certificate approve "$1" >/dev/null || return 1
  local crt
  for _ in $(seq 1 30); do
    crt="$(kc get csr "$1" -o jsonpath='{.status.certificate}')"
    [[ -n "${crt}" ]] && { base64 -d <<<"${crt}" >"${WORK}/$1.crt"; return 0; }
    sleep 1
  done
  return 1
}
secs() { # secs <crt> -> notAfter - notBefore
  local nb na
  nb="$(date -d "$(openssl x509 -in "$1" -noout -startdate | cut -d= -f2)" +%s)"
  na="$(date -d "$(openssl x509 -in "$1" -noout -enddate | cut -d= -f2)" +%s)"
  echo $((na - nb))
}
if issue long "/CN=github:verify:4242" 31536000; then
  d="$(secs "${WORK}/long.crt")"
  [[ "${d}" -eq 604800 ]] && ok "expirationSeconds=1年 の CSR が 168h に切り詰められる (${d}s)" || ng "168h への切り詰め (${d}s)"
  openssl x509 -in "${WORK}/long.crt" -noout -issuer | grep -q 'k3s-client-ca' && ok "client CA で署名される" || ng "client CA で署名される"
else ng "CSR の作成・承認・署名"; fi
if issue unset "/CN=github:verify-unset:1" ""; then
  d="$(secs "${WORK}/unset.crt")"
  [[ "${d}" -eq 604800 ]] && ok "expirationSeconds 未指定も 168h (${d}s)" || ng "未指定の期間 (${d}s)"
else ng "expirationSeconds 未指定の CSR"; fi
if mkreq masters "/O=system:masters/CN=github:evil:1" "" | kc create -f - >"${WORK}/masters.out" 2>&1; then
  ng "system:masters を含む CSR が作成時に拒否される (作成できてしまった)"
else
  grep -q 'system:masters' "${WORK}/masters.out" && ok "system:masters を含む CSR が作成時に拒否される" || ng "拒否理由に system:masters が出ない"
fi

echo "--- 3.6 監査ログ ---"
kc get nodes >/dev/null 2>&1
curl -sk --cert "${WORK}/long.crt" --key "${WORK}/long.key" "https://127.0.0.1:${PORT_A}/api/v1/namespaces" >/dev/null
AUDIT_PATH="$(sed -n 's/^k3s_audit_log_path: *//p' "${ROLE}/defaults/main.yml")"
docker exec "$(cname a)" cat "${AUDIT_PATH}" >"${WORK}/audit.json"
cnt() { jq -c "select($1)" "${WORK}/audit.json" | wc -l; }
[[ "$(cnt '.user.username=="gha:infra-health-check" and .verb=="create" and .objectRef.resource=="selfsubjectreviews"')" -gt 0 ]] \
  && ok "gha:* の要求がユーザー名・verb・リソース付きで記録される" || ng "gha:* の記録"
[[ "$(cnt '.user.username=="github:verify:4242" and .verb=="list" and .objectRef.resource=="namespaces"')" -gt 0 ]] \
  && ok "github:* の要求が記録される" || ng "github:* の記録"
[[ "$(cnt '(.user.groups // [] | index("system:masters")) and .verb=="list" and .objectRef.resource=="nodes"')" -gt 0 ]] \
  && ok "ローカル admin の要求が記録される" || ng "ローカル admin の記録"
[[ "$(cnt '.user.username as $u | ["system:kube-controller-manager","system:kube-scheduler","system:apiserver","system:kube-proxy","system:k3s-controller","system:k3s-supervisor","k3s-cloud-controller-manager"] | index($u)')" -eq 0 ]] \
  && [[ "$(cnt '((.user.username // "")|startswith("system:node:")) or ((.user.groups // [])|index("system:nodes")) or ((.user.groups // [])|index("system:serviceaccounts:kube-system")) or (.objectRef.resource=="events") or (.objectRef.resource=="leases")')" -eq 0 ]] \
  && ok "システムコンポーネント・ノード・kube-system SA・events・leases は記録されない (記録行 $(wc -l <"${WORK}/audit.json"))" \
  || ng "除外対象が記録されている"
echo "  記録されたユーザー名: $(jq -r '.user.username // "(anonymous)"' "${WORK}/audit.json" | sort -u | tr '\n' ' ')"

echo "--- 3.6 ローテーション上限 ---"
dirp="$(dirname "${AUDIT_PATH}")"
size() { docker exec "$(cname a)" sh -c "cat ${dirp}/audit*.log | wc -c"; }
files() { docker exec "$(cname a)" sh -c "ls ${dirp}/audit*.log | wc -l"; }
LIMIT_BYTES=$(((AUDIT_MAX_BACKUP + 1) * AUDIT_MAX_MB * 1048576))
LIMIT_FILES=$((AUDIT_MAX_BACKUP + 1))
python3 "${PY}" flood "${JUDGE_ARGS[@]}" --issuer "${ISSUER}" --api "127.0.0.1:${PORT_A}" --count 12000
sleep 3
f1="$(files)"; s1="$(size)"
python3 "${PY}" flood "${JUDGE_ARGS[@]}" --issuer "${ISSUER}" --api "127.0.0.1:${PORT_A}" --count 8000
sleep 3
f2="$(files)"; s2="$(size)"
echo "  1回目 ${f1} ファイル ${s1} bytes / 追加負荷後 ${f2} ファイル ${s2} bytes (上限 ${LIMIT_FILES} ファイル ${LIMIT_BYTES} bytes)"
[[ "${f1}" -ge 2 ]] && ok "ローテーションが発生する" || ng "ローテーションが発生しない"
[[ "${f1}" -le "${LIMIT_FILES}" && "${f2}" -le "${LIMIT_FILES}" && "${s1}" -le "${LIMIT_BYTES}" && "${s2}" -le "${LIMIT_BYTES}" ]] \
  && ok "世代数・総量が (maxbackup+1)×maxsize で頭打ちになる" || ng "総量が上限を超えた"

docker rm -fv "$(cname a)" >/dev/null

echo "=== 発行者に到達できない状態 ==="
python3 "${PY}" patch --inp "${WORK}/base/authentication-config.yaml" --out "${WORK}/dead.yaml" \
  --issuer "https://${GW}:${DEAD_PORT}" --ca "${WORK}/ca.pem"
mkdir -p "${WORK}/conf-b"
cp "${WORK}/base/audit-policy.yaml" "${WORK}/base/config.yaml" "${WORK}/conf-b/"
cp "${WORK}/dead.yaml" "${WORK}/conf-b/authentication-config.yaml"
C=b
start b "${WORK}/conf-b" --ip "${IP_A}" || exit 1
if role_check b "${READY_TIMEOUT}"; then
  ok "発行者に到達できなくても起動し、匿名 401 とローカル admin /readyz が通る"
  check "x509 の admin が通る" kc get ns
  python3 "${PY}" probe "${JUDGE_ARGS[@]}" --issuer "https://${GW}:${DEAD_PORT}" --api "127.0.0.1:$(api_port b)" >/dev/null \
    && ok "発行者不達のとき JWT は 401" || ng "発行者不達のとき JWT が 401 にならない"
else ng "発行者不達での起動"; fi
docker rm -fv "$(cname b)" >/dev/null

echo "=== 壊した設定の起動失敗をロールの確認が検知する ==="
mkdir -p "${WORK}/conf-c"
cp "${WORK}/conf-a/"* "${WORK}/conf-c/"
sed -i '0,/orValue/s//orValueX/' "${WORK}/conf-c/authentication-config.yaml"
# systemd の Restart=always を再現する (不正設定でも k3s は終了コード 0 で終わる)
start c "${WORK}/conf-c" --ip "${IP_A}" --restart always || exit 1
sleep 20
if role_check c 40; then ng "壊した認証設定でもロールの確認が通ってしまった"; else ok "壊した認証設定ではロールの確認 (readyz 期限 + 匿名 401) が失敗する"; fi
# grep -q が先に閉じると pipefail で偽の失敗になるため、いったんファイルに落とす
docker logs "$(cname c)" >"${WORK}/c.log" 2>&1
grep -qiE 'invalid authentication configuration' "${WORK}/c.log" \
  && ok "k3s のログに認証設定のエラーが出る" || ng "認証設定のエラーがログにない"
docker rm -fv "$(cname c)" >/dev/null

echo "=== 匿名無効が外れた設定を、匿名 401 の確認が検知する ==="
mkdir -p "${WORK}/conf-d"
cp "${WORK}/conf-a/"* "${WORK}/conf-d/"
sed -i 's/enabled: false/enabled: true/' "${WORK}/conf-d/authentication-config.yaml"
start d "${WORK}/conf-d" --ip "${IP_A}" || exit 1
if role_check d "${READY_TIMEOUT}"; then ng "匿名が許可された設定をロールの確認が見逃した"; else
  [[ "$(curl -sk -o /dev/null -w '%{http_code}' "https://127.0.0.1:$(api_port d)/version")" == 200 ]] \
    && ok "匿名 /version が 200 になる設定をロールの確認が失敗として検知する" || ng "匿名 200 を再現できない"
fi

echo ""
echo "PASS=${PASS} FAIL=${FAIL}"
[[ "${FAIL}" -eq 0 ]]
