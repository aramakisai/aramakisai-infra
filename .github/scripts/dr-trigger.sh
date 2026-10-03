#!/usr/bin/env bash
# DR 障害検知スクリプト (通知のみ。復旧は dr-recovery ワークフローを人が承認付きで実行する)
#
# .github/workflows/dr-trigger.yml (5分毎 cron) から実行される。
# Tailscale 上の実在ノードの接続状態と複数サービスエンドポイントの疎通を複合的に評価し、
# ノード障害と判定した場合は Discord 通知と dr-incident Issue の起票/追記のみを行う。
#
# 単体テスト: ./scripts/test-dr-trigger-logic.sh
#   (このファイルを source し、判定ロジックのみをネットワークアクセスなしで検証する)

set -uo pipefail

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') [dr-trigger] $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

REPO="${GITHUB_REPOSITORY:-aramakisai/aramakisai-infra}"
# 実在ノード判定に使うホスト名パターン。Terraform/inventory の定義ではなく Tailscale 上の実デバイスを見る
NODE_HOSTNAME_REGEX='^prod-node-[0-9]+$'
INCIDENT_LABEL="dr-incident"
# 1回の実行内で連続して障害と判定された回数がこの値に達したときだけ障害として扱う
PROBE_COUNT="${DR_TRIGGER_PROBES:-3}"
PROBE_INTERVAL_SECONDS="${DR_TRIGGER_PROBE_INTERVAL:-30}"
CURL_TIMEOUT_SECONDS=10
CURL_RETRIES=2
ENDPOINTS=(
  "https://idp.aramakisai.com"
  "https://argocd.aramakisai.com"
  "https://webmail.aramakisai.com"
)

# ============================================================
# 複合検出ロジック (ネットワークアクセスなしでテスト可能)
# ============================================================

# 実在ノードの接続状態を集約する。
# 引数: devices API の JSON
# 出力: 1=全ノード接続 / degraded=一部のみ切断でクォーラム (過半数) 維持 /
#       0=全断・クォーラム喪失・ノード未検出
# 非 ephemeral のため停止済みの旧デバイスが残っていると total が増える。その場合は
# 過半数判定が厳しめに出る (誤検知側) ので、再作成後は旧デバイスを削除しておくこと。
tailscale_state_from_devices() {
  echo "$1" | jq -r --arg re "${NODE_HOSTNAME_REGEX}" '
    [.devices[] | select(.hostname | test($re))] as $n
    | ($n | length) as $total
    | ([$n[] | select(.connectedToControl == true)] | length) as $online
    | if $total == 0 or $online * 2 <= $total then "0"
      elif $online < $total then "degraded"
      else "1" end'
}

# 引数: tailscale_state (1 / degraded / 0 / unknown), down_count (応答なしエンドポイント数)
# 出力: NodeFailureSuspected | NodeDegraded | SingleEndpointDown | Healthy
# unknown の場合は Tailscale シグナルを除外し down_count のみで判定する
# (API 疎通不良を誤ってノード障害扱いしないため)
classify_state() {
  local tailscale_state="$1" down_count="$2"

  if [[ "${tailscale_state}" == "0" ]]; then
    echo "NodeFailureSuspected"
  elif [[ "${down_count}" -ge 2 ]]; then
    echo "NodeFailureSuspected"
  elif [[ "${tailscale_state}" == "degraded" ]]; then
    echo "NodeDegraded"
  elif [[ "${down_count}" -eq 1 ]]; then
    echo "SingleEndpointDown"
  else
    echo "Healthy"
  fi
}

