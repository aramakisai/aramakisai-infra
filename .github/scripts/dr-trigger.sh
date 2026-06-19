#!/usr/bin/env bash
# DR 自動復旧トリガー判定スクリプト
#
# .github/workflows/dr-trigger.yml (5分毎 cron) から実行される。
# Tailscale デバイス状態 + 複数サービスエンドポイントの疎通を複合的に評価し、
# ノード障害と判定した場合のみ「通知 + 猶予期間オプトアウト」方式で
# repository_dispatch (event_type: dr-recovery) を発火する。
#
# 単体テスト: ./scripts/test-dr-trigger-logic.sh
#   (このファイルを source し、classify_state / is_abort_comment のみを
#    ネットワークアクセスなしで検証する)

set -uo pipefail

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') [dr-trigger] $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

REPO="${GITHUB_REPOSITORY:-aramakisai/aramakisai-infra}"
TARGET_HOSTNAME="${DR_TRIGGER_TARGET_HOSTNAME:-prod-node-1}"
INCIDENT_LABEL="dr-incident"
GRACE_PERIOD_MINUTES="${DR_TRIGGER_GRACE_MINUTES:-10}"
CURL_TIMEOUT_SECONDS=10
CURL_RETRIES=2
ENDPOINTS=(
  "https://idp.aramakisai.com"
  "https://argocd.aramakisai.com"
  "https://webmail.aramakisai.com"
)

# ============================================================
# 複合検出ロジック (Requirement 1 AC1-3, ネットワークアクセスなしでテスト可能)
# ============================================================

# 引数: tailscale_online (1=オンライン/0=オフライン), down_count (応答なしエンドポイント数)
# 出力: NodeFailureSuspected | SingleEndpointDown | Healthy
classify_state() {
  local tailscale_online="$1" down_count="$2"

  if [[ "${tailscale_online}" -eq 0 ]]; then
    echo "NodeFailureSuspected"
  elif [[ "${down_count}" -ge 2 ]]; then
    echo "NodeFailureSuspected"
  elif [[ "${down_count}" -eq 1 ]]; then
    echo "SingleEndpointDown"
  else
    echo "Healthy"
  fi
}

# ============================================================
# 中止操作の権限フィルタ (Requirement 1.6, ネットワークアクセスなしでテスト可能)
# ============================================================

# 引数: author_association (OWNER/MEMBER/COLLABORATOR/NONE等), コメント本文
# 戻り値: 0=中止操作として有効 / 1=無効
is_abort_comment() {
  local association="$1" body="$2"

  case "${association}" in
    OWNER | MEMBER | COLLABORATOR) ;;
    *) return 1 ;;
  esac

  echo "${body}" | grep -qiE 'abort|中止'
}

# ============================================================
# 外部 API 呼び出し (Tailscale / 公開エンドポイント)
# ============================================================

# 出力: 1=オンライン / 0=オフライン・デバイス未検出
check_tailscale_online() {
  local response online
  response=$(curl -sf \
    -H "Authorization: Bearer ${TAILSCALE_API_KEY}" \
    "https://api.tailscale.com/api/v2/tailnet/${TAILSCALE_TAILNET}/devices") \
    || die "Tailscale Devices API の呼び出しに失敗しました"

  online=$(echo "${response}" | jq -r --arg h "${TARGET_HOSTNAME}" \
    '[.devices[] | select(.hostname == $h) | .connectedToControl] | first // false')

  if [[ "${online}" == "true" ]]; then
    echo 1
  else
    echo 0
  fi
}

# 出力: 1=到達可能 / 0=到達不可 (タイムアウト+リトライ込み)
check_endpoint_up() {
  local url="$1" attempt
  for ((attempt = 1; attempt <= CURL_RETRIES; attempt++)); do
    if curl -sf -o /dev/null --max-time "${CURL_TIMEOUT_SECONDS}" "${url}"; then
      echo 1
      return
    fi
    sleep 2
  done
  echo 0
}

# ============================================================
# Discord 通知
# ============================================================

notify_discord() {
  local message="$1"
  curl -sf -X POST \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg content "${message}" '{content: $content}')" \
    "${DISCORD_OPS_WEBHOOK_URL}" >/dev/null \
    || log "警告: Discord 通知に失敗しました"
}

