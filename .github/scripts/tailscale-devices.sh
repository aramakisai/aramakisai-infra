#!/usr/bin/env bash
# Tailscale デバイスの選別・登録判定・旧デバイス削除。recovery.sh から source し、
# Terraform でノードを手動作成する前の掃除には直接実行する。
#
#   tailscale-devices.sh purge <node>...        node と同名系の offline デバイスを削除 (作成前に実行)
#   tailscale-devices.sh check <node> [since]   登録済みなら 0。未登録なら原因を表示して 1
#
# 認証: TAILSCALE_OAUTH_CLIENT_ID / TAILSCALE_OAUTH_CLIENT_SECRET (devices:core 書込) / TAILSCALE_TAILNET
#
# デバイスは非 ephemeral のためサーバー削除後も残り、同名で再作成すると新デバイスは
# .hostname が元の名前のまま .name (MagicDNS 名) だけが `<node>-N` になる。MagicDNS 名 `<node>` は
# 旧 (offline) デバイスに解決され続けるため、登録済みの判定は .hostname でなく .name の先頭ラベルで行う。

TS_API="${TS_API:-https://api.tailscale.com/api/v2}"

ts_token() {
  local response
  response=$(curl -sf -X POST "${TS_API}/oauth/token" \
    -d "client_id=${TAILSCALE_OAUTH_CLIENT_ID}" \
    -d "client_secret=${TAILSCALE_OAUTH_CLIENT_SECRET}") || return 1
  echo "${response}" | jq -r '.access_token // empty'
}

ts_devices() {
  local token="$1"
  curl -sf -H "Authorization: Bearer ${token}" "${TS_API}/tailnet/${TAILSCALE_TAILNET}/devices"
}

# 同名系 (.hostname が `<node>` または `<node>-N`) のデバイス ID。
# 引数: devices JSON, ノード名, 状態 (online|offline|any)
ts_device_ids() {
  local json="$1" node="$2" state="${3:-any}"
  echo "${json}" | jq -r --arg n "${node}" --arg s "${state}" '
    .devices[]
    | select((.hostname // "") | test("^" + $n + "(-[0-9]+)?$"))
    | select($s == "any" or ($s == "online" and .connectedToControl == true)
                         or ($s == "offline" and .connectedToControl != true))
    | .id'
}

# 新ノードの登録確認。次を全て満たすデバイスがあるときだけ 0。
#   - .name の先頭ラベルが <node> と完全一致 (旧デバイスが残ると `<node>-N` になる)
#   - 接続中
#   - since (RFC 3339。サーバー作成時刻) が指定されていれば .created がそれ以降
# 引数: devices JSON, ノード名, [since]
ts_node_registered() {
  echo "$1" | jq -e --arg n "$2" --arg since "${3:-}" '
    def epoch: sub("\\.[0-9]+"; "") | sub("\\+00:00$"; "Z") | fromdateiso8601;
    [.devices[]
      | select(((.name // "") | split(".")[0]) == $n and .connectedToControl == true)
      | select($since == "" or (.created // "" | epoch) >= ($since | epoch))] | length > 0' >/dev/null
}

# 未登録の原因調査用。同名系デバイスを `id hostname name online created` で出す
ts_node_diag() {
  echo "$1" | jq -r --arg re "^$2(-[0-9]+)?\$" '
    .devices[] | select((.hostname // "") | test($re))
    | "\(.id) \(.hostname) \(.name // "-") online=\(.connectedToControl == true) created=\(.created // "-")"'
}

# 同名系の offline デバイスを ID 指定で削除する。online は別の生きたノードの可能性があるため消さない。
ts_purge_stale() {
  local node="$1" token devices id
  token=$(ts_token) || token=""
  [[ -n "${token}" ]] || { echo "Tailscale OAuth token の取得に失敗しました" >&2; return 1; }
  devices=$(ts_devices "${token}") || { echo "Tailscale デバイス一覧の取得に失敗しました" >&2; return 1; }
  for id in $(ts_device_ids "${devices}" "${node}" offline); do
    echo "Tailscale 旧デバイス削除: ${node} ${id}" >&2
    curl -sf -X DELETE -H "Authorization: Bearer ${token}" "${TS_API}/device/${id}" >/dev/null \
      || { echo "デバイス削除に失敗しました (${id})。OAuth クライアントに devices:core の書込スコープが必要です" >&2; return 1; }
  done
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  set -euo pipefail
  case "${1:-}" in
    purge)
      shift
      [[ $# -gt 0 ]] || { echo "usage: $0 purge <node>..." >&2; exit 2; }
      for node in "$@"; do ts_purge_stale "${node}"; done
      ;;
    check)
      devices=$(ts_devices "$(ts_token)")
      if ts_node_registered "${devices}" "${2:?node}" "${3:-}"; then echo "registered"; else
        echo "not registered" >&2
        ts_node_diag "${devices}" "${2}" >&2
        exit 1
      fi
      ;;
    *) echo "usage: $0 purge <node>... | check <node> [since]" >&2; exit 2 ;;
  esac
fi
