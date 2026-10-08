#!/bin/sh
# generate-config.sh のローカル検証。mikefarah/yq が必要なため、無ければ docker 上の同イメージで自分自身を再実行する。
# 使い方: sh test-generate.sh
set -eu
IMAGE='mikefarah/yq:4'
here=$(cd "$(dirname "$0")" && pwd)

if [ "${1:-}" != "--inner" ]; then
  if yq --version 2>/dev/null | grep -q mikefarah; then
    exec sh "$0" --inner
  fi
  exec docker run --rm --entrypoint sh -v "$here:/portal:ro" "$IMAGE" /portal/test-generate.sh --inner
fi

gen="$here/generate-config.sh"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
fail() { echo "FAIL: $1" >&2; exit 1; }

names() { yq '[.services[].items[].name] | join(",")' "$1"; }
groups() { yq '[.services[].name] | join(",")' "$1"; }

# 内部 URL あり
out="$work/with"; mkdir "$out"
PORTAL_NOTION_URL=https://notion.example/n PORTAL_GOOGLE_DRIVE_URL=https://drive.example/d \
  sh "$gen" "$here" "$out"
[ "$(yq '[.. | select(has("url_env"))] | length' "$out/config.yml")" = 0 ] || fail "url_env が残っている"
[ "$(yq '.services[].items[] | select(.name == "Notion") | .url' "$out/config.yml")" = "https://notion.example/n" ] || fail "Notion の URL"
[ "$(yq '.services[].items[] | select(.name == "Google Drive") | .url' "$out/config.yml")" = "https://drive.example/d" ] || fail "Drive の URL"
[ "$(groups "$out/config.yml")" = "委員会のサービス,情報共有,公式サイト・SNS" ] || fail "通常版のグループ: $(groups "$out/config.yml")"
[ "$(groups "$out/config-admin.yml")" = "委員会のサービス,情報共有,公式サイト・SNS,管理者向け,外部管理画面" ] || fail "admin 版のグループ"
case "$(names "$out/config.yml")" in *運用ダッシュボード*|*Hetzner*) fail "通常版に admin 導線がある" ;; esac
case "$(names "$out/config-admin.yml")" in *運用ダッシュボード*) ;; *) fail "admin 版に導線がない" ;; esac

# 内部 URL なし (未設定と空文字の両方)
out="$work/without"; mkdir "$out"
PORTAL_NOTION_URL='' sh "$gen" "$here" "$out"
for f in config.yml config-admin.yml; do
  case "$(names "$out/$f")" in *Notion* | *Drive*) fail "$f に内部向けリンクが残っている" ;; esac
done
[ "$(groups "$out/config.yml")" = "委員会のサービス,公式サイト・SNS" ] || fail "空グループが残っている: $(groups "$out/config.yml")"
[ "$(yq '[.. | select(has("url_env"))] | length' "$out/config-admin.yml")" = 0 ] || fail "admin 版に url_env が残っている"

# 同じ入力なら同じ出力
out2="$work/again"; mkdir "$out2"
PORTAL_NOTION_URL='' sh "$gen" "$here" "$out2"
cmp "$out/config.yml" "$out2/config.yml" || fail "冪等でない"

echo "OK"
