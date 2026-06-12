#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# CloudNativePG PITR リストア検証スクリプト (タスク 6.1)
#
# restore-test namespace に一時 Cluster を作成してリストアを検証する。
# 本番 namespace (prod) には一切触れない。
#
# 使い方:
#   ./scripts/test-cnpg-restore.sh [authentik-db|directus-db] [targetTime]
#
#   $1  検証対象クラスター名 (デフォルト: authentik-db)
#   $2  PITR ターゲット時刻  (デフォルト: 省略 = 最新バックアップから復旧)
#       例: "2026-06-02 10:00:00"
#
# 前提:
#   - Infisical を経由して実行すること (例: infisical run -- ./scripts/test-cnpg-restore.sh)
#   - 対象 DB の barmanObjectStore バックアップが B2 に存在すること
#   - kubectl が使用可能であること
# ============================================================

TARGET_CLUSTER="${1:-authentik-db}"
RECOVERY_TARGET_TIME="${2:-}"
RESTORE_NS="restore-test"
B2_ENDPOINT="https://s3.us-west-004.backblazeb2.com"
B2_BUCKET="aramakisai-backups"

# ── 前提チェック ──────────────────────────────────────────
echo "=== 前提チェック ==="

if ! command -v kubectl &>/dev/null; then
  echo "❌ kubectl がインストールされていません"
  exit 1
fi
echo "  ✅ kubectl: $(kubectl version --client --short 2>/dev/null | head -1)"

if ! kubectl get ns prod &>/dev/null; then
  echo "❌ prod namespace が存在しません。KUBECONFIG を確認してください"
  exit 1
fi
echo "  ✅ クラスター接続確認"

# b2-credentials が prod に存在することを確認
if ! kubectl get secret b2-credentials -n prod &>/dev/null; then
  echo "❌ b2-credentials Secret が prod namespace にありません"
  echo "   ArgoCD で b2-external-secret.yaml が sync されているか確認してください"
  exit 1
fi
echo "  ✅ b2-credentials Secret 確認"

echo ""
echo "  対象クラスター : ${TARGET_CLUSTER}"
echo "  リストア先NS   : ${RESTORE_NS}"
if [[ -n "${RECOVERY_TARGET_TIME}" ]]; then
  echo "  PITR ターゲット: ${RECOVERY_TARGET_TIME}"
else
  echo "  PITR ターゲット: 最新バックアップ (省略)"
fi
echo ""

# ── restore-test namespace 作成 ────────────────────────────
echo "=== restore-test namespace を作成 ==="
if kubectl get ns "${RESTORE_NS}" &>/dev/null; then
  echo "  ⚠️  ${RESTORE_NS} namespace が既に存在します"
  read -r -p "  削除して再作成しますか? [y/N] " confirm
  [[ "${confirm}" =~ ^[Yy]$ ]] || { echo "中止しました"; exit 1; }
  kubectl delete ns "${RESTORE_NS}" --wait=true
fi
kubectl create namespace "${RESTORE_NS}"
echo "  ✅ ${RESTORE_NS} namespace 作成"

# ── b2-credentials を restore-test にコピー ────────────────
echo ""
echo "=== b2-credentials Secret をコピー ==="
kubectl get secret b2-credentials -n prod -o json \
  | jq 'del(.metadata.resourceVersion, .metadata.uid, .metadata.creationTimestamp, .metadata.annotations, .metadata.ownerReferences)
        | .metadata.namespace = "restore-test"' \
  | kubectl apply -f -
echo "  ✅ b2-credentials を ${RESTORE_NS} にコピー"

# ── recovery Cluster マニフェストを生成して適用 ────────────
echo ""
echo "=== recovery Cluster を作成 ==="

RECOVERY_TARGET_SPEC=""
if [[ -n "${RECOVERY_TARGET_TIME}" ]]; then
  RECOVERY_TARGET_SPEC="recoveryTarget:
        targetTime: \"${RECOVERY_TARGET_TIME}\""
fi

kubectl apply -f - <<EOF
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: ${TARGET_CLUSTER}-restore
  namespace: ${RESTORE_NS}
