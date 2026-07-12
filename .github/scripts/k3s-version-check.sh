#!/usr/bin/env bash
# K3s バージョンアップデート検知スクリプト
#
# 現在ピンされているK3sバージョンと、公式のstableチャンネル最新バージョンを比較し、
# アップデートがあればDiscordへ通知する。
# ansible/inventory や クラスタへの書き込みは一切行わない（読み取り専用）。
#
# 単体テスト: ./scripts/test-k3s-version-check-logic.sh
#   (このファイルを source し、classify_version_diff のみをテストする)

set -uo pipefail

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') [k3s-version-check] $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# ============================================================
# 純粋関数 (ネットワークアクセスなしでテスト可能)
# ============================================================

# 引数: <current> <latest> (例: v1.32.3+k3s1 v1.32.4+k3s1)
# 出力: none | patch | minor
# major差分もマイナーバージョンアップ以上の非互換リスクがあるため "minor" として扱う
classify_version_diff() {
  local current="$1" latest="$2"

  if [[ "${current}" == "${latest}" ]]; then
    echo "none"
    return
  fi

  local c_major c_minor c_patch c_build
  local l_major l_minor l_patch l_build

  if [[ "${current}" =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)\+k3s([0-9]+)$ ]]; then
    c_major="${BASH_REMATCH[1]}"
    c_minor="${BASH_REMATCH[2]}"
    c_patch="${BASH_REMATCH[3]}"
    c_build="${BASH_REMATCH[4]}"
  else
    echo "minor"
    return
  fi

  if [[ "${latest}" =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)\+k3s([0-9]+)$ ]]; then
    l_major="${BASH_REMATCH[1]}"
    l_minor="${BASH_REMATCH[2]}"
    l_patch="${BASH_REMATCH[3]}"
    l_build="${BASH_REMATCH[4]}"
  else
    echo "minor"
    return
  fi

  if [[ "${c_major}" != "${l_major}" ]] || [[ "${c_minor}" != "${l_minor}" ]]; then
    echo "minor"
  elif [[ "${c_patch}" != "${l_patch}" ]] || [[ "${c_build}" != "${l_build}" ]]; then
    echo "patch"
  else
    echo "none"
  fi
}

# ============================================================
# データ取得・通知 (外部副作用あり)
# ============================================================

get_current_version() {
  grep 'k3s_version:' ansible/inventory/tailscale.yml | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+\+k3s[0-9]+' || true
}

get_latest_version() {
  curl -sSf "https://update.k3s.io/v1-release/channels" | jq -r '.data[] | select(.id == "stable") | .latest' || true
}

notify_discord() {
  local message="$1"
  curl -sf -X POST \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg content "${message}" '{content: $content}')" \
    "${DISCORD_OPS_WEBHOOK_URL}" >/dev/null \
    || log "警告: Discord 通知に失敗しました"
}

# ============================================================
# メイン処理
# ============================================================

main() {
  if [[ -z "${DISCORD_OPS_WEBHOOK_URL:-}" ]]; then
    die "必須環境変数が未設定です: DISCORD_OPS_WEBHOOK_URL"
  fi

  local current latest diff message

  current=$(get_current_version)
  if [[ -z "${current}" ]]; then
    die "ansible/inventory/tailscale.yml から k3s_version を取得できませんでした"
  fi

  latest=$(get_latest_version)
  if [[ -z "${latest}" ]]; then
    die "update.k3s.io から最新バージョンを取得できませんでした"
  fi

  diff=$(classify_version_diff "${current}" "${latest}")
  log "Current: ${current}, Latest: ${latest}, Diff: ${diff}"

  if [[ "${diff}" == "none" ]]; then
    log "アップデートはありません。"
    exit 0
  fi

  if [[ "${diff}" == "minor" ]]; then
    # shellcheck disable=SC2016 # バッククォートはDiscord Markdown表記であり、シェル変数展開ではない
    message=$(printf '🚀 **K3s Update Available (Minor/Major)**\n現在: `%s`\n最新: `%s`\n⚠️ **マイナー(またはメジャー)バージョンアップです。非互換な変更が含まれる可能性があるため、リリースノートを必ず確認してください。**' "${current}" "${latest}")
  else
    # shellcheck disable=SC2016 # バッククォートはDiscord Markdown表記であり、シェル変数展開ではない
    message=$(printf '🚀 **K3s Update Available (Patch)**\n現在: `%s`\n最新: `%s`\n✅ パッチ/ビルド番号アップデートです。通常、後方互換性は維持されています。' "${current}" "${latest}")
  fi

  notify_discord "${message}"
}

if [[ "${BASH_SOURCE[0]:-$0}" == "${0}" ]]; then
  main "$@"
fi
