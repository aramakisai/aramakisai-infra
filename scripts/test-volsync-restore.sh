#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# VolSync restic リストア検証スクリプト (タスク 6.2)
#
# Stalwart を一時停止し、S3 restic リポジトリから PVC にリストアを実施する。
# リストア後に Stalwart を再起動し、SMTP/IMAP 疎通を確認する。
#
# 使い方:
#   ./scripts/test-volsync-restore.sh
#
# 前提:
#   - Infisical を経由して実行すること (例: infisical run -- ./scripts/test-volsync-restore.sh)
#   - VolSync ReplicationSource が少なくとも 1 回成功していること
#     (kubectl get replicationsource stalwart-backup -n prod で lastSyncTime を確認)
#   - prod-node-1 に ssh 接続できること (SMTP/IMAP 確認用)
#   - netcat (nc) が使用可能であること
#
# ⚠️  注意: このスクリプトは Stalwart を一時停止します。
#          実行中はメールの送受信が一時的に停止します。
# ============================================================

NAMESPACE="prod"
STALWART_STS="stalwart"
PVC_NAME="stalwart-data"
RESTORE_NAME="stalwart-restore-$(date +%Y%m%d-%H%M%S)"
MAIL_HOST="mail.aramakisai.com"

# ── 前提チェック ──────────────────────────────────────────
echo "=== 前提チェック ==="

if ! command -v kubectl &>/dev/null; then
  echo "❌ kubectl がインストールされていません"
  exit 1
fi
echo "  ✅ kubectl: $(kubectl version --client --short 2>/dev/null | head -1)"

if ! kubectl get ns "${NAMESPACE}" &>/dev/null; then
  echo "❌ ${NAMESPACE} namespace が存在しません"
  exit 1
fi
echo "  ✅ クラスター接続確認"

# ReplicationSource の最終 sync 確認
LAST_SYNC=$(kubectl get replicationsource stalwart-backup -n "${NAMESPACE}" \
  -o jsonpath='{.status.lastSyncTime}' 2>/dev/null || echo "")
if [[ -z "${LAST_SYNC}" ]]; then
  echo "❌ VolSync ReplicationSource がまだ成功していません"
  echo "   kubectl get replicationsource stalwart-backup -n ${NAMESPACE} で状態を確認してください"
  exit 1
fi
echo "  ✅ 最終バックアップ時刻: ${LAST_SYNC}"

# stalwart-restic-secret の存在確認
if ! kubectl get secret stalwart-restic-secret -n "${NAMESPACE}" &>/dev/null; then
  echo "❌ stalwart-restic-secret Secret が存在しません"
  exit 1
fi
echo "  ✅ stalwart-restic-secret 確認"

echo ""
echo "  ⚠️  このスクリプトは Stalwart を一時停止してリストアを実施します"
echo "     実行中はメールの送受信が停止します"
echo ""
read -r -p "続行しますか? [y/N] " confirm
[[ "${confirm}" =~ ^[Yy]$ ]] || { echo "中止しました"; exit 0; }

# ── Stalwart を停止 ────────────────────────────────────────
echo ""
echo "=== Stalwart を停止 ==="
CURRENT_REPLICAS=$(kubectl get statefulset "${STALWART_STS}" -n "${NAMESPACE}" \
  -o jsonpath='{.spec.replicas}' 2>/dev/null || echo "1")
echo "  現在のレプリカ数: ${CURRENT_REPLICAS}"

kubectl scale statefulset "${STALWART_STS}" -n "${NAMESPACE}" --replicas=0
kubectl rollout status statefulset "${STALWART_STS}" -n "${NAMESPACE}" \
  --timeout=120s 2>/dev/null || true

# Pod が完全に停止するまで待機
echo "  Pod の停止を待機..."
until [[ $(kubectl get pods -n "${NAMESPACE}" -l "app.kubernetes.io/name=stalwart" \
  --no-headers 2>/dev/null | wc -l) -eq 0 ]]; do
  sleep 5
done
echo "  ✅ Stalwart 停止確認"

# ── ReplicationDestination を作成 ─────────────────────────
echo ""
echo "=== ReplicationDestination を作成 (restic → PVC リストア) ==="

kubectl apply -f - <<EOF
apiVersion: volsync.backube/v1alpha1
kind: ReplicationDestination
metadata:
  name: ${RESTORE_NAME}
  namespace: ${NAMESPACE}
spec:
  trigger:
    manual: restore-$(date +%Y%m%d)
  restic:
    repository: stalwart-restic-secret
    destinationPVC: ${PVC_NAME}
    copyMethod: Direct
    moverSecurityContext:
      runAsUser: 0
      runAsGroup: 0
      fsGroup: 0
