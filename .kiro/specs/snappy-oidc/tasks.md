# ⚠️ 廃止済み — Snappymail は Roundcube に切り替えたため実装不要

---

# タスク定義 (Tasks) - Snappymail OIDC 設定 (完全自動化)

## タスク一覧

- [x] 1. `snappymail-secrets` が Infisical から正しく同期されていることを確認
  - `SNAPPYMAIL_OAUTH2_CLIENT_ID` と `SNAPPYMAIL_OAUTH2_CLIENT_SECRET` の2キーが存在することを検証済み
  - _Requirements: 1_

- [x] 2. Stalwart の `authentik-oidc` 設定が適用済みで OAUTHBEARER 検証が機能することを確認
  - `issuerUrl` が snappymail 向け (`/application/o/snappymail/`) に設定され、`requireAudience: snappymail-webmail` が有効であることを検証済み
  - _Requirements: 3_

- [ ] 3. Snappymail Kubernetes マニフェストセットを作成する
- [ ] 3.1 deployment.yaml を initContainers + 自動パッチスクリプト構成で新規作成
  - initContainer `install-authentik-plugin` で `login-authentik` プラグインのコピー・`plugin-login-authentik.ini` 自動生成・`domains/aramakisai.com.json` の OAUTHBEARER/XOAUTH2 有効化を実装する
  - メインコンテナのエントリポイントを「`application.ini` 生成の監視スクリプト（バックグラウンド）+ `/docker-entrypoint.sh apache2-foreground`」の二段構成にする
  - 監視スクリプトは `application.ini` 生成を検知次第、`enable`・`enabled_list`・`hide_login_form`・`default_domain`・`admin_password` を `sed` でパッチして書き込む
  - `snappymail-secrets` から `SNAPPYMAIL_OAUTH2_CLIENT_ID` / `SNAPPYMAIL_OAUTH2_CLIENT_SECRET` を `envFrom` で注入する設定を含める
  - `kubectl apply --dry-run=client -f deployment.yaml` が通過すれば完了
  - _Requirements: 2_
  - _Boundary: deployment.yaml_

- [ ] 3.2 (P) external-secret.yaml・pvc.yaml・service.yaml・ArgoCD Application yaml を作成
  - `external-secret.yaml` に `SNAPPYMAIL_OAUTH2_CLIENT_ID`・`SNAPPYMAIL_OAUTH2_CLIENT_SECRET` の2キーを定義し、`snappymail-secrets` として展開されるよう設定する
  - `pvc.yaml` に `snappymail-data` PVC（ReadWriteOnce・1Gi）を定義する
  - `service.yaml` に ClusterIP Service を定義する
  - `gitops/apps/prod/snappymail.yaml` に ArgoCD Application（path: `gitops/manifests/prod/snappymail`）を定義する
  - 各ファイルが `kubectl apply --dry-run=client` で検証通過すれば完了
  - _Requirements: 1, 2_
  - _Boundary: external-secret.yaml, pvc.yaml, service.yaml, ArgoCD app_

- [ ] 4. GitOps 適用と自動セットアップ検証
- [ ] 4.1 マニフェストを Git コミット・プッシュして ArgoCD 経由でクラスターに同期
  - `gitops/manifests/prod/snappymail/` と `gitops/apps/prod/snappymail.yaml` を main ブランチにプッシュする
  - ArgoCD が snappymail Application を Healthy かつ Synced 状態で表示すれば完了
  - _Requirements: 1, 2_

- [ ] 4.2 Pod 起動ログで initContainer と自動パッチスクリプトの動作を確認
  - `kubectl logs -n prod -l app=snappymail -c install-authentik-plugin` で initContainer の正常完了を確認
  - メインコンテナのログに `GitOps configurations successfully applied to application.ini` が出力されれば完了
  - _Requirements: 2_

- [ ] 4.3 PVC 上の設定ファイルが自動生成されていることを検証
  - `kubectl exec -n prod deploy/snappymail -- cat /var/lib/snappymail/_data_/_default_/configs/plugin-login-authentik.ini` で Client ID の設定を確認
  - `kubectl exec -n prod deploy/snappymail -- cat /var/lib/snappymail/_data_/_default_/configs/application.ini` で `hide_login_form = On` と `enabled_list = "login-authentik"` を確認
  - 期待する設定値がすべて確認できれば完了
  - _Requirements: 2_

- [ ] 5. E2E ログインテスト: `https://webmail.aramakisai.com` で手動設定なしに SSO ログインが成功することを確認
  - パスワードフォームが表示されず自動で Authentik にリダイレクトされることを確認
  - Authentik 認証後にメール一覧画面が表示されることを確認
  - Stalwart IMAP が OAUTHBEARER で正常に接続し受信メールが表示されれば完了
  - _Requirements: 2, 3_
  - _Depends: 4.3_
