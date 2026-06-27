#!/usr/bin/env bash
# DR k3d セットアップスクリプト
#
# recovery.sh の DR_LOCAL_TEST=1 実行前に必要な前提条件を k3d クラスターに整える。
# Ansible (k3s-bootstrap.yml) が本来行う以下の作業を代替する:
#   - k3d ノードラベル付与
#   - ArgoCD インストール + configmap ラベル
#   - infisical-auth Secret 作成
#   - GitHub Deploy Key Secret 作成
#   - prod namespace 作成
#
# 使い方:
#   infisical run --env=prod -- bash .github/scripts/dr-k3d-setup.sh
#
# 実行後に recovery.sh を実行:
#   DR_LOCAL_TEST=1 KUBECONFIG_FILE=/tmp/kubeconfig-dr-test \
#     infisical run --env=prod -- bash .github/scripts/recovery.sh

set -euo pipefail

export KUBECONFIG=/tmp/kubeconfig-dr-test

log() { echo "$(date -u '+%H:%M:%S') [k3d-setup] $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

kubectl_r() { kubectl --kubeconfig="${KUBECONFIG}" "$@"; }

[[ -n "${INFISICAL_CLIENT_ID:-}" ]]     || die "INFISICAL_CLIENT_ID が未設定"
[[ -n "${INFISICAL_CLIENT_SECRET:-}" ]] || die "INFISICAL_CLIENT_SECRET が未設定"
[[ -n "${ARGOCD_GITHUB_DEPLOY_KEY:-}" ]] || die "ARGOCD_GITHUB_DEPLOY_KEY が未設定"

# ノードが Ready か確認
kubectl_r get nodes || die "k3d クラスター dr-test に接続できません。先に dr-local-test.sh を実行してください"

# ============================================================
# 1. k3d ノードに prod-node-1 ラベル付与
# ============================================================

log "k3d ノードに prod-node-1 ラベルを付与します"
kubectl_r label node k3d-dr-test-server-0 \
  kubernetes.io/hostname=prod-node-1 --overwrite

# ============================================================
# 2. namespace 作成
# ============================================================

kubectl_r create namespace argocd --dry-run=client -o yaml | kubectl_r apply -f -
kubectl_r create namespace prod   --dry-run=client -o yaml | kubectl_r apply -f -

# ============================================================
# 3. ArgoCD v3.4.4 インストール
# ============================================================

log "ArgoCD v3.4.4 をインストールします"
kubectl_r apply -n argocd \
  --server-side --force-conflicts \
  -f https://raw.githubusercontent.com/argoproj/argo-cd/v3.4.4/manifests/install.yaml

log "ArgoCD rollout を待機します (最大 5 分)"
kubectl_r rollout status deployment/argocd-server -n argocd --timeout=300s

# v3.4.4 configmap informer ラベル要件
kubectl_r label configmap argocd-cm -n argocd \
  app.kubernetes.io/name=argocd-cm \
  app.kubernetes.io/part-of=argocd --overwrite
kubectl_r label configmap argocd-rbac-cm -n argocd \
  app.kubernetes.io/name=argocd-rbac-cm \
  app.kubernetes.io/part-of=argocd --overwrite
log "ArgoCD インストール完了"

# ============================================================
# 4. infisical-auth Secret (Ansible Play と同一形式)
# ============================================================

log "infisical-auth Secret を作成します"
kubectl_r create secret generic infisical-auth \
  -n argocd \
  --from-literal=clientId="${INFISICAL_CLIENT_ID}" \
  --from-literal=clientSecret="${INFISICAL_CLIENT_SECRET}" \
  --dry-run=client -o yaml | kubectl_r apply -f -

# ============================================================
# 5. GitHub Deploy Key Secret (Ansible Play と同一形式)
# ============================================================

log "GitHub Deploy Key Secret を作成します"
kubectl_r create secret generic aramakisai-infra-repo \
  -n argocd \
  --from-literal=type=git \
  --from-literal=url=git@github.com:aramakisai/aramakisai-infra.git \
  --from-literal=sshPrivateKey="${ARGOCD_GITHUB_DEPLOY_KEY}" \
  --dry-run=client -o yaml \
  | kubectl_r label --local -f - argocd.argoproj.io/secret-type=repository -o yaml \
  | kubectl_r apply -f -

# ============================================================
# 6. KUBECONFIG ファイルの内容を KUBECONFIG 環境変数に変換
#    recovery.sh は KUBECONFIG env var (内容) を KUBECONFIG_FILE に書き出す。
#    DR_LOCAL_TEST=1 ではファイルが既存なら env var は不要だが念のため設定する。
# ============================================================

log ""
log "セットアップ完了。recovery.sh を実行してください:"
log ""
log "  DR_LOCAL_TEST=1 KUBECONFIG_FILE=/tmp/kubeconfig-dr-test \\"
log "    infisical run --env=prod -- bash .github/scripts/recovery.sh"
