#!/usr/bin/env bash
# インフラヘルスチェック: CNPG WAL アーカイブ失敗 + ノードルートディスク使用率
#
# .github/workflows/infra-health-check.yml (cron) から実行される。
# KUBECONFIG は Infisical から注入される (make kubectl と同じ値)。
# 状態の持続化は GitHub Issue (ラベル infra-alert) で行う。dr-trigger.sh と異なり
# 自動復旧アクションは発火しない (通知のみ)。同一問題での cron 実行毎の再通知を
# 避けるため、Issue 本文に埋め込んだキーで既存インシデントを識別し、
# 発生時のみ新規作成・解消時のみクローズする。

set -uo pipefail

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') [infra-health-check] $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

REPO="${GITHUB_REPOSITORY:-aramakisai/aramakisai-infra}"
ALERT_LABEL="infra-alert"
NODE_NAME="${INFRA_HEALTH_NODE_NAME:-prod-node-1}"
DISK_THRESHOLD_PERCENT="${INFRA_HEALTH_DISK_THRESHOLD_PERCENT:-85}"

# ============================================================
# kubectl (KUBECONFIG は Infisical run 経由で環境変数として注入済み)
# ============================================================

KUBECTL_CONF="/tmp/kubeconfig-infra-health-check"

setup_kubeconfig() {
  [[ -n "${KUBECONFIG:-}" ]] || die "KUBECONFIG が未設定です"
  echo "${KUBECONFIG}" >"${KUBECTL_CONF}"
  chmod 600 "${KUBECTL_CONF}"
}

kc() {
  kubectl --kubeconfig="${KUBECTL_CONF}" "$@"
}

# ============================================================
# チェック1: ノードルートディスク使用率
# ============================================================

# 出力: "breach|<usage_percent>" または "ok|<usage_percent>"
check_disk_usage() {
  local stats used capacity percent
  stats=$(kc get --raw "/api/v1/nodes/${NODE_NAME}/proxy/stats/summary") \
    || { log "警告: ${NODE_NAME} の stats/summary 取得に失敗しました"; echo "unknown|0"; return; }

  used=$(echo "${stats}" | jq -r '.node.fs.usedBytes // empty')
  capacity=$(echo "${stats}" | jq -r '.node.fs.capacityBytes // empty')
  if [[ -z "${used}" || -z "${capacity}" || "${capacity}" == "0" ]]; then
    log "警告: ${NODE_NAME} の fs 使用量を取得できませんでした"
    echo "unknown|0"
    return
  fi

  percent=$(( used * 100 / capacity ))
  if ((percent >= DISK_THRESHOLD_PERCENT)); then
    echo "breach|${percent}"
  else
    echo "ok|${percent}"
  fi
}

# ============================================================
# チェック2: CNPG WAL アーカイブ失敗
# (Cluster.status.conditions の ContinuousArchiving 条件を使う。
#  cnpg_pg_stat_archiver_failed_count 相当の一次情報として
#  CNPG operator 自身がこの条件を管理しており、追加のメトリクス収集基盤なしに読める)
# ============================================================

