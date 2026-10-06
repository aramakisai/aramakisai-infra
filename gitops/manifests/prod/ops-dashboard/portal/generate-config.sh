#!/bin/sh
# 使い方: generate-config.sh <links.yaml のあるディレクトリ> <出力ディレクトリ>
# readOnlyRootFilesystem で動かすため yq -i (一時ファイルを /tmp に作る) は使わない。
set -eu
src=$1
out=$2

cfg=$(cat "$src/links.yaml")

for name in $(printf '%s\n' "$cfg" | yq '.services[].items[].url_env | select(. != null)'); do
  value=$(printenv "$name" || true)
  if [ -n "$value" ]; then
    cfg=$(printf '%s\n' "$cfg" | NAME=$name VALUE=$value yq '(.services[].items[] | select(.url_env == strenv(NAME))) |= (.url = strenv(VALUE) | del(.url_env))')
  else
    cfg=$(printf '%s\n' "$cfg" | NAME=$name yq 'del(.services[].items[] | select(.url_env == strenv(NAME)))')
  fi
done
cfg=$(printf '%s\n' "$cfg" | yq 'del(.services[] | select((.items | length) == 0))')

printf '%s\n' "$cfg" > "$out/config.yml"
printf '%s\n' "$cfg" | OVERLAY="$src/admin-overlay.yaml" yq '.services += load(strenv(OVERLAY)).services' > "$out/config-admin.yml"
