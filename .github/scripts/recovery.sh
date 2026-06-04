#!/usr/bin/env bash
# K3s コールドスタンバイ復旧スクリプト
# 実行フロー:
#   1. 必須環境変数チェック
#   2. Tailscale から prod-node-1 デバイスを削除 (残存デバイスによる重複ホスト名防止)
#   3. Terraform Cloud API でノードを再作成
#   4. Tailscale に prod-node-1 として登録されるまでポーリング (最大10分)
#   5. Ansible でシングルノード K3s をブートストラップ
#   6. ArgoCD sync 完了・CNPG healthy を待機
#   7. Stalwart メールデータを VolSync で B2 からリストア
#
# 注意: GitHub Actions の dr-recovery ワークフローから呼び出される。
#       infisical run --env=prod -- bash .github/scripts/recovery.sh で実行すること。

set -euo pipefail

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') [recovery] $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
KUBECONFIG_FILE="/tmp/kubeconfig-recovery"

kubectl_r() { kubectl --kubeconfig="${KUBECONFIG_FILE}" "$@"; }

# ============================================================
# 1. 必須環境変数チェック
# ============================================================

REQUIRED_VARS=(
  INFISICAL_CLIENT_ID
  INFISICAL_CLIENT_SECRET
  K3S_TOKEN
  ARGOCD_GITHUB_DEPLOY_KEY
  TAILSCALE_API_KEY
  TAILSCALE_TAILNET
  TFC_API_TOKEN
  TFC_WORKSPACE_ID
  CLOUDFLARE_TUNNEL_TOKEN
  CLOUDFLARE_TUNNEL_ID
  KUBECONFIG
)

for VAR in "${REQUIRED_VARS[@]}"; do
  [[ -n "${!VAR:-}" ]] || die "必須環境変数が未設定です: $VAR"
done

# KUBECONFIG 環境変数の内容をファイルに書き出す (kubectl は file path を要求するため)
echo "${KUBECONFIG}" > "${KUBECONFIG_FILE}"
chmod 600 "${KUBECONFIG_FILE}"

log "環境変数チェック完了"

# ============================================================
# 2. Tailscale から prod-node-1 デバイスを削除
#    ephemeral=false のため障害ノードが tailnet に残存し、
#    新ノードが prod-node-1-1 として登録されるのを防ぐ
# ============================================================

log "Tailscale から prod-node-1 を削除します"

DEVICE_IDS=$(curl -sf \
  -H "Authorization: Bearer ${TAILSCALE_API_KEY}" \
  "https://api.tailscale.com/api/v2/tailnet/${TAILSCALE_TAILNET}/devices" \
  | jq -r '.devices[] | select(.hostname == "prod-node-1") | .id' || true)

if [[ -z "$DEVICE_IDS" ]]; then
  log "Tailscale に prod-node-1 デバイスは見つかりませんでした (スキップ)"
else
  for ID in $DEVICE_IDS; do
    log "デバイス削除: $ID"
    curl -sf -X DELETE \
      -H "Authorization: Bearer ${TAILSCALE_API_KEY}" \
      "https://api.tailscale.com/api/v2/device/${ID}" || log "警告: デバイス削除に失敗しました ($ID)"
  done
fi

# ============================================================
# 3. Terraform Cloud API でプランを作成・適用
# ============================================================

log "Terraform Cloud でプランを作成します (Workspace: ${TFC_WORKSPACE_ID})"

RUN_PAYLOAD=$(jq -n \
  --arg ws_id "${TFC_WORKSPACE_ID}" \
  '{
    data: {
      attributes: {
        "is-destroy": false,
        "auto-apply": true,
        message: "Cold standby recovery triggered by recovery.sh"
      },
      type: "runs",
      relationships: {
        workspace: {
          data: { type: "workspaces", id: $ws_id }
        }
      }
    }
  }')

RUN_RESPONSE=$(curl -sf \
  -X POST \
  -H "Authorization: Bearer ${TFC_API_TOKEN}" \
  -H "Content-Type: application/vnd.api+json" \
  -d "$RUN_PAYLOAD" \
  "https://app.terraform.io/api/v2/runs")

RUN_ID=$(echo "$RUN_RESPONSE" | jq -r '.data.id')
[[ -n "$RUN_ID" && "$RUN_ID" != "null" ]] || die "Terraform Cloud の Run 作成に失敗しました"
log "Terraform Cloud Run 作成完了: $RUN_ID"

TIMEOUT=900
ELAPSED=0
SLEEP_INTERVAL=15

while true; do
  STATUS=$(curl -sf \
    -H "Authorization: Bearer ${TFC_API_TOKEN}" \
    "https://app.terraform.io/api/v2/runs/${RUN_ID}" \
    | jq -r '.data.attributes.status')

  log "Run ステータス: $STATUS (経過: ${ELAPSED}s)"

  case "$STATUS" in
    applied|planned_and_finished)
      log "Terraform Cloud Apply 完了"
      break
      ;;
    errored|canceled|force_canceled|discarded)
      die "Terraform Cloud Run が失敗しました (status: $STATUS)"
      ;;
  esac

  (( ELAPSED >= TIMEOUT )) && die "Terraform Cloud Apply がタイムアウトしました (${TIMEOUT}s)"

  sleep $SLEEP_INTERVAL
  ELAPSED=$(( ELAPSED + SLEEP_INTERVAL ))
done

# ============================================================
# 4. Tailscale に prod-node-1 として登録されるまでポーリング
# ============================================================

