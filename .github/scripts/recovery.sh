#!/usr/bin/env bash
# K3s コールドスタンバイ復旧スクリプト
# 実行フロー:
#   0. CNPG 古い Job をベストエフォートで削除 (non-fatal)
#   1. 必須環境変数チェック
#   2. Tailscale から prod-node-1 デバイスを削除 (残存デバイスによる重複ホスト名防止)
#   3. Terraform Cloud API でノードを再作成
#   4. Tailscale に prod-node-1 として登録されるまでポーリング (最大10分)
#   5. Ansible でシングルノード K3s をブートストラップ
#   6. ArgoCD sync 完了・CNPG healthy を待機
#   6a. infisical-auth / Deploy Key 空チェックと自己修復
#   6b. mail-tls 証明書の自己修復
#   7. mailserver メールデータを VolSync で Hetzner Object Storage からリストア
#      (完了時に directus-db リストア確認ログを出力)
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
# 0. CNPG 古い Job をベストエフォートで削除
#    再実行時に残存した full-recovery Job が PVC を initializing で stuck させる問題を予防する
#    クラスターが存在しない場合や到達不可の場合は非 fatal で続行する
# ============================================================

log "CNPG 古い Job をクリーンアップします (non-fatal)"

kubectl_r delete jobs -n prod -l "cnpg.io/cluster=authentik-db" \
  --request-timeout=10s 2>/dev/null \
  || log "警告: authentik-db の Job 削除をスキップしました (クラスター不在またはタイムアウト)"

kubectl_r delete jobs -n prod -l "cnpg.io/cluster=directus-db" \
  --request-timeout=10s 2>/dev/null \
  || log "警告: directus-db の Job 削除をスキップしました (クラスター不在またはタイムアウト)"

log "CNPG Job クリーンアップ完了 (または非 fatal スキップ)"

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
# 6a. infisical-auth / Deploy Key 空チェックと自己修復
#     2026-06-02 インシデント: infisical-auth と Deploy Key が空になり ESO 全停止・ArgoCD 接続不可
# ============================================================

log "infisical-auth と Deploy Key の空チェックを開始します"

INFISICAL_AUTH_CLIENT_ID=$(kubectl_r get secret infisical-auth -n argocd \
  -o jsonpath='{.data.clientId}' 2>/dev/null | base64 -d || true)
DEPLOY_KEY_LEN=$(kubectl_r get secret aramakisai-infra-repo -n argocd \
  -o jsonpath='{.data.sshPrivateKey}' 2>/dev/null | base64 -d | wc -c || echo "0")

NEEDS_SECRET_REPAIR=false
[[ -z "$INFISICAL_AUTH_CLIENT_ID" ]] && { log "警告: infisical-auth.clientId が空です"; NEEDS_SECRET_REPAIR=true; }
[[ "$DEPLOY_KEY_LEN" -lt 100 ]] && { log "警告: aramakisai-infra-repo.sshPrivateKey が空または短すぎます"; NEEDS_SECRET_REPAIR=true; }

if [[ "$NEEDS_SECRET_REPAIR" == "true" ]]; then
  log "Infisical から再取得して infisical-auth を修復します"

  kubectl_r create secret generic infisical-auth \
    --from-literal=clientId="${INFISICAL_CLIENT_ID}" \
    --from-literal=clientSecret="${INFISICAL_CLIENT_SECRET}" \
    -n argocd --dry-run=client -o yaml | kubectl_r apply -f -

  log "ESO の ExternalSecret に force-sync annotation を付与します"
  kubectl_r annotate externalsecret --all -n prod \
    "force-sync=$(date +%s)" --overwrite

  log "infisical-auth 修復完了・ESO force-sync 実行済み"
else
  log "infisical-auth と Deploy Key は正常です"
fi

# ============================================================
# 6b. mail-tls 証明書の自己修復
#     mailserver Pod が ContainerCreating で 2 分以上停止している場合に mail-tls を確認・修復する
# ============================================================

log "mailserver Pod の状態を確認します"