notify_discord_single_endpoint_down() {
  local down_list="$1"
  notify_discord "$(printf '⚠️ **DR Trigger**: 単体サービス障害を検知しました (ノード障害ではありません)\n応答なし: %s\nTailscale: オンライン\n人間による確認をお願いします (アプリ再起動など)。' "${down_list}")"
}

notify_discord_node_failure() {
  local issue_number="$1" reason="$2"
  # shellcheck disable=SC2016 # バッククォートはMarkdown装飾の文字リテラル (展開不要)
  notify_discord "$(printf '🚨 **DR Trigger**: ノード障害疑いを検知しました\n理由: %s\nIssue: https://github.com/%s/issues/%s\n猶予期間 (%s分) 経過後、自動で復旧ワークフロー (repository_dispatch: dr-recovery) を発火します。\n中止する場合はIssueに `abort` または `中止` を含むコメントを付けてください (OWNER/MEMBER/COLLABORATOR 権限が必要です)。' "${reason}" "${REPO}" "${issue_number}" "${GRACE_PERIOD_MINUTES}")"
}

# ============================================================
# GitHub Issue による状態管理 (Requirement 1.5-1.8, 単一インシデント保証)
# ============================================================

ensure_incident_label() {
  gh api "repos/${REPO}/labels/${INCIDENT_LABEL}" >/dev/null 2>&1 && return 0
  gh api "repos/${REPO}/labels" \
    -f name="${INCIDENT_LABEL}" \
    -f color="d73a4a" \
    -f description="DR自動復旧トリガー: ノード障害疑いインシデント" >/dev/null
}

find_open_incident() {
  gh issue list --repo "${REPO}" --label "${INCIDENT_LABEL}" --state open \
    --json number --jq '.[0].number // empty'
}

create_incident_issue() {
  local reason="$1"
  local now
  now="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  local title="DR: ${TARGET_HOSTNAME} ノード障害疑い (${now})"
  local body
  # shellcheck disable=SC2016 # バッククォートはMarkdown装飾の文字リテラル (展開不要)
  body=$(printf '## ノード障害疑い検知\n\n- 理由: %s\n- 検知時刻 (UTC): %s\n- 猶予期間: %s分\n\n猶予期間経過後、運用者の中止操作がない場合は自動で `repository_dispatch` (event_type: `dr-recovery`) を発火します。\n\n**中止する場合**: このIssueに `abort` または `中止` を含むコメントを付けるか、Issueをクローズしてください (OWNER/MEMBER/COLLABORATOR 権限が必要です)。' \
    "${reason}" "${now}" "${GRACE_PERIOD_MINUTES}")

  ensure_incident_label
  gh issue create --repo "${REPO}" --title "${title}" --body "${body}" \
    --label "${INCIDENT_LABEL}" \
    | grep -oE '[0-9]+$'
}

close_issue_recovered() {
  local issue_number="$1"
  gh issue close "${issue_number}" --repo "${REPO}" \
    --comment "全シグナルが猶予期間内に復旧したため、このインシデントをクローズします。"
}

close_issue_aborted() {
  local issue_number="$1"
  gh issue close "${issue_number}" --repo "${REPO}" \
    --comment "運用者の中止操作を確認したため、repository_dispatch を発火せずこのインシデントをクローズします。"
}

close_issue_dispatched() {
  local issue_number="$1"
  gh issue close "${issue_number}" --repo "${REPO}" \
    --comment "猶予期間が経過し中止操作がなかったため、repository_dispatch (dr-recovery) を発火しました。"
}

# author_association によるコメントベースの中止操作検出
comment_abort_found() {
  local issue_number="$1" comments_json count i assoc body
  comments_json=$(gh api "repos/${REPO}/issues/${issue_number}/comments" --paginate) || return 1
  count=$(echo "${comments_json}" | jq 'length')

  for ((i = 0; i < count; i++)); do
    assoc=$(echo "${comments_json}" | jq -r ".[${i}].author_association")
    body=$(echo "${comments_json}" | jq -r ".[${i}].body")
    if is_abort_comment "${assoc}" "${body}"; then
      return 0
    fi
  done
  return 1
}