# 出力: 1行1クラスターで "<namespace>/<name>|breach|<message>" または "<namespace>/<name>|ok|"
check_cnpg_archiving() {
  local clusters
  clusters=$(kc get clusters.postgresql.cnpg.io -A -o json) \
    || { log "警告: CNPG Cluster 一覧の取得に失敗しました"; return; }

  echo "${clusters}" | jq -r '
    .items[] |
    . as $c |
    ($c.status.conditions // [] | map(select(.type == "ContinuousArchiving")) | first) as $cond |
    if $cond == null then
      "\($c.metadata.namespace)/\($c.metadata.name)|unknown|"
    elif $cond.status == "True" then
      "\($c.metadata.namespace)/\($c.metadata.name)|ok|"
    else
      "\($c.metadata.namespace)/\($c.metadata.name)|breach|\($cond.message // "reason unknown")"
    end
  '
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
# GitHub Issue によるアラート状態の管理 (キー単位でdedupe、dr-trigger.shと同じパターン)
# ============================================================

ensure_alert_label() {
  gh api "repos/${REPO}/labels/${ALERT_LABEL}" >/dev/null 2>&1 && return 0
  gh api "repos/${REPO}/labels" \
    -f name="${ALERT_LABEL}" \
    -f color="e99695" \
    -f description="インフラヘルスチェック: 閾値超過アラート" >/dev/null
}

# 引数: key
# 出力: マッチしたIssue番号 (なければ空)
# gh issue list の --jq は jq 本体の --arg のような追加フラグを受け付けないため、
# 素の jq へパイプして --arg で安全にキーを渡す
find_open_alert() {
  local key="$1"
  # shellcheck disable=SC2016 # $k は jq 側の --arg 変数 (シェル展開ではない)
  gh issue list --repo "${REPO}" --label "${ALERT_LABEL}" --state open --json number,body \
    | jq -r --arg k "infra-alert-key: ${key}" \
      '[.[] | select(.body | contains($k))] | .[0].number // empty'
}

# 引数: key, title, message
sync_breach() {
  local key="$1" title="$2" message="$3" existing
  existing=$(find_open_alert "${key}")
  if [[ -n "${existing}" ]]; then
    log "既存アラート継続中のため再通知はスキップします (Issue #${existing}, key=${key})"
    return
  fi

  ensure_alert_label
  local body
  body=$(printf '## %s\n\n%s\n\n検知時刻 (UTC): %s\n\n解消すると自動でこのIssueをクローズします。\n\n<!-- infra-alert-key: %s -->' \
    "${title}" "${message}" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "${key}")
  local issue_number
  issue_number=$(gh issue create --repo "${REPO}" --title "${title}" --body "${body}" \
    --label "${ALERT_LABEL}" | grep -oE '[0-9]+$')
  notify_discord "$(printf '🚨 **Infra Health Check**: %s\n%s\nIssue: https://github.com/%s/issues/%s' \
    "${title}" "${message}" "${REPO}" "${issue_number}")"
  log "新規アラートを作成しました (Issue #${issue_number}, key=${key})"
}

# 引数: key
sync_recovered() {
  local key="$1" existing
  existing=$(find_open_alert "${key}")
  [[ -n "${existing}" ]] || return
  gh issue close "${existing}" --repo "${REPO}" --comment "状態が正常に戻ったため、このインシデントをクローズします。"
  notify_discord "$(printf '✅ **Infra Health Check**: 復旧を確認しました (key=%s)\nIssue: https://github.com/%s/issues/%s' \
    "${key}" "${REPO}" "${existing}")"
  log "アラートをクローズしました (Issue #${existing}, key=${key})"
}

# ============================================================
# メイン処理
# ============================================================

main() {
  local required_vars=(KUBECONFIG DISCORD_OPS_WEBHOOK_URL GH_TOKEN)
  local var
  for var in "${required_vars[@]}"; do
    [[ -n "${!var:-}" ]] || die "必須環境変数が未設定です: ${var}"
  done

  setup_kubeconfig

  local disk_result disk_state disk_percent
  disk_result=$(check_disk_usage)
  disk_state="${disk_result%%|*}"
  disk_percent="${disk_result##*|}"
  log "ディスク使用率チェック: ${NODE_NAME} = ${disk_percent}% (閾値 ${DISK_THRESHOLD_PERCENT}%, state=${disk_state})"

  case "${disk_state}" in
    breach)
      # shellcheck disable=SC2016 # バッククォートはMarkdown装飾の文字リテラル (展開不要)
      sync_breach "disk-${NODE_NAME}" \
        "ルートディスク使用率が閾値を超えました (${NODE_NAME})" \
        "$(printf '使用率: %s%% (閾値: %s%%)\n\n参照: `.kiro/steering/dr.md` のCNPGの節 (WALアーカイブ失敗によるディスク肥大化の既知パターン)' "${disk_percent}" "${DISK_THRESHOLD_PERCENT}")"
      ;;
    ok)
      sync_recovered "disk-${NODE_NAME}"
      ;;
    *)
      log "ディスク使用率を判定できなかったため今回はスキップします"
      ;;
  esac

  local cluster_key state message
  while IFS='|' read -r cluster_key state message; do
    [[ -n "${cluster_key}" ]] || continue
    case "${state}" in
      breach)
        # shellcheck disable=SC2016 # バッククォートはMarkdown装飾の文字リテラル (展開不要)
        sync_breach "cnpg-wal-${cluster_key}" \
          "CNPG WALアーカイブが失敗しています (${cluster_key})" \
          "$(printf '%s\n\n参照: `.kiro/steering/dr.md` のCNPGの節' "${message}")"
        ;;
      ok)
        sync_recovered "cnpg-wal-${cluster_key}"
        ;;
      *)
        log "CNPG ${cluster_key}: ContinuousArchiving 条件が未設定のためスキップします (バックアップ未設定の可能性)"
        ;;
    esac
  done < <(check_cnpg_archiving)
}

if [[ "${BASH_SOURCE[0]:-$0}" == "${0}" ]]; then
  main "$@"
fi
