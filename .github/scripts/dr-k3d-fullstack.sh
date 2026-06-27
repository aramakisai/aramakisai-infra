#!/usr/bin/env bash
# DR k3d フルスタックデプロイ & データ整合性検証スクリプト
#
# 前提: k3d クラスター dr-test が起動済み (dr-local-test.sh で作成)
#       CNPG 0.21.x + cert-manager v1.16.x インストール済み
#
# 使い方:
#   infisical run --env=prod -- bash .github/scripts/dr-k3d-fullstack.sh
#
# 必要な Infisical 環境変数 (infisical run で自動注入):
#   INFISICAL_CLIENT_ID, INFISICAL_CLIENT_SECRET
#   ARGOCD_GITHUB_DEPLOY_KEY
#   HETZNER_OS_ACCESS_KEY_ID, HETZNER_OS_SECRET_ACCESS_KEY, HETZNER_OS_REGION

set -euo pipefail

export KUBECONFIG=/tmp/kubeconfig-dr-test
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

log() { echo "$(date -u '+%H:%M:%S') [fullstack] $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

kubectl_r() { kubectl --kubeconfig="${KUBECONFIG}" "$@"; }

# ============================================================
# 1. k3d ノードに prod-node-1 ラベル付与
# ============================================================

log "=== Step1: k3d ノードラベル付与 ==="
kubectl_r label node k3d-dr-test-server-0 \
  kubernetes.io/hostname=prod-node-1 --overwrite
log "ノードラベル付与完了"

# ============================================================
# 2. ArgoCD v3.4.4 インストール
# ============================================================

log "=== Step2: ArgoCD v3.4.4 インストール ==="
kubectl_r create namespace argocd --dry-run=client -o yaml | kubectl_r apply -f -

# --server-side --force-conflicts: ArgoCD CRD が 262144 バイト制限を超えるため必須
kubectl_r apply -n argocd \
  --server-side --force-conflicts \
  -f https://raw.githubusercontent.com/argoproj/argo-cd/v3.4.4/manifests/install.yaml

log "ArgoCD rollout 完了を待機します (最大 5 分)"
kubectl_r rollout status deployment/argocd-server -n argocd --timeout=300s

# v3.4.4 以降の configmap informer ラベル要件 (未付与だと全コンポーネント crash)
kubectl_r label configmap argocd-cm -n argocd \
  app.kubernetes.io/name=argocd-cm \
  app.kubernetes.io/part-of=argocd --overwrite
kubectl_r label configmap argocd-rbac-cm -n argocd \
  app.kubernetes.io/name=argocd-rbac-cm \
  app.kubernetes.io/part-of=argocd --overwrite
log "ArgoCD インストール完了"

# ============================================================
# 3. GitHub Deploy Key Secret (ArgoCD リポジトリ認証)
# ============================================================

log "=== Step3: GitHub Deploy Key Secret 作成 ==="
[[ -n "${ARGOCD_GITHUB_DEPLOY_KEY:-}" ]] || die "ARGOCD_GITHUB_DEPLOY_KEY が未設定"

# Ansible と同一の形式: type/url/sshPrivateKey を from-literal で指定し
# argocd.argoproj.io/secret-type=repository ラベルを付与する
kubectl_r create secret generic aramakisai-infra-repo \
  -n argocd \
  --from-literal=type=git \
  --from-literal=url=git@github.com:aramakisai/aramakisai-infra.git \
  --from-literal=sshPrivateKey="${ARGOCD_GITHUB_DEPLOY_KEY}" \
  --dry-run=client -o yaml \
  | kubectl_r label --local -f - argocd.argoproj.io/secret-type=repository -o yaml \
  | kubectl_r apply -f -
log "Deploy Key Secret 作成完了"

# ============================================================
# 4. infisical-auth Secret (ESO ClusterSecretStore 用)
# ============================================================

log "=== Step4: infisical-auth Secret 作成 ==="
[[ -n "${INFISICAL_CLIENT_ID:-}" ]]     || die "INFISICAL_CLIENT_ID が未設定"
[[ -n "${INFISICAL_CLIENT_SECRET:-}" ]] || die "INFISICAL_CLIENT_SECRET が未設定"

kubectl_r create secret generic infisical-auth \
  -n argocd \
  --from-literal=clientId="${INFISICAL_CLIENT_ID}" \
  --from-literal=clientSecret="${INFISICAL_CLIENT_SECRET}" \
  --dry-run=client -o yaml | kubectl_r apply -f -
log "infisical-auth Secret 作成完了"

# ============================================================
# 5. hetzner-os-credentials Secret (CNPG barman / VolSync 用)
#    ESO が ClusterSecretStore 経由で作成するが、CNPG Cluster apply 前に
#    必要なため先行して手動作成する
# ============================================================

log "=== Step5: hetzner-os-credentials Secret 作成 ==="
[[ -n "${HETZNER_OS_ACCESS_KEY_ID:-}" ]]     || die "HETZNER_OS_ACCESS_KEY_ID が未設定"
[[ -n "${HETZNER_OS_SECRET_ACCESS_KEY:-}" ]] || die "HETZNER_OS_SECRET_ACCESS_KEY が未設定"
[[ -n "${HETZNER_OS_REGION:-}" ]]            || die "HETZNER_OS_REGION が未設定"

kubectl_r create namespace prod --dry-run=client -o yaml | kubectl_r apply -f -
kubectl_r create secret generic hetzner-os-credentials \
  -n prod \
  --from-literal=ACCESS_KEY_ID="${HETZNER_OS_ACCESS_KEY_ID}" \
  --from-literal=SECRET_ACCESS_KEY="${HETZNER_OS_SECRET_ACCESS_KEY}" \
  --from-literal=REGION="${HETZNER_OS_REGION}" \
  --dry-run=client -o yaml | kubectl_r apply -f -
log "hetzner-os-credentials Secret 作成完了"

# ============================================================
# 6. 既存 CNPG クラスターをクリーンアップ (再実行時の冪等性)
#    gitops/manifests の db-cluster.yaml が bootstrap.recovery になっているため
#    ArgoCD sync で正しく WAL リストアが行われる。
#    再実行時に古いクラスターが残っていると bootstrap 競合が起きるため先に削除する。
# ============================================================

log "=== Step6: 既存 CNPG クラスターをクリーンアップ (冪等性) ==="

# CNPG CRD が確立しているか確認
kubectl_r wait --for=condition=established \
  crd/clusters.postgresql.cnpg.io --timeout=60s

for _C in authentik-db directus-db vaultwarden-db presence-db; do
  if kubectl_r get cluster "${_C}" -n prod &>/dev/null; then
    log "  既存クラスター ${_C} を削除します"
    kubectl_r delete jobs -n prod -l "cnpg.io/cluster=${_C}" --ignore-not-found 2>/dev/null || true
    kubectl_r delete cluster "${_C}" -n prod --wait=false --ignore-not-found 2>/dev/null || true
  fi
done
# ArgoCD が bootstrap.recovery で即時再作成するため、削除完了を待たずに進む
# (Step9 で Cluster が healthy になるまで待機する)

log "CNPG クリーンアップ完了 — ArgoCD sync で bootstrap.recovery として再作成されます"

# ============================================================
# 7. App of Apps (root.yaml) apply → ArgoCD が全アプリを sync
# ============================================================

log "=== Step7: App of Apps apply ==="
kubectl_r apply -n argocd -f "${REPO_ROOT}/gitops/root.yaml"
log "root.yaml apply 完了 — ArgoCD が全アプリを sync します"

# ============================================================
# 8. ESO ClusterSecretStore が Ready になるまで待機
# ============================================================

log "=== Step7.5: CNPG バックアップを無効化 (本番 HOS を汚染しないため) ==="
# k3d テストクラスターが本番と同じ HOS パスに書き込まないよう
# ArgoCD が CNPG クラスターを作成した後、backup セクションを削除する
# externalClusters (リストア用) は残す
_CNPG_ELAPSED=0
_CLUSTERS_PATCHED=0
for _C in authentik-db directus-db vaultwarden-db presence-db; do
  until kubectl_r get cluster "${_C}" -n prod &>/dev/null; do
    (( _CNPG_ELAPSED >= 120 )) && break
    sleep 5; _CNPG_ELAPSED=$(( _CNPG_ELAPSED + 5 ))
  done
  if kubectl_r get cluster "${_C}" -n prod &>/dev/null; then
    if kubectl_r patch cluster "${_C}" -n prod --type=json \
      -p '[{"op":"remove","path":"/spec/backup"}]' 2>/dev/null; then
      log "  ${_C} backup section 削除完了"
      (( _CLUSTERS_PATCHED++ )) || true
    fi
  fi
done
log "CNPG バックアップ停止完了 (${_CLUSTERS_PATCHED}/4 クラスター)"

log "=== Step8: ESO ClusterSecretStore 待機 ==="
log "ClusterSecretStore CRD が作成されるまでポーリングします (最大 10 分)"
ELAPSED=0
until kubectl_r get crd clustersecretstores.external-secrets.io &>/dev/null; do
  (( ELAPSED >= 600 )) && die "ESO CRD が 10 分以内に作成されませんでした"
  log "  ESO CRD 待機中... (${ELAPSED}s)"
  sleep 15
  ELAPSED=$(( ELAPSED + 15 ))
done
kubectl_r wait --for=condition=established \
  crd/clustersecretstores.external-secrets.io --timeout=60s

log "ClusterSecretStore infisical が Ready になるまで待機します"
ELAPSED=0
while true; do
  READY=$(kubectl_r get clustersecretstore infisical \
    -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)
  [[ "$READY" == "True" ]] && break
  (( ELAPSED >= 600 )) && { log "警告: ClusterSecretStore Ready 待機タイムアウト"; break; }
  log "  ClusterSecretStore 待機中... (${ELAPSED}s)"
  sleep 15
  ELAPSED=$(( ELAPSED + 15 ))
done
log "ESO ClusterSecretStore 準備完了"

# ============================================================
# 9. CNPG Cluster が healthy になるまで待機
# ============================================================

log "=== Step9: CNPG Cluster healthy 待機 ==="
# ArgoCD が db-cluster.yaml を sync して Cluster CR を作成するまで待つ
for CLUSTER in authentik-db directus-db vaultwarden-db presence-db; do
  log "  ${CLUSTER} の Cluster CR 作成を待機します (最大 5 分)"
  _ELAPSED=0
  until kubectl_r get cluster "${CLUSTER}" -n prod &>/dev/null; do
    (( _ELAPSED >= 300 )) && { log "  警告: ${CLUSTER} の Cluster CR が作成されませんでした"; break; }
    sleep 10
    _ELAPSED=$(( _ELAPSED + 10 ))
  done
done
# Cluster CR が揃ったら healthy を待つ
for CLUSTER in authentik-db directus-db vaultwarden-db presence-db; do
  log "  ${CLUSTER} の healthy を待機します (最大 20 分)"
  kubectl_r wait cluster "${CLUSTER}" -n prod \
    --for=jsonpath='{.status.phase}'='Cluster in healthy state' \
    --timeout=1200s \
    || log "  警告: ${CLUSTER} の healthy 待機タイムアウト"
done

# ============================================================
# 10. mailserver PVC が作成されるまで待機してから VolSync リストア
# ============================================================

log "=== Step10: mailserver VolSync リストア ==="

# mailserver StatefulSet が PVC を作成するまで待機
log "mailserver-data PVC が作成されるまで待機します (最大 10 分)"
ELAPSED=0
while true; do
  PVC_EXISTS=$(kubectl_r get pvc mailserver-data -n prod \
    --ignore-not-found 2>/dev/null | wc -l || true)
  [[ "$PVC_EXISTS" -gt 0 ]] && break
  (( ELAPSED >= 600 )) && die "mailserver-data PVC が作成されませんでした"
  log "  PVC 待機中... (${ELAPSED}s)"
  sleep 15
  ELAPSED=$(( ELAPSED + 15 ))
done

# mailserver を停止してからリストア
kubectl_r scale statefulset mailserver -n prod --replicas=0 2>/dev/null || true
kubectl_r wait pod -n prod -l app=mailserver --for=delete --timeout=60s 2>/dev/null || true

# VolSync CRD が確立するまで待機
kubectl_r wait --for=condition=established \
  crd/replicationdestinations.volsync.backube --timeout=300s

RESTORE_TRIGGER="dr-k3d-$(date +%Y%m%dT%H%M%S)"
kubectl_r apply -f - <<EOF
apiVersion: volsync.backube/v1alpha1
kind: ReplicationDestination
metadata:
  name: mailserver-restore
  namespace: prod
spec:
  trigger:
    manual: "${RESTORE_TRIGGER}"
  restic:
    repository: mailserver-restic-secret
    destinationPVC: mailserver-data
    copyMethod: Direct
    moverSecurityContext:
      runAsUser: 0
      runAsGroup: 0
      fsGroup: 0
EOF

log "VolSync リストア完了を待機します (最大 30 分)"
_VOLSYNC_TIMEOUT=1800
_VOLSYNC_ELAPSED=0
while true; do
  _RESULT=$(kubectl_r get replicationdestination/mailserver-restore -n prod \
    -o jsonpath='{.status.latestMoverStatus.result}' 2>/dev/null || true)
  if [[ "$_RESULT" == "Successful" ]]; then
    log "VolSync リストア成功"
    break
  elif [[ "$_RESULT" == "Failed" ]]; then
    die "VolSync リストアが失敗しました"
  fi
  (( _VOLSYNC_ELAPSED >= _VOLSYNC_TIMEOUT )) && die "VolSync リストアがタイムアウトしました"
  log "  VolSync 待機中... result=${_RESULT:-同期中} (${_VOLSYNC_ELAPSED}s)"
  sleep 15
  _VOLSYNC_ELAPSED=$(( _VOLSYNC_ELAPSED + 15 ))
done
log "VolSync リストア完了"

kubectl_r delete replicationdestination mailserver-restore -n prod
kubectl_r scale statefulset mailserver -n prod --replicas=1

# ============================================================
# 11. 全 ArgoCD Application Healthy 待機
# ============================================================

log "=== Step11: ArgoCD 全 Application Healthy 待機 (最大 30 分) ==="
ARGOCD_ELAPSED=0
while true; do
  NOT_HEALTHY=$(kubectl_r get applications -n argocd --no-headers 2>/dev/null \
    | awk '{print $3}' | grep -cv "Healthy" || echo "99")

  [[ "$NOT_HEALTHY" -eq 0 ]] && { log "全 ArgoCD Application が Healthy になりました"; break; }
  (( ARGOCD_ELAPSED >= 1800 )) && { log "警告: ArgoCD Healthy 待機タイムアウト"; break; }

  log "  Healthy でない Application: ${NOT_HEALTHY} 件 (${ARGOCD_ELAPSED}s)"
  sleep 30
  ARGOCD_ELAPSED=$(( ARGOCD_ELAPSED + 30 ))
done

# ============================================================
# 12. データ整合性確認
# ============================================================

log "=== Step12: データ整合性確認 ==="

_check_db() {
  local cluster="$1"
  local dbname="$2"
  local user="$3"

  local pod
  pod=$(kubectl_r get pod -n prod \
    -l "cnpg.io/cluster=${cluster},role=primary" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)

  if [[ -z "$pod" ]]; then
    log "  警告: ${cluster} の primary Pod が見つかりません"
    return
  fi

  local first_recov
  first_recov=$(kubectl_r get cluster "${cluster}" -n prod \
    -o jsonpath='{.status.firstRecoverabilityPoint}' 2>/dev/null || true)
  log "  ${cluster} firstRecoverabilityPoint: ${first_recov:-未取得}"

  local table_count
  table_count=$(kubectl_r exec -n prod "${pod}" \
    -- psql -U "${user}" -d "${dbname}" \
    -c "SELECT count(*) FROM pg_tables WHERE schemaname='public';" \
    --tuples-only --no-align 2>/dev/null | tr -d '[:space:]' || true)
  log "  ${cluster} (${dbname}) public スキーマのテーブル数: ${table_count:-取得失敗}"
}

_check_db "authentik-db"   "authentik"   "postgres"
_check_db "directus-db"    "directus"    "postgres"
_check_db "vaultwarden-db" "vaultwarden" "postgres"
_check_db "presence-db"    "presence"    "postgres"

# mailserver データ確認
log "  mailserver-data PVC の内容確認"
MAIL_POD=$(kubectl_r get pod -n prod -l app=mailserver \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
if [[ -n "$MAIL_POD" ]]; then
  MAIL_COUNT=$(kubectl_r exec -n prod "${MAIL_POD}" \
    -- find /var/mail -type f -name "*.idx" 2>/dev/null | wc -l || true)
  log "  mailserver メールボックスファイル数: ${MAIL_COUNT:-取得失敗}"
else
  log "  警告: mailserver Pod が見つかりません"
fi

# ============================================================
# 13. アクセス方法の出力
# ============================================================

log ""
log "=========================================================="
log "DR k3d フルスタック検証完了"
log "=========================================================="
log ""
log "各アプリへのアクセス (別ターミナルで port-forward を実行):"
log ""
log "# ArgoCD (ユーザー: admin / Secret: argocd-initial-admin-secret)"
log "kubectl --kubeconfig=/tmp/kubeconfig-dr-test port-forward svc/argocd-server -n argocd 8080:443"
log "URL: https://localhost:8080"
log ""
log "# Authentik"
log "kubectl --kubeconfig=/tmp/kubeconfig-dr-test port-forward svc/authentik-server -n prod 9000:80"
log "URL: http://localhost:9000"
log ""
log "# Directus"
log "kubectl --kubeconfig=/tmp/kubeconfig-dr-test port-forward svc/directus -n prod 8055:8055"
log "URL: http://localhost:8055"
log ""
log "# Vaultwarden"
log "kubectl --kubeconfig=/tmp/kubeconfig-dr-test port-forward svc/vaultwarden -n prod 8081:80"
log "URL: http://localhost:8081"
log ""
log "# Roundcube"
log "kubectl --kubeconfig=/tmp/kubeconfig-dr-test port-forward svc/roundcube -n prod 8082:80"
log "URL: http://localhost:8082"
log ""
log "# Room Presence"
log "kubectl --kubeconfig=/tmp/kubeconfig-dr-test port-forward svc/room-presence -n prod 8083:3000"
log "URL: http://localhost:8083"
log ""
log "ArgoCD 管理者パスワード確認:"
log "kubectl --kubeconfig=/tmp/kubeconfig-dr-test get secret argocd-initial-admin-secret -n argocd -o jsonpath='{.data.password}' | base64 -d && echo"