spec:
  instances: 1
  bootstrap:
    recovery:
      source: ${TARGET_CLUSTER}-backup
      ${RECOVERY_TARGET_SPEC}
  externalClusters:
    - name: ${TARGET_CLUSTER}-backup
      barmanObjectStore:
        destinationPath: "s3://${B2_BUCKET}/cnpg/${TARGET_CLUSTER}"
        endpointURL: "${B2_ENDPOINT}"
        serverName: "${TARGET_CLUSTER}"
        s3Credentials:
          accessKeyId:
            name: b2-credentials
            key: ACCESS_KEY_ID
          secretAccessKey:
            name: b2-credentials
            key: SECRET_ACCESS_KEY
  storage:
    size: 5Gi
  resources:
    requests:
      memory: "256Mi"
      cpu: "100m"
    limits:
      memory: "512Mi"
      cpu: "500m"
EOF

echo "  ✅ Cluster リソース適用"

# ── Pod が Running になるまで待機 ─────────────────────────
echo ""
echo "=== クラスターの起動を待機 (最大 10 分) ==="
echo "  (Ctrl-C で中断可能)"

TIMEOUT=600
ELAPSED=0
until kubectl get cluster "${TARGET_CLUSTER}-restore" -n "${RESTORE_NS}" \
    -o jsonpath='{.status.phase}' 2>/dev/null | grep -q "Cluster in healthy state"; do
  if [[ ${ELAPSED} -ge ${TIMEOUT} ]]; then
    echo ""
    echo "❌ タイムアウト (${TIMEOUT}秒) — クラスターが Ready になりませんでした"
    echo "   kubectl describe cluster ${TARGET_CLUSTER}-restore -n ${RESTORE_NS}"
    echo "   kubectl get events -n ${RESTORE_NS} --sort-by='.lastTimestamp'"
    exit 1
  fi
  printf "  待機中... (%ds)\r" "${ELAPSED}"
  sleep 10
  ELAPSED=$((ELAPSED + 10))
done

echo ""
echo "  ✅ Cluster が healthy 状態になりました"
kubectl get cluster "${TARGET_CLUSTER}-restore" -n "${RESTORE_NS}"

# ── SQL 検証 ─────────────────────────────────────────────
echo ""
echo "=== SQL でデータを確認 ==="

PRIMARY_POD=$(kubectl get pods -n "${RESTORE_NS}" \
  -l "cnpg.io/cluster=${TARGET_CLUSTER}-restore,cnpg.io/instanceRole=primary" \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)

if [[ -z "${PRIMARY_POD}" ]]; then
  echo "❌ Primary Pod が見つかりません"
  kubectl get pods -n "${RESTORE_NS}"
  exit 1
fi

echo "  Primary Pod: ${PRIMARY_POD}"

# テーブル一覧
echo ""
echo "  テーブル一覧:"
kubectl exec -n "${RESTORE_NS}" "${PRIMARY_POD}" -- \
  psql -U postgres -c "\dt" 2>/dev/null || \
  kubectl exec -n "${RESTORE_NS}" "${PRIMARY_POD}" -- \
    psql -U "${TARGET_CLUSTER//-/_}" "${TARGET_CLUSTER//-/_}" -c "\dt" || true

# レコード数確認 (主要テーブル)
echo ""
echo "  主要テーブルのレコード数:"
if [[ "${TARGET_CLUSTER}" == "authentik-db" ]]; then
  kubectl exec -n "${RESTORE_NS}" "${PRIMARY_POD}" -- \
    psql -U authentik authentik -c "SELECT schemaname, tablename, n_live_tup FROM pg_stat_user_tables ORDER BY n_live_tup DESC LIMIT 10;" 2>/dev/null || true
elif [[ "${TARGET_CLUSTER}" == "directus-db" ]]; then
  kubectl exec -n "${RESTORE_NS}" "${PRIMARY_POD}" -- \
    psql -U directus directus -c "SELECT schemaname, tablename, n_live_tup FROM pg_stat_user_tables ORDER BY n_live_tup DESC LIMIT 10;" 2>/dev/null || true
fi

echo ""
echo "✅ SQL 検証完了"

# ── クリーンアップ ────────────────────────────────────────
echo ""
echo "=== クリーンアップ ==="
read -r -p "  restore-test namespace を削除しますか? [Y/n] " confirm
if [[ ! "${confirm}" =~ ^[Nn]$ ]]; then
  kubectl delete namespace "${RESTORE_NS}" --wait=true
  echo "  ✅ ${RESTORE_NS} namespace を削除しました"
else
  echo "  ⚠️  手動で削除してください: kubectl delete namespace ${RESTORE_NS}"
fi

echo ""
echo "=== PITR リストア検証完了 ==="
echo "  対象クラスター: ${TARGET_CLUSTER}"
echo "  本番 (prod namespace) への影響: なし"
