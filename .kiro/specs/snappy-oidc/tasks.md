# タスク定義 (Tasks) - Snappymail OIDC 設定 (完全自動化)

## タスク一覧

### Task 1: Kubernetes 側シークレットとマニフェスト同期の検証
- [x] 1.1. `snappymail-secrets` Secret が存在し、OIDC用Client ID/SecretがInfisicalから同期されていることを確認。

### Task 2: Stalwart 側設定適用の検証
- [x] 2.1. Stalwart の設定プランにおいて `authentik-oidc` が正常に適用され、アクティブになっていることを検証する。

### Task 3: Snappymail 自動セットアップマニフェストの適用と検証
- [ ] 3.1. [deployment.yaml](gitops/manifests/prod/snappymail/deployment.yaml) の自動化設定を適用（Gitコミット & プッシュ、または手動テスト適用）。
- [ ] 3.2. 新しい Snappymail Pod の起動ログにて、自動パッチスクリプトが動作したことを確認。
  - ログ確認コマンド: `kubectl logs -n prod deploy/snappymail`
- [ ] 3.3. PVC上の設定ファイル群が自動で期待通りに生成・更新されているか検証。
  - 検証コマンド: `kubectl exec -n prod deploy/snappymail -- cat /var/lib/snappymail/_data_/_default_/configs/plugin-login-authentik.ini`

### Task 4: 最終疎通確認 (ログインテスト)
- [ ] 4.1. `https://webmail.aramakisai.com` にアクセスし、手動操作を一切行うことなく、自動で Authentik にリダイレクトされ、SSOログインが成功することを確認する。
