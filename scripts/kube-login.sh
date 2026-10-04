#!/usr/bin/env bash
# 手元の kubeconfig にコンテキスト aramakisai-prod を作る: 鍵と CSR を手元で作り、発行ワークフロー
# (.github/workflows/kube-cert-issue.yml) で署名済みの短命クライアント証明書を受け取る。
#
#   scripts/kube-login.sh [--accept-new-ca]
#
# 前提: gh ログイン済み (>= 2.87.0)、tailnet 接続済み、openssl・kubectl・curl・jq。
# 環境変数: KUBE_API_HOST (既定 prod-node-1)、KUBE_API_PORT (既定 6443)、
#           KUBE_LOGIN_DIR (鍵・証明書の置き場。既定 ~/.kube/aramakisai)、
#           KUBE_LOGIN_TIMEOUT (発行ワークフローの完了待ち秒数。既定 600)、
#           KUBECONFIG (書き込み先。複数指定時は先頭)

set -euo pipefail

NAME="aramakisai-prod"
WORKFLOW="kube-cert-issue.yml"
ARTIFACT="kube-client-cert"
HOST="${KUBE_API_HOST:-prod-node-1}"
PORT="${KUBE_API_PORT:-6443}"
DIR="${KUBE_LOGIN_DIR:-${HOME}/.kube/aramakisai}"
TIMEOUT="${KUBE_LOGIN_TIMEOUT:-600}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

die() { echo "kube-login: $*" >&2; exit 1; }

accept_new_ca=0
case "${1:-}" in
  "") ;;
  --accept-new-ca) accept_new_ca=1 ;;
  *) die "usage: kube-login.sh [--accept-new-ca]" ;;
esac

for t in gh openssl kubectl curl jq; do
  command -v "${t}" >/dev/null || die "${t} が見つかりません"
done

target="${KUBECONFIG:-${HOME}/.kube/config}"
target="$(realpath -m "${target%%:*}")"

# 途中で失敗しても既存の鍵・kubeconfig には触れないよう、作業はすべて一時領域で行う
umask 077
mkdir -p "${DIR}" "$(dirname "${target}")"
tmp="$(mktemp -d "${DIR}/.work.XXXXXX")"
trap 'rm -rf "${tmp}"' EXIT

fingerprint() { openssl x509 -in "$1" -noout -fingerprint -sha256 | cut -d= -f2; }

# --- 利用者の特定 ---
login="$(gh api user --jq .login)" || die "gh api user に失敗しました (gh auth login 済みか確認してください)"
id="$(gh api user --jq .id)" || die "gh api user に失敗しました"
[[ "${login}" =~ ^[A-Za-z0-9-]+$ && "${id}" =~ ^[0-9]+$ ]] || die "gh が返したユーザー情報が不正です"
user="github:${login}:${id}"
echo "ユーザー名: ${user}"

# --- server CA (tailnet 経路が信頼の根拠。取得時点では検証できない) ---
curl -sk --fail --max-time 15 "https://${HOST}:${PORT}/cacerts" -o "${tmp}/ca.pem" \
  || die "${HOST}:${PORT} から server CA を取得できません (tailnet に接続していますか)"
openssl x509 -in "${tmp}/ca.pem" -noout 2>/dev/null || die "取得した server CA が証明書として不正です"
curl -s --max-time 15 --cacert "${tmp}/ca.pem" -o /dev/null "https://${HOST}:${PORT}/version" \
  || die "取得した CA で ${HOST}:${PORT} の TLS 検証が通りません"