CONTAINER_CREATING_REASON=$(kubectl_r get pod -n prod -l app=mailserver \
  -o jsonpath='{.items[0].status.containerStatuses[0].state.waiting.reason}' 2>/dev/null || true)

if [[ "$CONTAINER_CREATING_REASON" == "ContainerCreating" ]]; then
  POD_START=$(kubectl_r get pod -n prod -l app=mailserver \
    -o jsonpath='{.items[0].status.startTime}' 2>/dev/null || true)

  if [[ -n "$POD_START" ]]; then
    START_EPOCH=$(date -d "$POD_START" +%s 2>/dev/null || true)
    NOW_EPOCH=$(date +%s)
    STUCK_SECS=$(( NOW_EPOCH - ${START_EPOCH:-NOW_EPOCH} ))

    if [[ $STUCK_SECS -ge 120 ]]; then
      log "mailserver Pod が ContainerCreating で ${STUCK_SECS}s 停止しています"

      MAIL_TLS_EXISTS=$(kubectl_r get secret mail-tls -n prod --ignore-not-found 2>/dev/null || true)
      if [[ -z "$MAIL_TLS_EXISTS" ]]; then
        log "mail-tls Secret が存在しません。certificate.yaml 等を apply します"
        kubectl_r apply -f "${REPO_ROOT}/gitops/manifests/prod/mailserver/certificate.yaml"
        kubectl_r apply -f "${REPO_ROOT}/gitops/manifests/prod/mailserver/external-secret.yaml"
        kubectl_r apply -f "${REPO_ROOT}/gitops/manifests/prod/mailserver/restic-external-secret.yaml"
        log "mail-tls 関連リソースを apply しました"
      else
        log "mail-tls Secret は存在します (Pod stuck の原因は別)"
      fi
    else
      log "mailserver Pod は ContainerCreating ですが待機時間が短いため様子を見ます (${STUCK_SECS}s)"
    fi
  fi
else
  log "mailserver Pod は ContainerCreating 以外の状態です (status: ${CONTAINER_CREATING_REASON:-不明})"
fi

# ============================================================
# 7. mailserver メールデータを VolSync で Hetzner Object Storage からリストア
# ============================================================

log "mailserver を停止して VolSync リストアを開始します"

kubectl_r scale statefulset mailserver -n prod --replicas=0
kubectl_r wait pod -n prod -l app=mailserver --for=delete --timeout=60s || true

# ReplicationDestination を適用
RESTORE_TRIGGER="dr-$(date +%Y%m%dT%H%M%S)"
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

log "VolSync リストア完了を待機します (最大30分)"
kubectl_r wait replicationdestination/mailserver-restore -n prod \
  --for=condition=Reconciled --timeout=1800s

log "VolSync リストア完了"

# リストア用リソースを削除
kubectl_r delete replicationdestination mailserver-restore -n prod

# mailserver を再起動
kubectl_r scale statefulset mailserver -n prod --replicas=1
kubectl_r wait pod -n prod -l app=mailserver --for=condition=Ready --timeout=120s

# Step 7 末尾: directus-db リストア確認ログ
log "directus-db リストア確認を行います"

FIRST_RECOV=$(kubectl_r get cluster directus-db -n prod \
  -o jsonpath='{.status.firstRecoverabilityPoint}' 2>/dev/null || true)
log "directus-db firstRecoverabilityPoint: ${FIRST_RECOV:-未取得 (initdb 起動の可能性あり)}"

DIRECTUS_PRIMARY=$(kubectl_r get pod -n prod \
  -l "cnpg.io/cluster=directus-db,role=primary" \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)

if [[ -n "$DIRECTUS_PRIMARY" ]]; then
  TABLE_COUNT=$(kubectl_r exec -n prod "${DIRECTUS_PRIMARY}" \
    -- psql -U postgres -d directus \
    -c "SELECT count(*) FROM pg_tables WHERE schemaname='public';" \
    --tuples-only --no-align 2>/dev/null | tr -d '[:space:]' || true)
  log "directus-db public スキーマのテーブル数: ${TABLE_COUNT:-取得失敗}"
else
  log "警告: directus-db primary Pod が見つかりません"
fi

log "復旧完了"
