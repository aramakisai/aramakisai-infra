#!/usr/bin/env bash
# scripts/kube-login.sh を、gh のモックと server CA を返すローカル HTTPS モックで検証する。
# 成功時にコンテキストが作られること、失敗時 (拒否・タイムアウト・不正な証明書・CA 相違・CA 取得不可) に
# 既存の kubeconfig と鍵・証明書が 1 バイトも変わらないことを、一時 KUBECONFIG で確かめる。
# 本番・~/.kube/config・実際の gh には触れない。
#
# 使い方:
#   ./scripts/test-kube-login.sh
# 前提: python3 (PyYAML・PyJWT・cryptography。verify-k3s-authn-disposable.py のモックを流用)、openssl、kubectl、jq、curl

# ok/ng は常に成功するため A && ok || ng は if-else として安全
# shellcheck disable=SC2015
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOGIN="${ROOT}/scripts/kube-login.sh"
PY="${ROOT}/scripts/verify-k3s-authn-disposable.py"
WORK="$(mktemp -d)"
PIDS=()
cleanup() {
  for p in "${PIDS[@]}"; do kill "${p}" 2>/dev/null; done
  rm -rf "${WORK}"
}
trap cleanup EXIT

PASS=0
FAIL=0
ok() { echo "  ✅ $1"; PASS=$((PASS + 1)); }
ng() { echo "  ❌ $1"; FAIL=$((FAIL + 1)); }
check() { local d="$1"; shift; if "$@" >/dev/null 2>&1; then ok "${d}"; else ng "${d}"; fi; }

port() { python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])'; }

# --- 認証局とモック API サーバー (server CA を /cacerts で返す) ---
mkca() { openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 1 -keyout "${WORK}/$1.key" -out "${WORK}/$1.pem" -subj "/CN=$1" 2>/dev/null; }
mkca server-ca-1
mkca server-ca-2
mkca client-ca
openssl genrsa -out "${WORK}/sign.key" 2048 2>/dev/null
mkserver() { # mkserver <name> <ca>  -> 127.0.0.1 の SAN 付きサーバー証明書
  openssl req -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -keyout "${WORK}/$1.key" -out "${WORK}/$1.csr" -subj "/CN=mock-api" 2>/dev/null
  openssl x509 -req -in "${WORK}/$1.csr" -CA "${WORK}/$2.pem" -CAkey "${WORK}/$2.key" -CAcreateserial -days 1 \
    -extfile <(echo "subjectAltName=IP:127.0.0.1") -out "${WORK}/$1.pem" 2>/dev/null
}
startserver() { # startserver <name> <ca> -> ポート番号を ${WORK}/<name>.port へ
  mkserver "$1" "$2"
  local p; p="$(port)"
  python3 "${PY}" serve --issuer https://127.0.0.1 --bind 127.0.0.1 --port "${p}" --cert "${WORK}/$1.pem" \
    --tls-key "${WORK}/$1.key" --key "${WORK}/sign.key" --cacerts "${WORK}/$2.pem" &
  PIDS+=($!)
  echo "${p}" >"${WORK}/$1.port"
  for _ in $(seq 1 20); do curl -sk -o /dev/null "https://127.0.0.1:${p}/cacerts" && return 0; sleep 0.5; done
  return 1
}
startserver api1 server-ca-1 || { echo "モック API の起動に失敗"; exit 1; }
startserver api2 server-ca-2 || { echo "モック API の起動に失敗"; exit 1; }
P1="$(cat "${WORK}/api1.port")"
P2="$(cat "${WORK}/api2.port")"
DEAD="$(port)"