# Issue クローズによる中止操作検出。
# GitHub API は close 操作の actor に author_association を直接提供しないため、
# 同等の権限判定として collaborator permission (admin/write) を使う。
issue_closed_by_authorized_user() {
  local issue_number="$1" issue_json state closer permission
  issue_json=$(gh api "repos/${REPO}/issues/${issue_number}") || return 1
  state=$(echo "${issue_json}" | jq -r '.state')
  [[ "${state}" == "closed" ]] || return 1

  closer=$(echo "${issue_json}" | jq -r '.closed_by.login // empty')
  [[ -n "${closer}" ]] || return 1
  [[ "${closer}" == "github-actions[bot]" ]] && return 1

  permission=$(gh api "repos/${REPO}/collaborators/${closer}/permission" --jq '.permission' 2>/dev/null || echo "none")
  [[ "${permission}" == "admin" || "${permission}" == "write" ]]
}

abort_requested() {
  local issue_number="$1"
  comment_abort_found "${issue_number}" || issue_closed_by_authorized_user "${issue_number}"
}

elapsed_minutes_since() {
  local created_at="$1"
  echo $(( ($(date -u +%s) - $(date -u -d "${created_at}" +%s)) / 60 ))
}

dispatch_recovery() {
  gh api "repos/${REPO}/dispatches" -f event_type="dr-recovery" >/dev/null
}

# 既に open な dr-incident Issue を、猶予期間判定・中止判定の対象として処理する
process_existing_incident() {
  local issue_number="$1"

  if abort_requested "${issue_number}"; then
    log "中止操作を検出しました (Issue #${issue_number})。dispatch を発火しません。"
    close_issue_aborted "${issue_number}"
    return
  fi

  local created_at elapsed
  created_at=$(gh issue view "${issue_number}" --repo "${REPO}" --json createdAt --jq '.createdAt')
  elapsed=$(elapsed_minutes_since "${created_at}")

  if ((elapsed >= GRACE_PERIOD_MINUTES)); then
    log "猶予期間 (${GRACE_PERIOD_MINUTES}分) が経過しました (経過: ${elapsed}分)。repository_dispatch を発火します。"
    dispatch_recovery
    close_issue_dispatched "${issue_number}"
  else
    log "猶予期間内です (経過: ${elapsed}分 / ${GRACE_PERIOD_MINUTES}分)。待機します。"
  fi
}

# ============================================================
# メイン処理
# ============================================================

main() {
  local required_vars=(TAILSCALE_API_KEY TAILSCALE_TAILNET DISCORD_OPS_WEBHOOK_URL GH_TOKEN)
  local var
  for var in "${required_vars[@]}"; do
    [[ -n "${!var:-}" ]] || die "必須環境変数が未設定です: ${var}"
  done

  local tailscale_online down_count=0 down_list=()
  tailscale_online=$(check_tailscale_online)

  local endpoint up
  for endpoint in "${ENDPOINTS[@]}"; do
    up=$(check_endpoint_up "${endpoint}")
    if [[ "${up}" -eq 0 ]]; then
      down_count=$((down_count + 1))
      down_list+=("${endpoint}")
    fi
  done

  local state down_list_str
  down_list_str=$(IFS=', '; echo "${down_list[*]}")
  state=$(classify_state "${tailscale_online}" "${down_count}")
  log "判定結果: ${state} (tailscale_online=${tailscale_online}, down_count=${down_count}, down=[${down_list_str}])"

  local existing_issue
  existing_issue=$(find_open_incident)

  case "${state}" in
    Healthy)
      if [[ -n "${existing_issue}" ]]; then
        close_issue_recovered "${existing_issue}"
      fi
      log "正常: 何もしません"
      ;;

    SingleEndpointDown)
      notify_discord_single_endpoint_down "${down_list_str}"
      if [[ -n "${existing_issue}" ]]; then
        process_existing_incident "${existing_issue}"
      fi
      ;;

    NodeFailureSuspected)
      if [[ -z "${existing_issue}" ]]; then
        existing_issue=$(create_incident_issue "Tailscale offline=$((1 - tailscale_online)), down_endpoints=[${down_list_str}]")
        notify_discord_node_failure "${existing_issue}" "Tailscaleオフラインまたは複数エンドポイント同時ダウン"
        log "新規インシデントを作成し、猶予期間を開始しました (Issue #${existing_issue})"
      else
        process_existing_incident "${existing_issue}"
      fi
      ;;
  esac
}

if [[ "${BASH_SOURCE[0]:-$0}" == "${0}" ]]; then
  main "$@"
fi
