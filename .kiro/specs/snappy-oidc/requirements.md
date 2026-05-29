# 要件定義 (Requirements) - Snappymail OIDC 設定

## 1. 目的
Snappymail（webmail.aramakisai.com）において、Authentikを用いたOIDC (OAuth2) 認証を有効化し、パスワード認証を廃止してSSO（シングルサインオン）を可能にする。

## 2. 現状と前提条件
- **Authentik側**: Snappymail用のOAuth2 ProviderおよびApplicationの設定は完了している（リダイレクトURI: `https://webmail.aramakisai.com?LoginAuthentik`）。
- **Infisical側**: クライアントID (`SNAPPYMAIL_OAUTH2_CLIENT_ID`) およびクライアントシークレット (`SNAPPYMAIL_OAUTH2_CLIENT_SECRET`) は登録済み。
- **インフラマニフェスト側**: Snappymailの `login-authentik` プラグイン定義、およびStalwartの `authentik-oidc` 設定プランは記述済み。

## 3. 要件
1. **マニフェストとシークレット同期の確認**:
   - `gitops/manifests/prod/snappymail/external-secret.yaml` がArgoCD経由で正常にクラスターに同期され、Kubernetes Secret `snappymail-secrets` に `SNAPPYMAIL_OAUTH2_CLIENT_ID` と `SNAPPYMAIL_OAUTH2_CLIENT_SECRET` が展開されることを確認する。
2. **Snappymail側のプラグイン設定**:
   - Admin UI（`http://localhost:8888/?admin`）にて、`login-authentik` プラグインを有効化し、環境変数から読み取った（もしくは手動で）Client ID / Client Secretを設定する。
   - ログインフォームでのパスワード認証を無効化（非表示）にし、OIDCログイン画面（Authentik）へリダイレクトされるようにする。
3. **StalwartのOIDC設定の確認**:
   - Stalwartの `settings-configmap.yaml` 内の `authentik-oidc` ディレクトリ設定が適用されており、Snappymailからの `OAUTHBEARER` ログインを正常に検証できることを確認する。

## 4. スコープ外
- Authentik 側でのユーザーおよびグループのプロビジョニング。
- インフラ全体のネットワーク・DNS再構築。