# --- gh のモック (MOCK_MODE: ok | reject | timeout | wrongkey | wrongcn | nocert) ---
mkdir -p "${WORK}/bin"
cat >"${WORK}/bin/gh" <<'GH'
#!/usr/bin/env bash
echo "$*" >>"${MOCK_DIR}/gh-args.log"
case "$1 $2" in
  "api user")
    [[ "$*" == *".login"* ]] && echo "alice" || echo "1001" ;;
  "repo view") echo "example/repo" ;;
  "api -X")
    for a in "$@"; do [[ "$a" == inputs\[csr\]=* ]] && base64 -d <<<"${a#inputs\[csr\]=}" >"${MOCK_DIR}/csr.pem"; done
    echo 4242 ;;
  "run watch")
    case "${MOCK_MODE}" in
      reject) echo "run failed" >&2; exit 1 ;;
      timeout) sleep 30 ;;
    esac ;;
  "run download")
    while [[ $# -gt 0 ]]; do [[ "$1" == --dir ]] && dir="$2"; shift; done
    mkdir -p "${dir}"
    csr="${MOCK_DIR}/csr.pem"
    case "${MOCK_MODE}" in
      nocert) exit 0 ;;
      wrongkey)
        openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -keyout /dev/null -subj "/CN=github:alice:1001" -out "${MOCK_DIR}/other.csr" 2>/dev/null
        csr="${MOCK_DIR}/other.csr" ;;
      wrongcn)
        openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -keyout /dev/null -subj "/CN=github:bob:1002" -out "${MOCK_DIR}/other.csr" 2>/dev/null
        openssl req -in "${csr}" -noout -pubkey >/dev/null
        csr="${MOCK_DIR}/other.csr" ;;
    esac
    # wrongcn は鍵も別になるが、CN の不一致だけを確かめるため公開鍵の検査を通すよう元の鍵で署名し直す
    if [[ "${MOCK_MODE}" == wrongcn ]]; then
      openssl x509 -req -in "${MOCK_DIR}/csr.pem" -CA "${CLIENT_CA}.pem" -CAkey "${CLIENT_CA}.key" -CAcreateserial -days 7 \
        -subj "/CN=github:bob:1002" -out "${dir}/kube-client.crt" 2>/dev/null
    else
      openssl x509 -req -in "${csr}" -CA "${CLIENT_CA}.pem" -CAkey "${CLIENT_CA}.key" -CAcreateserial -days 7 -out "${dir}/kube-client.crt" 2>/dev/null
    fi ;;
esac
GH
chmod +x "${WORK}/bin/gh"

export MOCK_DIR="${WORK}/mock" CLIENT_CA="${WORK}/client-ca"
mkdir -p "${MOCK_DIR}"