# 連続 probe の判定結果から最終状態を決める。
# 引数: probe ごとの classify_state 結果 (古い順)
# 出力: 最後が障害でなければその状態。障害が PROBE_COUNT 回連続したときだけ NodeFailureSuspected。
#       それ以外 (障害が続いているが回数不足) は Pending。
decide_final_state() {
  local last="${!#}"
  if [[ "${last}" != "NodeFailureSuspected" ]]; then
    echo "${last}"
  elif (($# >= PROBE_COUNT)); then
    echo "NodeFailureSuspected"
  else
    echo "Pending"
  fi
}

# ============================================================
# 外部 API 呼び出し (Tailscale / 公開エンドポイント)
# ============================================================

# OAuth Client Credentials (失効しない) から短命 access token (1時間) を取得する。
# 実行毎に取得し直すため、キャッシュはしない。
# 出力: access token / 空文字列 (取得失敗)
fetch_tailscale_access_token() {
  local response
  response=$(curl -sf -X POST "https://api.tailscale.com/api/v2/oauth/token" \
    -d "client_id=${TAILSCALE_OAUTH_CLIENT_ID}" \
    -d "client_secret=${TAILSCALE_OAUTH_CLIENT_SECRET}") || return 1
  echo "${response}" | jq -r '.access_token // empty'
}

# 出力: tailscale_state_from_devices の値 / unknown=API呼び出し失敗 (判定不能)
check_tailscale_state() {
  local token response

  token=$(fetch_tailscale_access_token)
  if [[ -z "${token}" ]]; then
    log "警告: Tailscale OAuth token の取得に失敗しました (判定不能として続行します)"
    echo "unknown"
    return
  fi
  echo "::add-mask::${token}" >&2

  if ! response=$(curl -sf \
    -H "Authorization: Bearer ${token}" \
    "https://api.tailscale.com/api/v2/tailnet/${TAILSCALE_TAILNET}/devices"); then
    log "警告: Tailscale Devices API の呼び出しに失敗しました (判定不能として続行します)"
    echo "unknown"
    return
  fi

  tailscale_state_from_devices "${response}"
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

# 1回分の観測。出力: "<state>|<tailscale_state>|<down_list>"
probe_once() {
  local tailscale_state down_count=0 down_list=() endpoint state
  tailscale_state=$(check_tailscale_state)
  for endpoint in "${ENDPOINTS[@]}"; do
    if [[ "$(check_endpoint_up "${endpoint}")" -eq 0 ]]; then
      down_count=$((down_count + 1))
      down_list+=("${endpoint}")
    fi
  done
  state=$(classify_state "${tailscale_state}" "${down_count}")
  echo "${state}|${tailscale_state}|$(IFS=', '; echo "${down_list[*]:-}")"
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

# ============================================================
# GitHub Issue による記録 (open な dr-incident があれば追記のみ)
# ============================================================

ensure_incident_label() {
  gh api "repos/${REPO}/labels/${INCIDENT_LABEL}" >/dev/null 2>&1 && return 0
  gh api "repos/${REPO}/labels" \
    -f name="${INCIDENT_LABEL}" \
    -f color="d73a4a" \
    -f description="DR: ノード障害疑いインシデント" >/dev/null
}

find_open_incident() {
  gh issue list --repo "${REPO}" --label "${INCIDENT_LABEL}" --state open \
    --json number --jq '.[0].number // empty'
}

create_incident_issue() {
  local detail="$1" now body
  now="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  # shellcheck disable=SC2016 # バッククォートはMarkdown装飾の文字リテラル (展開不要)
  body=$(printf '## ノード障害疑い検知\n\n- 内容: %s\n- 検知時刻 (UTC): %s\n\n検知は通知のみです。復旧する場合は Actions の `DR Recovery` を `workflow_dispatch` で実行し、required reviewers の承認を受けてください (手順: docs/dr-runbook.md)。復旧ワークフローは冒頭で生存確認を行い、ノードが生きていれば停止します。' \
    "${detail}" "${now}")
  ensure_incident_label
  gh issue create --repo "${REPO}" --title "DR: ノード障害疑い (${now})" --body "${body}" \
    --label "${INCIDENT_LABEL}" | grep -oE '[0-9]+$'
}

comment_issue() {
  gh issue comment "$1" --repo "${REPO}" --body "$2" >/dev/null
}

close_issue_recovered() {
  gh issue close "$1" --repo "${REPO}" \
    --comment "全シグナルが正常に戻ったため、このインシデントをクローズします。"
}

# cron は5分毎なので、継続中の障害の再通知・追記は毎時1回程度に間引く
hourly_slot() { [[ "$(date -u +%-M)" -lt 5 ]]; }

# ============================================================
# メイン処理
# ============================================================

main() {
  local required_vars=(TAILSCALE_OAUTH_CLIENT_ID TAILSCALE_OAUTH_CLIENT_SECRET TAILSCALE_TAILNET DISCORD_OPS_WEBHOOK_URL GH_TOKEN)
  local var
  for var in "${required_vars[@]}"; do
    [[ -n "${!var:-}" ]] || die "必須環境変数が未設定です: ${var}"
  done

  local states=() result state ts_state down_list i
  for ((i = 1; i <= PROBE_COUNT; i++)); do
    result=$(probe_once)
    state="${result%%|*}"
    ts_state=$(echo "${result}" | cut -d'|' -f2)
    down_list="${result##*|}"
    states+=("${state}")
    log "probe ${i}/${PROBE_COUNT}: ${state} (tailscale=${ts_state}, down=[${down_list}])"
    [[ "${state}" == "NodeFailureSuspected" ]] || break
    ((i < PROBE_COUNT)) && sleep "${PROBE_INTERVAL_SECONDS}"
  done

  # Tailscale API障害はActionsログにしか気づけないため通知する (毎時1回)
  if [[ "${ts_state}" == "unknown" ]] && hourly_slot; then
    notify_discord "⚠️ **DR Trigger**: Tailscale Devices API の呼び出しに失敗しています (OAuth クライアントの失効などをご確認ください)。判定はエンドポイント疎通のみで継続します。"
  fi

  local final existing_issue detail
  final=$(decide_final_state "${states[@]}")
  detail="state=${final}, tailscale=${ts_state}, down_endpoints=[${down_list}]"
  log "最終判定: ${final} (${detail})"
  existing_issue=$(find_open_incident)

  case "${final}" in
    Healthy)
      [[ -z "${existing_issue}" ]] || close_issue_recovered "${existing_issue}"
      ;;

    Pending)
      log "障害判定が ${PROBE_COUNT} 回連続に達していないため通知しません"
      ;;

    SingleEndpointDown | NodeDegraded)
      if hourly_slot; then
        notify_discord "$(printf '⚠️ **DR Trigger**: ノード全体の障害ではありません (%s)\n%s\n人間による確認をお願いします。' "${final}" "${detail}")"
      fi
      ;;

    NodeFailureSuspected)
      if [[ -z "${existing_issue}" ]]; then
        existing_issue=$(create_incident_issue "${detail}")
        notify_discord "$(printf '🚨 **DR Trigger**: ノード障害疑いを検知しました (%s)\nIssue: https://github.com/%s/issues/%s\n自動復旧は行いません。復旧する場合は Actions の DR Recovery を実行し承認してください (docs/dr-runbook.md)。' "${detail}" "${REPO}" "${existing_issue}")"
        log "新規インシデントを作成しました (Issue #${existing_issue})"
      elif hourly_slot; then
        comment_issue "${existing_issue}" "障害継続中: ${detail} ($(date -u '+%Y-%m-%dT%H:%M:%SZ'))"
        notify_discord "$(printf '🚨 **DR Trigger**: ノード障害疑いが継続しています (%s)\nIssue: https://github.com/%s/issues/%s' "${detail}" "${REPO}" "${existing_issue}")"
      fi
      ;;
  esac
}

if [[ "${BASH_SOURCE[0]:-$0}" == "${0}" ]]; then
  main "$@"
fi
