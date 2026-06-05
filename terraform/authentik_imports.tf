# ============================================================
# authentik_imports.tf — 既存リソースのインポート定義
# ============================================================
# この定義ファイルを使用して、既存の手動設定された Provider/Application を
# 安全に IaC 管理下に取り込みます。

import {
  to = authentik_provider_oauth2.roundcube
  id = "12"
}

import {
  to = authentik_application.roundcube
  id = "roundcube"
}

import {
  to = authentik_provider_oauth2.argocd
  id = "10"
}

import {
  to = authentik_application.argocd
  id = "argocd"
}