EOF

echo "  ✅ ReplicationDestination 作成: ${RESTORE_NAME}"

# ── リストア完了を待機 ────────────────────────────────────
echo ""
echo "=== リストア完了を待機 (最大 30 分) ==="
echo "  (データ量によって時間が変わります)"

TIMEOUT=1800
ELAPSED=0
until kubectl get replicationdestination "${RESTORE_NAME}" -n "${NAMESPACE}" \
    -o jsonpath='{.status.lastSyncTime}' 2>/dev/null | grep -q "^[0-9]"; do
  if [[ ${ELAPSED} -ge ${TIMEOUT} ]]; then
    echo ""
    echo "❌ タイムアウト (${TIMEOUT}秒) — リストアが完了しませんでした"
    echo "   kubectl describe replicationdestination ${RESTORE_NAME} -n ${NAMESPACE}"
    echo "   Stalwart を手動で再起動してください:"
    echo "   kubectl scale statefulset ${STALWART_STS} -n ${NAMESPACE} --replicas=${CURRENT_REPLICAS}"
    exit 1
  fi
  printf "  待機中... (%ds / %ds)\r" "${ELAPSED}" "${TIMEOUT}"
  sleep 15
  ELAPSED=$((ELAPSED + 15))
done

echo ""
SYNC_TIME=$(kubectl get replicationdestination "${RESTORE_NAME}" -n "${NAMESPACE}" \
  -o jsonpath='{.status.lastSyncTime}')
SYNC_DURATION=$(kubectl get replicationdestination "${RESTORE_NAME}" -n "${NAMESPACE}" \
  -o jsonpath='{.status.lastSyncDuration}' 2>/dev/null || echo "不明")
echo "  ✅ リストア完了"
echo "     lastSyncTime    : ${SYNC_TIME}"
echo "     lastSyncDuration: ${SYNC_DURATION}"

kubectl describe replicationdestination "${RESTORE_NAME}" -n "${NAMESPACE}" \
  | grep -A 5 "Status:" || true

# ── Stalwart を再起動 ─────────────────────────────────────
echo ""
echo "=== Stalwart を再起動 ==="
kubectl scale statefulset "${STALWART_STS}" -n "${NAMESPACE}" \
  --replicas="${CURRENT_REPLICAS}"
kubectl rollout status statefulset "${STALWART_STS}" -n "${NAMESPACE}" \
  --timeout=120s
echo "  ✅ Stalwart 起動確認"

# Pod が Running になるまで少し待機
sleep 10

# ── SMTP/IMAP 疎通確認 ────────────────────────────────────
echo ""
echo "=== SMTP/IMAP 疎通確認 ==="

check_port() {
  local host="$1"
  local port="$2"
  local name="$3"
  if timeout 5 bash -c "echo >/dev/tcp/${host}/${port}" 2>/dev/null; then
    echo "  ✅ ${name} (port ${port}): 接続成功"
    return 0
  else
    echo "  ❌ ${name} (port ${port}): 接続失敗"
    return 1
  fi
}

FAILED=0
check_port "${MAIL_HOST}" 25  "SMTP"   || FAILED=1
check_port "${MAIL_HOST}" 587 "SMTP (submission)" || FAILED=1
check_port "${MAIL_HOST}" 993 "IMAP SSL" || FAILED=1

if [[ ${FAILED} -eq 0 ]]; then
  echo ""
  echo "✅ SMTP/IMAP 疎通確認完了"
else
  echo ""
  echo "⚠️  一部のポートへの接続に失敗しました"
  echo "   Stalwart のログを確認してください:"
  echo "   kubectl logs -n ${NAMESPACE} statefulset/${STALWART_STS} --tail=50"
fi

# ── クリーンアップ ────────────────────────────────────────
echo ""
echo "=== クリーンアップ ==="
read -r -p "  ReplicationDestination (${RESTORE_NAME}) を削除しますか? [Y/n] " confirm
if [[ ! "${confirm}" =~ ^[Nn]$ ]]; then
  kubectl delete replicationdestination "${RESTORE_NAME}" -n "${NAMESPACE}"
  echo "  ✅ ReplicationDestination を削除"
fi

echo ""
echo "=== VolSync リストア検証完了 ==="
echo "  リストア時刻   : ${SYNC_TIME}"
echo "  Stalwart 状態  : $(kubectl get statefulset ${STALWART_STS} -n ${NAMESPACE} \
  -o jsonpath='{.status.readyReplicas}') / ${CURRENT_REPLICAS} Pod が Ready"
