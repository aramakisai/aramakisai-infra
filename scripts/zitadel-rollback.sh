#!/usr/bin/env bash
# Zitadel切替後に重大な認証障害が発生した場合の、旧authentik構成への切り戻しを
# 機械的に行うためのスクリプト。手順の全体像はdocs/zitadel-rollback-runbook.md参照。
#
# GitOps原則(CLAUDE.md)により、このスクリプトはkubectl/argocd/terraformを
# 一切実行しない。git revertとInfisicalシークレットの復元のみを行い、
# ArgoCD syncはランブックの手順に従って人間が実行する。
#
# 使い方:
#   scripts/zitadel-rollback.sh find-commits
#   scripts/zitadel-rollback.sh revert <commit-ish>
#   infisical run --env=prod -- scripts/zitadel-rollback.sh backup-secrets prod [出力先ディレクトリ]
#   infisical run --env=prod -- scripts/zitadel-rollback.sh restore-secrets prod [入力元ディレクトリ]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_SECRETS_DIR="${SCRIPT_DIR}/.zitadel-rollback-secrets-backup"

# カットオーバー(task9.4)が実際に書き換えたgitops manifestパス。
# revertサブコマンドはこの配列を「対象コミットが本当にカットオーバーの
# コミットか」を機械的に判定するガードとして使う(無関係なコミットSHAを
# 誤ってrevertしてしまう事故を防ぐ)。
CUTOVER_PATHS=(
  "gitops/manifests/prod/mailserver/"
  "gitops/manifests/prod/cms/"
  "gitops/manifests/prod/cms-secrets/"
  "gitops/manifests/prod/vaultwarden/"
  "gitops/manifests/prod/roundcube/"
)

# authentik時代と同じキー名のままZitadelのclient_id/secretで上書きされる
# Infisical prod既存キー (ansible/roles/zitadel-bootstrap/vars/resources.yml の
# infisical_hint "既存キー更新" 記載分と対応)。新設キー
# (CMS_PROD_OIDC_CLIENT_ID/ROUNDCUBE_OIDC_CLIENT_ID等)は切り戻し後の
# authentik構成マニフェストから参照されなくなるだけなので対象外。
REUSED_SECRET_KEYS=(
  CMS_PROD_OIDC_CLIENT_SECRET
  VAULTWARDEN_OIDC_CLIENT_ID
  VAULTWARDEN_OIDC_CLIENT_SECRET
  MAIL_OAUTH2_CLIENT_SECRET
)

usage() {
  cat <<'EOF'
使い方:
  zitadel-rollback.sh find-commits
  zitadel-rollback.sh revert <commit-ish>
  zitadel-rollback.sh backup-secrets <infisical env> [出力先ディレクトリ]
  zitadel-rollback.sh restore-secrets <infisical env> [入力元ディレクトリ]

詳細: docs/zitadel-rollback-runbook.md
EOF
}

cmd_find_commits() {
  git -C "${SCRIPT_DIR}" log --oneline -- "${CUTOVER_PATHS[@]}"
}

# 指定コミットがCUTOVER_PATHSのいずれかを実際に変更していればtrue。
commit_touches_cutover_paths() {
  local commit="$1"
  local changed
  changed="$(git -C "${SCRIPT_DIR}" show --name-only --format= "${commit}" -- "${CUTOVER_PATHS[@]}")"
  [[ -n "${changed}" ]]
}

cmd_revert() {
  local commit="${1:?commit-ishを指定すること}"

  if ! git -C "${SCRIPT_DIR}" rev-parse --verify --quiet "${commit}^{commit}" >/dev/null; then
    echo "エラー: '${commit}' は存在するコミットとして解決できない" >&2
    return 1
  fi

  if ! commit_touches_cutover_paths "${commit}"; then
    echo "エラー: ${commit} はカットオーバー対象パス(${CUTOVER_PATHS[*]})を含んでいない。" >&2
    echo "        find-commitsで正しいコミットSHAを確認すること。" >&2
    return 1
  fi

  git -C "${SCRIPT_DIR}" revert --no-commit "${commit}"

  cat <<EOF

--no-commitでワーキングツリーに反映した(まだコミットしていない)。
次の手順:
  1. git diff --cached で authentik構成に戻っていることを目視確認する
  2. git commit / git push / PR作成・レビュー・マージ
  3. ArgoCD Application (mailserver cms vaultwarden roundcube) をsyncする
  4. restore-secrets で以下のInfisicalキーを復元する: ${REUSED_SECRET_KEYS[*]}
EOF
}

# 値をターミナルに一切出さない(ファイルへ直接リダイレクトのみ)。
cmd_backup_secrets() {
  local env="${1:?infisical環境名(例: prod)を指定すること}"
  local outdir="${2:-${DEFAULT_SECRETS_DIR}}"

  mkdir -p "${outdir}"
  chmod 700 "${outdir}"

  local key
  for key in "${REUSED_SECRET_KEYS[@]}"; do
    infisical secrets get "${key}" --env="${env}" --plain --silent \
      >"${outdir}/${key}.value" 2>/dev/null
    chmod 600 "${outdir}/${key}.value"
  done

  echo "退避完了: ${outdir} (${#REUSED_SECRET_KEYS[@]}件、値はターミナルに出力していない)"
}

# infisical secrets set の "KEY=@/path/to/file" 構文でファイルから直接読ませ、
# シェル引数やプロセスリストに値そのものが現れないようにする。
cmd_restore_secrets() {
  local env="${1:?infisical環境名(例: prod)を指定すること}"
  local indir="${2:-${DEFAULT_SECRETS_DIR}}"

  local key f
  for key in "${REUSED_SECRET_KEYS[@]}"; do
    f="${indir}/${key}.value"
    if [[ ! -f "${f}" ]]; then
      echo "エラー: ${f} が見つからない (先にbackup-secretsを実行したか確認すること)" >&2
      return 1
    fi
  done

  for key in "${REUSED_SECRET_KEYS[@]}"; do
    f="${indir}/${key}.value"
    infisical secrets set "${key}=@${f}" --env="${env}" >/dev/null
  done

  echo "復元完了: ${env}環境の${REUSED_SECRET_KEYS[*]}"
}

main() {
  local sub="${1:-}"
  case "${sub}" in
    find-commits)
      cmd_find_commits
      ;;
    revert)
      shift
      cmd_revert "$@"
      ;;
    backup-secrets)
      shift
      cmd_backup_secrets "$@"
      ;;
    restore-secrets)
      shift
      cmd_restore_secrets "$@"
      ;;
    *)
      usage
      exit 1
      ;;
  esac
}

if [[ "${BASH_SOURCE[0]:-$0}" == "${0}" ]]; then
  main "$@"
fi
