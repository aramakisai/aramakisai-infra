#!/usr/bin/env bash
# scripts/zitadel-rollback.sh のロジックを、実際のgit履歴・実際のInfisicalに
# 触れずに検証するユニットテスト。
#
# - git revertガード: 使い捨てのtmp gitリポジトリを作って検証する(実リポジトリは触らない)
# - Infisicalシークレット退避・復元: PATH上にfake infisicalコマンドを置いて検証する
#   (ネットワークアクセスなし、実際のシークレット値は一切扱わない)
#
# 使い方:
#   ./scripts/test-zitadel-rollback.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck source=/dev/null
source "${SCRIPT_DIR}/scripts/zitadel-rollback.sh"

PASS=0
FAIL=0

assert_eq() {
  local desc="$1" expected="$2" actual="$3"
  if [[ "${expected}" == "${actual}" ]]; then
    echo "  ✅ ${desc}"
    PASS=$((PASS + 1))
  else
    echo "  ❌ ${desc} (expected=${expected}, actual=${actual})"
    FAIL=$((FAIL + 1))
  fi
}

assert_true() {
  local desc="$1"
  if "${@:2}"; then
    echo "  ✅ ${desc}"
    PASS=$((PASS + 1))
  else
    echo "  ❌ ${desc}"
    FAIL=$((FAIL + 1))
  fi
}

assert_false() {
  local desc="$1"
  if ! "${@:2}"; then
    echo "  ✅ ${desc}"
    PASS=$((PASS + 1))
  else
    echo "  ❌ ${desc}"
    FAIL=$((FAIL + 1))
  fi
}

# assert_true/assert_falseへ渡すコマンドの標準出力・標準エラーだけを黙らせ、
# assert自体の✅/❌ログはterminalに残すためのヘルパー。
quiet() { "$@" >/dev/null 2>&1; }

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "${TMP_ROOT}"' EXIT

echo "=== commit_touches_cutover_paths / revert ガード ユニットテスト ==="

TMP_REPO="${TMP_ROOT}/repo"
mkdir -p "${TMP_REPO}/gitops/manifests/prod/cms" "${TMP_REPO}/unrelated"
git -C "${TMP_REPO}" init -q
git -C "${TMP_REPO}" config user.email test@example.invalid
git -C "${TMP_REPO}" config user.name test

echo "authentik: cms-prod" >"${TMP_REPO}/gitops/manifests/prod/cms/deployment.yaml"
echo "base" >"${TMP_REPO}/unrelated/file.txt"
git -C "${TMP_REPO}" add -A
git -C "${TMP_REPO}" commit -q -m "base"

echo "unrelated change" >"${TMP_REPO}/unrelated/file.txt"
git -C "${TMP_REPO}" commit -q -am "unrelated commit"
UNRELATED_SHA="$(git -C "${TMP_REPO}" rev-parse HEAD)"

echo "zitadel: (issuer only)" >"${TMP_REPO}/gitops/manifests/prod/cms/deployment.yaml"
git -C "${TMP_REPO}" commit -q -am "cutover commit"
CUTOVER_SHA="$(git -C "${TMP_REPO}" rev-parse HEAD)"

# 以降、実リポジトリではなくtmpリポジトリに対してrelativeのgit操作を行わせる。
# (sourceしたzitadel-rollback.sh内の関数がこれらのグローバル変数を参照するため、
# 静的解析からは未使用に見える)
SCRIPT_DIR="${TMP_REPO}"
# shellcheck disable=SC2034
CUTOVER_PATHS=("gitops/manifests/prod/cms/")

assert_false "無関係コミットはカットオーバーパスを含まない" \
  commit_touches_cutover_paths "${UNRELATED_SHA}"
assert_true "カットオーバーコミットはカットオーバーパスを含む" \
  commit_touches_cutover_paths "${CUTOVER_SHA}"

assert_false "無関係コミットのrevertは拒否される" \
  quiet cmd_revert "${UNRELATED_SHA}"
assert_eq "拒否された場合ワーキングツリーは変更されない" \
  "" "$(git -C "${TMP_REPO}" status --porcelain)"

assert_true "カットオーバーコミットのrevertは受理される" \
  quiet cmd_revert "${CUTOVER_SHA}"
assert_eq "revert後、内容がauthentik構成に戻っている" \
  "authentik: cms-prod" "$(cat "${TMP_REPO}/gitops/manifests/prod/cms/deployment.yaml")"
git -C "${TMP_REPO}" reset -q --hard "${CUTOVER_SHA}"

echo ""
echo "=== backup-secrets / restore-secrets ユニットテスト (fake infisical) ==="

FAKE_BIN="${TMP_ROOT}/bin"
mkdir -p "${FAKE_BIN}"
export FAKE_INFISICAL_SET_LOG="${TMP_ROOT}/set.log"
: >"${FAKE_INFISICAL_SET_LOG}"

cat >"${FAKE_BIN}/infisical" <<'FAKE'
#!/usr/bin/env bash
set -euo pipefail
if [[ "$1 $2" == "secrets get" ]]; then
  key="$3"
  echo "fake-value-for-${key}"
elif [[ "$1 $2" == "secrets set" ]]; then
  arg="$3"
  name="${arg%%=@*}"
  file="${arg#*=@}"
  echo "${name}=$(cat "${file}")" >>"${FAKE_INFISICAL_SET_LOG}"
else
  echo "unexpected fake infisical invocation: $*" >&2
  exit 1
fi
FAKE
chmod +x "${FAKE_BIN}/infisical"
export PATH="${FAKE_BIN}:${PATH}"

# shellcheck disable=SC2034
REUSED_SECRET_KEYS=(FAKE_KEY_A FAKE_KEY_B)
SECRETS_DIR="${TMP_ROOT}/secrets"

BACKUP_STDOUT="$(cmd_backup_secrets dev "${SECRETS_DIR}" 2>&1)"

assert_eq "FAKE_KEY_Aの退避内容が正しい" \
  "fake-value-for-FAKE_KEY_A" "$(cat "${SECRETS_DIR}/FAKE_KEY_A.value")"
assert_eq "FAKE_KEY_Bの退避内容が正しい" \
  "fake-value-for-FAKE_KEY_B" "$(cat "${SECRETS_DIR}/FAKE_KEY_B.value")"
assert_eq "退避ファイルのパーミッションが600" \
  "600" "$(stat -c '%a' "${SECRETS_DIR}/FAKE_KEY_A.value")"
assert_false "backup-secretsの標準出力にシークレット値そのものが含まれない" \
  grep -q "fake-value-for-FAKE_KEY_A" <<<"${BACKUP_STDOUT}"

quiet cmd_restore_secrets dev "${SECRETS_DIR}"
assert_eq "restore-secretsがfake infisical secrets setへ正しいKEY=値で渡している" \
  "FAKE_KEY_A=fake-value-for-FAKE_KEY_A
FAKE_KEY_B=fake-value-for-FAKE_KEY_B" \
  "$(cat "${FAKE_INFISICAL_SET_LOG}")"

assert_false "退避ファイルが無い場合restore-secretsは失敗する" \
  quiet cmd_restore_secrets dev "${TMP_ROOT}/no-such-dir"

echo ""
echo "${PASS} passed, ${FAIL} failed"
[[ "${FAIL}" -eq 0 ]]
