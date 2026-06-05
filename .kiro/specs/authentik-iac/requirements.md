# 要件定義 (Requirements) - Authentik IaC化

## 1. 目的
Kubernetesクラスター上で稼働しているAuthentik（IDプロバイダー）の各種設定を手動管理からTerraformによるIaC（Infrastructure as Code）へと移行し、再現性の向上および変更管理の自動化を実現する。
本スペックでは、一度設定したら頻繁に変更されない「第1段階（アプリ連携・LDAP）」および「第2段階（Discord連携・リカバリー）」にスコープを絞ってIaC化を行う。

## 2. 現状と前提条件
- **Authentikのデプロイ**: Kubernetes上のデプロイ（Helm/マニフェスト）はArgoCDで自動管理されている（`gitops/apps/prod/authentik.yaml` 等）。
- **Authentikのホスト名**: `https://idp.aramakisai.com` で稼働している。
- **メールサーバーの移行**: StalwartからDocker Mailserver (DMS) への移行が進んでいる。DMSはAuthentikのLDAP Outpostと連携してユーザー認証・アカウントプロビジョニングを行っている。
- **機密情報の管理**: 各クライアントシークレットや接続用トークンなどは、HCP Terraformの環境変数、もしくはInfisicalで管理し、Kubernetesクラスター側はExternalSecretで受け取っている。
- **安全第一 of 対応**: ユーザーのセキュリティ懸念に配慮し、本番・検証環境のサーバーに対する不要なコマンド実行（`kubectl`など）は行わず、リポジトリ内のソースコード上の定義変更によって設定変更を行う。

## 3. 要件

### 3.1. TerraformにおけるAuthentik Providerの導入
- `goauthentik/authentik` プロバイダーを `terraform/providers.tf` に追加する。
- 接続先URL (`https://idp.aramakisai.com`) および接続に必要な認証トークン（AuthentikのAPIトークン）を環境変数（`AUTHENTIK_TOKEN` 等）経由で安全に渡せるようにする。

### 3.2. 認証連携リソースのIaC化
以下の各種アプリケーションとの認証連携のための `Provider` および `Application` をTerraformで定義する。

#### ① Cloudflare Access 連携 (新規作成)
- **OIDC Provider**: 
  - Slug: `cloudflare`
  - Client ID: （HCP Terraformの `var.authentik_cf_client_id`）
  - Client Secret: （HCP Terraformの `var.authentik_cf_client_secret`）
  - Redirect URIs: Cloudflare Accessの仕様に準ずる
- **Application**:
  - Name: `Cloudflare Access`
  - Slug: `cloudflare`
  - Provider: 上記のOIDC Provider

#### ② ArgoCD 連携 (既存からインポート)
- **OIDC Provider**:
  - Name: `ArgoCD`
  - Slug: `argocd`
  - Client ID: `argocd`
  - Redirect URIs: `https://argocd.aramakisai.com/auth/callback`
  - Scopes: `openid`, `profile`, `email`, `groups`
- **Application**:
  - Name: `ArgoCD`
  - Slug: `argocd`
  - Provider: 上記のOIDC Provider

#### ③ Roundcube (Webmail) 連携 (既存からインポート)
- **OIDC Provider**:
  - Name: `Roundcube`
  - Slug: `roundcube` または `aramakisai-mail`
  - Client ID: `aramakisai-mail`
  - Redirect URIs: `https://webmail.aramakisai.com/index.php/login/oauth`
  - Scopes: `openid`, `profile`, `email`, `offline_access`
- **Application**:
  - Name: `Roundcube`
  - Slug: `roundcube`
  - Provider: 上記のOIDC Provider

### 3.3. Docker Mailserver (DMS) / LDAP 連携のIaC化 (新規作成)
- **LDAP Provider**:
  - Name: `DMS LDAP`
  - Slug: `dms-ldap`
- **Application**:
  - Name: `DMS LDAP`
  - Slug: `dms-ldap`
  - Provider: 上記のLDAP Provider
- **LDAP Outpost**:
  - Name: `dms-ldap-outpost`
  - Type: `ldap`
  - アサインするApplication: `DMS LDAP`
  - Outpostのサービストークンを管理できるようにする。

### 3.4. 外部IDP（Discord）連携ソースおよびポリシーのIaC化 (新規作成)
Discordを用いたソーシャルログイン設定をIaC化する。
- **ソース連携設定 (`authentik_source_oauth`)**:
  - Discord用のOAuth2連携ソースを定義し、DiscordのDeveloper Portalで取得する「Client ID」「Client Secret」を安全に渡せるようにする。
- **Discord Property Mapping**:
  - DiscordのAPI（`https://discord.com/api/users/@me`等）から取得できるユーザー情報（ユーザー名、メールアドレス、アバターなど）を、Authentik側のユーザープロパティ（`username`, `email`, `attributes`等）に適切にマッピングするProperty MappingをTerraformで定義する。
- **Membership Policy**:
  - Discordでログインしたユーザーを、特定のグループ（例: `discord-users`）に自動で所属させるポリシー、またはフロー設定を定義する。

### 3.5. パスワードリカバリーフローのIaC化 (新規作成)
- メール認証によるパスワードリセット機能を定義する。
- ユーザーにリセット確認メールを送信する「Eメール送信ステージ (Email Stage)」を定義し、移行中のDMS（または外部SMTP）を経由して送信できるように連携する。

### 3.6. シークレット、既存環境変数の移行および管理方針
- **移行および新規作成の切り分け**:
  - **既存インポート対象**: **Roundcube** および **ArgoCD** に関連するリソース。これらは既存の設定値（Client ID / Client Secret等）を維持し、`terraform import` で取り込みます。
  - **新規作成対象**: **Cloudflare Access**、**DMS LDAP / LDAP Outpost**、**Discord連携関連**、**パスワードリカバリーフロー**。これらはすべて新規リソースとしてTerraformで定義・構築します。
- **既存環境変数の流用**:
  - すでに HCP Terraform や Infisical に定義されている環境変数（例: `var.authentik_cf_client_id` や `var.authentik_cf_client_secret` など）およびシークレットをそのまま再利用し、新たな変数追加による設定変更を最小限に抑えます。
- **管理用認証情報の取り扱い**:
  - AuthentikをTerraformで管理するために、管理用APIトークンを発行して環境変数 `AUTHENTIK_TOKEN` でHCP Terraformに渡す必要があります。

## 4. スコープ外
- Authentik サーバー本体のKubernetesデプロイ構成の変更。
- 招待コードによる制限付き登録など、複雑な独自Flowのカスタマイズ（基本的にはデフォルトフローの利用、または簡易的なバインドのみに留める）。
- 本番クラスターのKubernetesリソースの直接操作・状態破壊。
