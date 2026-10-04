#!/usr/bin/env bash
# CI・DR 共通: GitHub Actions OIDC で kube-apiserver に認証する kubeconfig の生成と、
# kubectl の exec プラグインとしてのトークン供給。
#
#   kube-oidc.sh kubeconfig <path>   server CA を取得・検証し、<path> に 0600 の kubeconfig を書く
#   kube-oidc.sh token               ExecCredential を stdout に返す (kubectl が呼ぶ)
#
# 環境変数: KUBE_API_HOST (既定 prod-node-1)、KUBE_API_PORT (既定 6443)、
#           ACTIONS_ID_TOKEN_REQUEST_URL / ACTIONS_ID_TOKEN_REQUEST_TOKEN (job に id-token: write が必要)

set -euo pipefail

# ansible/roles/k3s-server/defaults/main.yml の k3s_github_oidc_audience と同じ値にする
AUDIENCE="aramakisai-kube-prod"

die() { echo "kube-oidc: $*" >&2; exit 1; }

require_request_env() {
  [[ -n "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" && -n "${ACTIONS_ID_TOKEN_REQUEST_TOKEN:-}" ]] \
    || die "ACTIONS_ID_TOKEN_REQUEST_URL / ACTIONS_ID_TOKEN_REQUEST_TOKEN が未設定です (permissions: id-token: write が必要)"
}

cmd_kubeconfig() {
  local out="${1:-}" host port
  [[ -n "${out}" ]] || die "usage: kube-oidc.sh kubeconfig <path>"
  require_request_env
  host="${KUBE_API_HOST:-prod-node-1}"
  port="${KUBE_API_PORT:-6443}"

  tmp="$(mktemp -d)"
  trap 'rm -rf "${tmp:-}"' EXIT

  # /cacerts は匿名で取れ、取得時点では検証できない。信頼の根拠は tailnet 経路
  curl -sk --fail --max-time 15 "https://${host}:${port}/cacerts" -o "${tmp}/ca.pem" \
    || die "${host}:${port} から server CA を取得できません"
  openssl x509 -in "${tmp}/ca.pem" -noout 2>/dev/null || die "取得した server CA が証明書として不正です"

  # 匿名の /version は 401 だが、TLS 検証に失敗すると curl が非 0 で終わる。HTTP ステータスは問わない
  curl -s --max-time 15 --cacert "${tmp}/ca.pem" -o /dev/null "https://${host}:${port}/version" \
    || die "取得した CA で ${host}:${port} の TLS 検証が通りません"

  (
    umask 077
    cat >"${out}" <<YAML
apiVersion: v1
kind: Config
clusters:
  - name: aramakisai-prod
    cluster:
      server: https://${host}:${port}
      certificate-authority-data: $(base64 -w0 "${tmp}/ca.pem")
users:
  - name: aramakisai-prod
    user:
      exec:
        apiVersion: client.authentication.k8s.io/v1
        command: $(realpath "${BASH_SOURCE[0]}")
        args: [token]
        interactiveMode: Never
contexts:
  - name: aramakisai-prod
    context: {cluster: aramakisai-prod, user: aramakisai-prod}
current-context: aramakisai-prod
YAML
  )
}

cmd_token() {
  local resp tok payload exp
  require_request_env
  resp="$(curl -s --fail --max-time 15 -H "Authorization: bearer ${ACTIONS_ID_TOKEN_REQUEST_TOKEN}" \
    --get --data-urlencode "audience=${AUDIENCE}" "${ACTIONS_ID_TOKEN_REQUEST_URL}")" \
    || die "ID トークンの取得に失敗しました"
  tok="$(jq -r '.value // empty' <<<"${resp}")"
  [[ -n "${tok}" ]] || die "ID トークンの応答に value がありません"
  # 公式ランナーは stderr のワークフローコマンドも解釈する
  [[ -z "${GITHUB_ACTIONS:-}" ]] || echo "::add-mask::${tok}" >&2

  payload="$(cut -d. -f2 <<<"${tok}" | tr '_-' '/+')"
  payload="${payload}$(printf '=%.0s' $(seq 1 $(((4 - ${#payload} % 4) % 4))))"
  exp="$(base64 -d <<<"${payload}" 2>/dev/null | jq -r '.exp // empty')" || true
  [[ "${exp}" =~ ^[0-9]+$ ]] || die "ID トークンから exp を読めません"

  jq -n --arg t "${tok}" --arg e "$(date -u -d "@${exp}" '+%Y-%m-%dT%H:%M:%SZ')" \
    '{apiVersion: "client.authentication.k8s.io/v1", kind: "ExecCredential", status: {token: $t, expirationTimestamp: $e}}'
}

case "${1:-}" in
  kubeconfig) shift; cmd_kubeconfig "$@" ;;
  token) cmd_token ;;
  *) die "usage: kube-oidc.sh kubeconfig <path> | token" ;;
esac
