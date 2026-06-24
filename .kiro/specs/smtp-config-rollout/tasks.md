# Implementation Plan

- [x] 1. (P) Vaultwarden に Authentik と同条件のSMTP設定を追加する
  - `gitops/manifests/prod/vaultwarden/external-secret.yaml` のコメントアウトされた `SMTP_PASSWORD` エントリを有効化し、`remoteRef.key` を（未使用の）`VAULTWARDEN_SMTP_PASSWORD` から `NOREPLY_SMTP_PASSWORD` に変更する
  - `gitops/manifests/prod/vaultwarden/deployment.yaml` に `SMTP_HOST=mail.aramakisai.com`・`SMTP_PORT=587`・`SMTP_SECURITY=starttls`・`SMTP_USERNAME=noreply`・`SMTP_FROM=noreply@aramakisai.com`（plain env）と `SMTP_PASSWORD`（secretKeyRef）を追加する <!-- confidential:allow -->
  - Observable: `make kubectl ARGS="get secret vaultwarden-secrets -n prod -o jsonpath={.data.SMTP_PASSWORD}"` でキーの存在を確認でき、`make kubectl ARGS="get deploy vaultwarden -n prod -o jsonpath={.spec.template.spec.containers[0].env}"` に `SMTP_*` 環境変数が反映されている
  - _Requirements: 1.1, 1.2, 1.3, 1.4_
  - _Boundary: Vaultwarden Config_

- [x] 2. (P) Directus に Authentik と同条件のSMTP設定を追加する
  - `gitops/manifests/prod/directus/external-secret.yaml` に `EMAIL_SMTP_PASSWORD`（`remoteRef.key: NOREPLY_SMTP_PASSWORD`）を追加する
  - `gitops/manifests/prod/directus/deployment.yaml` に `EMAIL_TRANSPORT=smtp`・`EMAIL_FROM=noreply@aramakisai.com`・`EMAIL_SMTP_HOST=mail.aramakisai.com`・`EMAIL_SMTP_PORT=587`・`EMAIL_SMTP_USER=noreply`（plain env）と `EMAIL_SMTP_PASSWORD`（secretKeyRef）を追加する。`EMAIL_SMTP_SECURE`・`EMAIL_SMTP_IGNORE_TLS` は設定しない（research.md Design Decision参照、587番STARTTLSのため） <!-- confidential:allow -->
  - `metadata.annotations` に `secret.reloader.stakater.com/reload: "directus-secrets"` を追加する（既存4サービスとのReloaderパターン整合）
  - Observable: `make kubectl ARGS="get secret directus-secrets -n prod -o jsonpath={.data.EMAIL_SMTP_PASSWORD}"` でキーの存在を確認でき、deploymentの環境変数とアノテーションに反映されている
  - _Requirements: 2.1, 2.2, 2.3, 2.4_
  - _Boundary: Directus Config_

- [x] 3. (P) steering/tech.md のシークレット一覧をSMTP設定の実態に同期する
  - Authentikのシークレットセクションを新設し `AUTHENTIK_SECRET_KEY`・`DB_PASSWORD`・`NOREPLY_SMTP_PASSWORD` を記載する（既存だが未記載だった抜けの補完）
  - Vaultwardenの記載から未使用の `VAULTWARDEN_SMTP_PASSWORD` を削除し、SMTPは `NOREPLY_SMTP_PASSWORD` を再利用する旨を注記する
  - Directusのセクションに `EMAIL_SMTP_PASSWORD`（`NOREPLY_SMTP_PASSWORD` 再利用）を追記する
  - Observable: `git diff .kiro/steering/tech.md` で上記3点の差分が確認できる
  - _Requirements: 3.1_
  - _Boundary: .kiro/steering/tech.md_

- [ ] 4. ArgoCD反映後のメール送信動作確認
- [ ] 4.1 (P) Vaultwardenのメール送信動作確認
  - ArgoCD syncにより `vaultwarden` Podが再起動し、新規環境変数が反映されていることを確認する
  - Vaultwarden管理画面（/admin）のテストメール送信機能を実行し、送信成功を確認する
  - 新規ユーザーをOrganizationに招待し、招待メールが `noreply@aramakisai.com` から到達することを確認する <!-- confidential:allow -->
  - 送信失敗時はmailserverのPostfixログで `SPOOF_PROTECTION` 関連の拒否有無を確認する
  - Observable: テストメール送信・招待メール送信の双方が成功し、受信ボックスで内容を確認できる
  - _Requirements: 1.5, 1.6, 3.2, 3.4_
  - _Depends: 1_
  - _Boundary: Vaultwarden Config_

- [ ] 4.2 (P) Directusのメール送信動作確認
  - ArgoCD syncにより `directus` Podが再起動し、新規環境変数が反映されていることを確認する
  - Directus管理画面からパスワードリセットを申請し、リセットメールの到達を確認する
  - 新規ユーザーを招待し、招待メールの到達を確認する
  - 送信失敗時はmailserverのPostfixログで `SPOOF_PROTECTION` 関連の拒否有無を確認する
  - Observable: パスワードリセットメール・招待メールの双方が受信ボックスで確認できる
  - _Requirements: 2.5, 2.6, 3.3, 3.4_
  - _Depends: 2_
  - _Boundary: Directus Config_