log "Tailscale への prod-node-1 登録を待機します (最大10分)"
TS_TIMEOUT=600
TS_ELAPSED=0
TS_SLEEP=15

while true; do
  REGISTERED=$(curl -sf \
    -H "Authorization: Bearer ${TAILSCALE_API_KEY}" \
    "https://api.tailscale.com/api/v2/tailnet/${TAILSCALE_TAILNET}/devices" \
    | jq -r '.devices[] | select(.hostname == "prod-node-1") | .addresses[0]' || true)

  if [[ -n "$REGISTERED" ]]; then
    log "prod-node-1 が Tailscale に登録されました: $REGISTERED"
    break
  fi

  (( TS_ELAPSED >= TS_TIMEOUT )) && die "prod-node-1 の Tailscale 登録がタイムアウトしました (${TS_TIMEOUT}s)"

  log "未登録 (経過: ${TS_ELAPSED}s) — ${TS_SLEEP}s 後に再確認します"
  sleep $TS_SLEEP
  TS_ELAPSED=$(( TS_ELAPSED + TS_SLEEP ))
done

# ============================================================
# 5. Ansible でシングルノード K3s をブートストラップ
# ============================================================

log "Ansible Playbook を実行します"
ANSIBLE_HOST_KEY_CHECKING=False \
  K3S_TOKEN="${K3S_TOKEN}" \
  CLOUDFLARE_TUNNEL_TOKEN="${CLOUDFLARE_TUNNEL_TOKEN}" \
  CLOUDFLARE_TUNNEL_ID="${CLOUDFLARE_TUNNEL_ID}" \
  INFISICAL_CLIENT_ID="${INFISICAL_CLIENT_ID}" \
  INFISICAL_CLIENT_SECRET="${INFISICAL_CLIENT_SECRET}" \
  ARGOCD_GITHUB_DEPLOY_KEY="${ARGOCD_GITHUB_DEPLOY_KEY}" \
  ansible-playbook \
    -i "${REPO_ROOT}/ansible/inventory/tailscale.yml" \
    "${REPO_ROOT}/ansible/playbooks/k3s-bootstrap.yml"

# Ansible が新しい kubeconfig を Infisical に登録するため、再取得して上書きする
KUBECONFIG_NEW=$(infisical secrets get KUBECONFIG --env=prod --plain 2>/dev/null || true)
if [[ -n "$KUBECONFIG_NEW" ]]; then
  echo "${KUBECONFIG_NEW}" > "${KUBECONFIG_FILE}"
  log "kubeconfig を Infisical から再取得しました"
fi

# ============================================================
# 6. ArgoCD sync 完了・CNPG healthy を待機
# ============================================================

log "ArgoCD sync と CNPG healthy を待機します (最大20分)"
ARGOCD_TIMEOUT=1200
ARGOCD_ELAPSED=0

while true; do
  NOT_HEALTHY=$(kubectl_r get applications -n argocd --no-headers 2>/dev/null \
    | awk '{print $3}' | grep -cv "Healthy" || echo "99")

  if [[ "$NOT_HEALTHY" -eq 0 ]]; then
    log "全 ArgoCD Application が Healthy になりました"
    break
  fi

  (( ARGOCD_ELAPSED >= ARGOCD_TIMEOUT )) && {
    log "警告: ArgoCD の Healthy 待機がタイムアウトしました。処理を続行します。"
    break
  }

  log "Healthy でない Application: ${NOT_HEALTHY} 件 (経過: ${ARGOCD_ELAPSED}s)"
  sleep 30
  ARGOCD_ELAPSED=$(( ARGOCD_ELAPSED + 30 ))
done

# CNPG クラスターが healthy になるまで待機
for CLUSTER in authentik-db directus-db; do
  log "CNPG クラスター ${CLUSTER} の healthy を待機します"
  kubectl_r wait cluster "${CLUSTER}" -n prod \
    --for=jsonpath='{.status.phase}'='Cluster in healthy state' \
    --timeout=600s || log "警告: ${CLUSTER} の healthy 待機がタイムアウトしました"
done

# ============================================================
# 7. Stalwart メールデータを VolSync で B2 からリストア
# ============================================================

log "Stalwart を停止して VolSync リストアを開始します"

kubectl_r scale statefulset stalwart -n prod --replicas=0
kubectl_r wait pod -n prod -l app=stalwart --for=delete --timeout=60s || true

# ReplicationDestination を適用
RESTORE_TRIGGER="dr-$(date +%Y%m%dT%H%M%S)"
kubectl_r apply -f - <<EOF
apiVersion: volsync.backube/v1alpha1
kind: ReplicationDestination
metadata:
  name: stalwart-restore
  namespace: prod
spec:
  trigger:
    manual: "${RESTORE_TRIGGER}"
  restic:
    repository: stalwart-restic-secret
    destinationPVC: stalwart-data
    copyMethod: Direct
    moverSecurityContext:
      runAsUser: 0
      runAsGroup: 0
      fsGroup: 0
EOF

log "VolSync リストア完了を待機します (最大30分)"
kubectl_r wait replicationdestination/stalwart-restore -n prod \
  --for=condition=Reconciled --timeout=1800s

log "VolSync リストア完了"

# リストア用リソースを削除
kubectl_r delete replicationdestination stalwart-restore -n prod

# Stalwart を再起動
kubectl_r scale statefulset stalwart -n prod --replicas=1
kubectl_r wait pod -n prod -l app=stalwart --for=condition=Ready --timeout=120s

log "復旧完了"
