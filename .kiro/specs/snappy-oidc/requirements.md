# 要件定義 (Requirements) - Snappymail OIDC 設定

## 1. 目的
Snappymail（webmail.aramakisai.com）において、Authentikを用いたOIDC (OAuth2) 認証を有効化し、パスワード認証を廃止してSSO（シングルサインオン）を可能にする。

## ⚠️ このスペックは廃止済み

Snappymail は採用されず、**Roundcube** に切り替えた。Webmail の OIDC 設定は `gitops/manifests/prod/roundcube/` で管理済み。以下は当時の記録として残す。

## 2. 現状と前提条件 (廃止時点)
- **Authentik側**: Snappymail用のOAuth2 ProviderおよびApplicationの設定は完了していた。
- **Infisical側**: クライアントID (`SNAPPYMAIL_OAUTH2_CLIENT_ID`) およびクライアントシークレット (`SNAPPYMAIL_OAUTH2_CLIENT_SECRET`) は登録済み（不要になったため削除可）。
- **インフラマニフェスト側**: `gitops/manifests/prod/snappymail/` は結局作成されなかった。
- **Stalwart OIDC**: Roundcube 用の `authentik-oidc` 設定に切り替え済み（issuerUrl: roundcube / requireAudience: roundcube）。

## 3. 要件
1. **マニフェスト作成とシークレット同期**:
   - `gitops/manifests/prod/snappymail/external-secret.yaml` を新規作成し、ArgoCD経由でクラスターに同期させる。
   - Kubernetes Secret `snappymail-secrets` に `SNAPPYMAIL_OAUTH2_CLIENT_ID` と `SNAPPYMAIL_OAUTH2_CLIENT_SECRET` が展開されること。
2. **Snappymail 自動セットアップ**:
   - `initContainers` + バックグラウンド監視スクリプトにより、PVCが空の状態から手動操作なしで `login-authentik` プラグインが有効化されること。
   - ログインフォームでのパスワード認証が非表示になり、Authentikへ自動リダイレクトされること。
3. **StalwartのOIDC設定の確認**:
   - Stalwartの `settings-configmap.yaml` 内の `authentik-oidc` ディレクトリ設定が適用されており、Snappymailからの `OAUTHBEARER` ログインを正常に検証できること。

## 4. スコープ外
- Authentik 側でのユーザーおよびグループのプロビジョニング。
- インフラ全体のネットワーク・DNS再構築。
