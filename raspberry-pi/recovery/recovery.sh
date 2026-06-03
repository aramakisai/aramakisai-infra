#!/usr/bin/env bash
# K3s コールドスタンバイ復旧スクリプト
# 実行フロー:
#   1. 必須環境変数チェック
#   2. Tailscale から prod-node-1 デバイスを削除 (残存デバイスによる重複ホスト名防止)
#   3. Terraform Cloud API でノードを再作成
#   4. Tailscale に prod-node-1 として登録されるまでポーリング (最大10分)
#   5. Ansible でシングルノード K3s をブートストラップ
#
# 注意: このスクリプトは recover.py から呼び出される。
#       完了後、recover.py が /tmp/recovery.lock を削除する。

set -euo pipefail

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') [recovery] $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

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
)

for VAR in "${REQUIRED_VARS[@]}"; do
  [[ -n "${!VAR:-}" ]] || die "必須環境変数が未設定です: $VAR"
done

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

# Run が完了するまで待機 (最大15分)
log "Terraform Cloud の Apply 完了を待機します"
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

  if (( ELAPSED >= TIMEOUT )); then
    die "Terraform Cloud Apply がタイムアウトしました (${TIMEOUT}s)"
  fi

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

  if (( TS_ELAPSED >= TS_TIMEOUT )); then
    die "prod-node-1 の Tailscale 登録がタイムアウトしました (${TS_TIMEOUT}s)"
  fi

  log "未登録 (経過: ${TS_ELAPSED}s) — ${TS_SLEEP}s 後に再確認します"
  sleep $TS_SLEEP
  TS_ELAPSED=$(( TS_ELAPSED + TS_SLEEP ))
done

# ============================================================
# 5. Ansible でシングルノード K3s をブートストラップ
# ============================================================

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

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

log "復旧完了"