login() { # login <mode> <port> [args...]  KUBECONFIG=${CFG} KUBE_LOGIN_DIR=${KDIR}
  local mode="$1" p="$2"; shift 2
  PATH="${WORK}/bin:${PATH}" MOCK_MODE="${mode}" KUBE_API_HOST=127.0.0.1 KUBE_API_PORT="${p}" \
    KUBECONFIG="${CFG}" KUBE_LOGIN_DIR="${KDIR}" KUBE_LOGIN_TIMEOUT=3 "${LOGIN}" "$@" >"${WORK}/out.log" 2>&1
}
snap() { { [[ -f "${CFG}" ]] && sha256sum "${CFG}"; find "${KDIR}" -type f -exec sha256sum {} + 2>/dev/null | sort; } 2>/dev/null; }
untouched() { # untouched <desc> <before>
  [[ "$(snap)" == "$2" ]] && ok "$1" || ng "$1"
}
# kubectl は kubeconfig からの相対パスで保存する
userfile() { # userfile <client-key|client-certificate>
  local f; f="$(kubectl config view --raw --kubeconfig "${CFG}" -o jsonpath="{.users[?(@.name==\"aramakisai-prod\")].user.$1}")"
  (cd "$(dirname "${CFG}")" && realpath -m "${f}")
}
ctx_names() { kubectl config get-contexts -o name --kubeconfig "${CFG}" 2>/dev/null | sort | tr '\n' ' '; }

CFG="${WORK}/kubeconfig"
KDIR="${WORK}/kube-dir"

echo "=== 成功: 新規作成 (既存の他コンテキストは保持) ==="
kubectl config set-cluster other --server https://other.example --kubeconfig "${CFG}" >/dev/null
kubectl config set-credentials other --token x --kubeconfig "${CFG}" >/dev/null
kubectl config set-context other --cluster other --user other --kubeconfig "${CFG}" >/dev/null
kubectl config use-context other --kubeconfig "${CFG}" >/dev/null
if login ok "${P1}"; then
  ok "login が成功する"
  [[ "$(ctx_names)" == "aramakisai-prod other " ]] && ok "コンテキスト aramakisai-prod が作られ、other が残る" || ng "コンテキスト一覧 ($(ctx_names))"
  [[ "$(kubectl config current-context --kubeconfig "${CFG}")" == other ]] && ok "current-context を変更しない" || ng "current-context が変わった"
  [[ "$(kubectl config view --raw --kubeconfig "${CFG}" -o jsonpath='{.clusters[?(@.name=="aramakisai-prod")].cluster.server}')" == "https://127.0.0.1:${P1}" ]] && ok "cluster の server" || ng "cluster の server"
  cmp -s <(kubectl config view --raw --kubeconfig "${CFG}" -o jsonpath='{.clusters[?(@.name=="aramakisai-prod")].cluster.certificate-authority-data}' | base64 -d) "${WORK}/server-ca-1.pem" \
    && ok "cluster の CA が取得した server CA" || ng "cluster の CA"
  keyf="$(userfile client-key)"
  crtf="$(userfile client-certificate)"
  [[ -f "${keyf}" && -f "${crtf}" ]] && ok "user が鍵・証明書のファイルを参照する" || ng "user の参照先"
  [[ "$(stat -c %a "${keyf}")" == 600 ]] && ok "秘密鍵が 0600" || ng "秘密鍵のパーミッション"
  [[ "$(stat -c %a "${KDIR}")" == 700 ]] && ok "鍵の置き場が 0700" || ng "鍵の置き場のパーミッション"
  [[ "$(stat -c %a "${CFG}")" == 600 ]] && ok "kubeconfig が 0600" || ng "kubeconfig のパーミッション"
  [[ "$(openssl x509 -in "${crtf}" -noout -subject -nameopt RFC2253)" == "subject=CN=github:alice:1001" ]] && ok "証明書の CN が github:<login>:<id>" || ng "証明書の CN"
  cmp -s <(openssl x509 -in "${crtf}" -noout -pubkey) <(openssl pkey -in "${keyf}" -pubout) && ok "証明書と手元の鍵が対応する" || ng "鍵の対応"
  bash "${ROOT}/.github/scripts/kube-cert-validate.sh" github:alice:1001 <"${MOCK_DIR}/csr.pem" >/dev/null && ok "ワークフローに送った CSR が検証部品を通る" || ng "送った CSR の検証"
  key_b64="$(openssl pkey -in "${keyf}" -outform DER | base64 -w0)"
  grep -qF "${key_b64:0:40}" "${MOCK_DIR}/gh-args.log" && ng "秘密鍵が gh に渡されている" || ok "秘密鍵を gh に渡さない (送るのは CSR だけ)"
  grep -q 'BEGIN.*PRIVATE' "${WORK}/out.log" && ng "出力に秘密鍵が出ている" || ok "出力に秘密鍵が出ない"
  [[ -z "$(find "${KDIR}" -name '.work.*')" ]] && ok "一時領域が残らない" || ng "一時領域が残った"
else ng "login が成功しない"; cat "${WORK}/out.log"; fi

echo ""
echo "=== 成功: 再実行で新しい鍵に置き換わる ==="
old_pub="$(openssl pkey -in "${keyf:-/dev/null}" -pubout 2>/dev/null)"
sleep 1
if login ok "${P1}"; then
  newkeyf="$(userfile client-key)"
  [[ "$(openssl pkey -in "${newkeyf}" -pubout)" != "${old_pub}" ]] && ok "新しい鍵で再発行される" || ng "鍵が同じ"
  [[ "$(find "${KDIR}" -type f | wc -l)" -eq 2 ]] && ok "旧世代の鍵・証明書が消える (鍵と証明書の 2 ファイルだけ)" || ng "旧世代が残る"
  [[ "$(ctx_names)" == "aramakisai-prod other " ]] && ok "エントリが重複しない" || ng "エントリが重複した"
else ng "再実行"; cat "${WORK}/out.log"; fi

echo ""
echo "=== 失敗時は既存の kubeconfig・鍵・証明書が変わらない ==="
declare -A REASON=([reject]="失敗またはタイムアウト" [timeout]="失敗またはタイムアウト" [wrongkey]="公開鍵が手元の鍵と一致しません" [wrongcn]="CN が期待するユーザー名と一致しません" [nocert]="artifact")
for mode in reject timeout wrongkey wrongcn nocert; do
  before="$(snap)"
  if login "${mode}" "${P1}"; then ng "${mode}: 成功してしまった"; else ok "${mode}: 非 0 で終了する"; fi
  grep -q "${REASON[${mode}]}" "${WORK}/out.log" && ok "${mode}: 意図した理由で失敗する" || ng "${mode}: 失敗理由が違う ($(tail -1 "${WORK}/out.log"))"
  untouched "${mode}: 既存エントリと鍵・証明書が不変" "${before}"
  [[ -z "$(find "${KDIR}" -name '.work.*')" ]] && ok "${mode}: 一時領域が残らない" || ng "${mode}: 一時領域が残った"
done
login reject "${P1}"; grep -q 'job summary' "${WORK}/out.log" && ok "拒否時に job summary の確認を案内する" || ng "拒否時の案内"

before="$(snap)"
login ok "${DEAD}" && ng "CA 取得不可: 成功してしまった" || ok "CA 取得不可: 非 0 で終了する"
untouched "CA 取得不可: 既存エントリと鍵・証明書が不変" "${before}"
grep -q 'CA を取得できません' "${WORK}/out.log" && ok "CA 取得不可: 理由を表示する" || ng "CA 取得不可の理由"

echo ""
echo "=== server CA が既存のコンテキストと違う場合 ==="
before="$(snap)"
login ok "${P2}" && ng "CA 相違: 成功してしまった" || ok "CA 相違: 既定では停止する"
untouched "CA 相違: 既存エントリと鍵・証明書が不変" "${before}"
fp_old="$(openssl x509 -in "${WORK}/server-ca-1.pem" -noout -fingerprint -sha256 | cut -d= -f2)"
fp_new="$(openssl x509 -in "${WORK}/server-ca-2.pem" -noout -fingerprint -sha256 | cut -d= -f2)"
grep -qF "${fp_old}" "${WORK}/out.log" && grep -qF "${fp_new}" "${WORK}/out.log" && ok "CA 相違: 既存と取得の指紋を表示する" || ng "指紋の表示"
if login ok "${P2}" --accept-new-ca; then
  ok "--accept-new-ca で更新できる"
  cmp -s <(kubectl config view --raw --kubeconfig "${CFG}" -o jsonpath='{.clusters[?(@.name=="aramakisai-prod")].cluster.certificate-authority-data}' | base64 -d) "${WORK}/server-ca-2.pem" \
    && ok "cluster の CA が新しい server CA に更新される" || ng "CA の更新"
else ng "--accept-new-ca での更新"; cat "${WORK}/out.log"; fi

echo ""
echo "=== 初回 (kubeconfig なし) ==="
CFG="${WORK}/fresh/kubeconfig"
KDIR="${WORK}/fresh-dir"
if login ok "${P1}" && [[ "$(ctx_names)" == "aramakisai-prod " ]]; then ok "kubeconfig が無くても作成できる"; else ng "初回作成"; cat "${WORK}/out.log"; fi

echo ""
echo "${PASS} passed, ${FAIL} failed"
[[ "${FAIL}" -eq 0 ]]