old_ca=""
if [[ -f "${target}" ]]; then
  old_ca="$(kubectl config view --raw --kubeconfig "${target}" \
    -o jsonpath="{.clusters[?(@.name==\"${NAME}\")].cluster.certificate-authority-data}")"
fi
if [[ -n "${old_ca}" ]]; then
  base64 -d <<<"${old_ca}" >"${tmp}/old-ca.pem" 2>/dev/null || die "既存エントリの CA を読めません"
  if ! cmp -s <(openssl x509 -in "${tmp}/old-ca.pem" -outform DER) <(openssl x509 -in "${tmp}/ca.pem" -outform DER); then
    echo "server CA が既存のコンテキストと異なります (クラスタの再作成か、経路上の中間者の可能性があります)" >&2
    echo "  既存: $(fingerprint "${tmp}/old-ca.pem")" >&2
    echo "  取得: $(fingerprint "${tmp}/ca.pem")" >&2
    ((accept_new_ca)) || die "指紋が正当な値と確認できた場合だけ --accept-new-ca を付けて再実行してください。既存の kubeconfig は変更していません"
  fi
fi

# --- 鍵と CSR (利用者の openssl.cnf の req_extensions 等に依存しないよう最小の設定を明示) ---
cat >"${tmp}/csr.cnf" <<CNF
[req]
distinguished_name = dn
prompt = no
string_mask = utf8only
default_md = sha256
[dn]
CN = ${user}
CNF
openssl ecparam -name prime256v1 -genkey -noout -out "${tmp}/client.key" 2>/dev/null || die "鍵の生成に失敗しました"
openssl req -new -config "${tmp}/csr.cnf" -key "${tmp}/client.key" -out "${tmp}/client.csr" 2>/dev/null || die "CSR の生成に失敗しました"
bash "${ROOT}/.github/scripts/kube-cert-validate.sh" "${user}" <"${tmp}/client.csr" >/dev/null \
  || die "生成した CSR が検証を通りません"

# --- 発行ワークフローの起動・完了待ち・証明書の取得 (秘密鍵は送らない。送るのは CSR だけ) ---
repo="$(gh repo view --json nameWithOwner --jq .nameWithOwner)" || die "リポジトリを特定できません"
run_id="$(gh api -X POST "repos/${repo}/actions/workflows/${WORKFLOW}/dispatches" \
  -f ref=main -F return_run_details=true -f "inputs[csr]=$(base64 -w0 "${tmp}/client.csr")" \
  --jq .workflow_run_id)" || die "発行ワークフローを起動できません"
[[ "${run_id}" =~ ^[0-9]+$ ]] || die "dispatch の応答から run ID を取得できません (gh を更新してください)"
echo "発行ワークフローを起動しました: run ${run_id}"

timeout "${TIMEOUT}" gh run watch "${run_id}" --repo "${repo}" --exit-status --interval 5 >/dev/null \
  || die "発行ワークフローが失敗またはタイムアウトしました。理由は run ${run_id} の job summary を確認してください"
gh run download "${run_id}" --repo "${repo}" --name "${ARTIFACT}" --dir "${tmp}/artifact" >/dev/null \
  || die "証明書の artifact を取得できません"
crt="${tmp}/artifact/kube-client.crt"
[[ -f "${crt}" ]] || die "artifact に証明書がありません"

# --- 受け取った証明書の確認 ---
cmp -s <(openssl x509 -in "${crt}" -noout -pubkey) <(openssl pkey -in "${tmp}/client.key" -pubout) \
  || die "証明書の公開鍵が手元の鍵と一致しません"
[[ "$(openssl x509 -in "${crt}" -noout -subject -nameopt RFC2253)" == "subject=CN=${user}" ]] \
  || die "証明書の CN が期待するユーザー名と一致しません"
openssl x509 -in "${crt}" -noout -checkend 0 >/dev/null || die "証明書が期限切れです"

# --- すべて成功した後でだけ反映する。kubeconfig はコピー上で編集して置き換える ---
stamp="$(date +%s)"
newkey="${DIR}/client-${stamp}.key"
newcrt="${DIR}/client-${stamp}.crt"
cp "${tmp}/client.key" "${newkey}"
cp "${crt}" "${newcrt}"
work_cfg="$(mktemp "$(dirname "${target}")/.kubeconfig.XXXXXX")"
trap 'rm -rf "${tmp}" "${work_cfg}"' EXIT
if [[ -f "${target}" ]]; then cp "${target}" "${work_cfg}"; fi
{
  kubectl config set-cluster "${NAME}" --kubeconfig "${work_cfg}" --server "https://${HOST}:${PORT}" \
    --certificate-authority "${tmp}/ca.pem" --embed-certs=true
  kubectl config set-credentials "${NAME}" --kubeconfig "${work_cfg}" --client-certificate "${newcrt}" --client-key "${newkey}"
  kubectl config set-context "${NAME}" --kubeconfig "${work_cfg}" --cluster "${NAME}" --user "${NAME}"
} >/dev/null || { rm -f "${newkey}" "${newcrt}"; die "kubeconfig の更新に失敗しました。既存の kubeconfig は変更していません"; }
chmod 600 "${work_cfg}"
mv "${work_cfg}" "${target}" || { rm -f "${newkey}" "${newcrt}"; die "kubeconfig を置き換えられません"; }

# 旧世代の鍵・証明書は新しい kubeconfig が参照しなくなってから消す
find "${DIR}" -maxdepth 1 -name 'client-*' ! -name "client-${stamp}.*" -delete

echo "コンテキスト ${NAME} を ${target} に作成・更新しました (有効期限: $(openssl x509 -in "${crt}" -noout -enddate | cut -d= -f2))"
echo "使い方: make kubectl ARGS=\"get pods -A\""
