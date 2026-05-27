#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Infisical へのアプリシークレット一括登録スクリプト
#
# 使い方:
#   1. infisical login        # ブラウザ認証 (初回のみ)
#   2. cp .env.app-secrets.example .env.app-secrets
#      vi .env.app-secrets    # 実際の値を記入
#   3. ./scripts/push-secrets-to-infisical.sh
#
# 引数 (省略可):
#   $1  読み込む .env ファイルパス  (デフォルト: .env.app-secrets)
#   $2  Infisical 環境名            (デフォルト: prod)
#
# 注意:
#   このスクリプトは開発者がローカルで手動実行するもの (Write 権限が必要)。
#   K8s 内の ESO (ClusterSecretStore) は別途 Machine Identity で Read のみ動作する。
# ============================================================

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${1:-${REPO_ROOT}/.env.app-secrets}"
INFISICAL_ENV="${2:-prod}"

# ── 前提チェック ──────────────────────────────────────────
echo "=== 前提チェック ==="

if ! command -v infisical &>/dev/null; then
  echo "❌ infisical CLI がインストールされていません。"
  echo "   curl -1sLf 'https://dl.cloudsmith.io/public/infisical/infisical-cli/setup.deb.sh' | sudo bash"
  echo "   sudo apt-get install -y infisical"
  exit 1
fi
echo "  ✅ infisical CLI: $(infisical --version 2>&1 | head -1)"

# infisical login 済みか確認
if ! infisical login status --silent 2>/dev/null; then
  echo ""
  echo "❌ Infisical にログインしていません。"
  echo "   infisical login"
  exit 1
fi
echo "  ✅ Infisical ログイン済み"

: "${INFISICAL_PROJECT_ID:?'❌ INFISICAL_PROJECT_ID が未設定です。source .env を実行してください'}"
echo "  ✅ INFISICAL_PROJECT_ID=${INFISICAL_PROJECT_ID}"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo ""
  echo "❌ ${ENV_FILE} が見つかりません。"
  echo "   cp .env.app-secrets.example .env.app-secrets"
  echo "   # 値を埋めてから再実行してください"
  exit 1
fi
echo "  ✅ シークレットファイル: ${ENV_FILE}"

# ── シークレット一括登録 ──────────────────────────────────
echo ""
echo "=== Infisical へ反映中 ==="
echo "   プロジェクト: ${INFISICAL_PROJECT_ID}"
echo "   環境        : ${INFISICAL_ENV}"
echo "   ファイル    : ${ENV_FILE}"
echo ""

infisical secrets set \
  --file "${ENV_FILE}" \
  --env "${INFISICAL_ENV}" \
  --projectId "${INFISICAL_PROJECT_ID}" \
  --silent

# ── 登録内容の確認表示 ────────────────────────────────────
echo "✅ Infisical へのシークレット登録が完了しました"
echo ""
echo "登録済みシークレット一覧 (値は非表示):"
infisical secrets \
  --env "${INFISICAL_ENV}" \
  --projectId "${INFISICAL_PROJECT_ID}" \
  --silent \
  2>/dev/null | awk 'NR>2 && NF {print "  - " $1}' || true

echo ""
echo "確認: https://app.infisical.com"
echo ""
echo "次のステップ:"
echo "  source .env && ansible-playbook -i ansible/inventory/tailscale.yml ansible/playbooks/k3s-bootstrap.yml"
