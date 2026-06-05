# 設計書 (Design) - Authentik IaC化

## 1. 全体構成案
Authentikの設定をコード化するために、Terraformのプロバイダー設定を追加し、用途ごとにファイルを分割して定義します。

### ディレクトリ構成
```text
terraform/
├── providers.tf             # goauthentik/authentik プロバイダー定義を追加
├── variables.tf             # 既存変数に Authentik 接続情報や Discord 認証情報を追加
├── authentik_main.tf        # プロバイダー接続設定、共通のフロー/グループ定義
├── authentik_apps.tf        # 各種アプリケーション連携 (Roundcube, ArgoCD, Cloudflare Access)
├── authentik_ldap.tf        # DMS LDAP プロバイダーおよび LDAP Outpost
└── authentik_discord.tf     # Discord 連携ソース、Property Mapping、Group Membership Policy
```

---

## 2. 詳細設計

### 2.1. プロバイダー設定の追加 (`providers.tf`)
`goauthentik/authentik` プロバイダーを追加します。

```hcl
# terraform/providers.tf
terraform {
  required_providers {
    # ... 既存のプロバイダー (hcloud, tailscale, cloudflare 等)
    authentik = {
      source  = "goauthentik/authentik"
      version = "~> 2024.12.0" # Authentikのバージョンに合わせた適切なプロバイダーバージョン
    }
  }
}

provider "authentik" {
  url   = var.authentik_url   # 環境変数 AUTHENTIK_URL もしくはデフォルト https://idp.aramakisai.com
  token = var.authentik_token # 環境変数 AUTHENTIK_TOKEN (APIトークン)
}
```

### 2.2. 変数定義 (`variables.tf`)
既存の `variables.tf` に、変更を最小限に抑える形で環境変数や新規変数を定義します。

```hcl
variable "authentik_url" {
  type        = string
  default     = "https://idp.aramakisai.com"
  description = "Authentik API Endpoint URL"
}

variable "authentik_token" {
  type        = string
  sensitive   = true
  description = "Authentik API Token"
}

# 既存の変数（var.authentik_cf_client_id / var.authentik_cf_client_secret）はそのまま使い回す

variable "discord_client_id" {
  type        = string
  description = "Discord OAuth2 Client ID"
}

variable "discord_client_secret" {
  type        = string
  sensitive   = true
  description = "Discord OAuth2 Client Secret"
}
```

### 2.3. アプリケーション連携の定義 (`authentik_apps.tf`)

#### ① Roundcube (Webmail) - 既存インポート
手動で作成されたリソースを `terraform import` で取り込めるよう、同一の `client_id` と `client_secret` で定義します。
- `client_id` は既存の `aramakisai-mail` を指定します。
- `client_secret` は Infisical 上の `MAIL_OAUTH2_CLIENT_SECRET` を変数経由で流し込みます。

```hcl
resource "authentik_provider_oauth2" "roundcube" {
  name          = "Roundcube"
  client_id     = "aramakisai-mail"
  client_secret = var.roundcube_oauth2_client_secret
  
  authorization_flow = data.authentik_flow.default_authorization.id
  
  redirect_uris = [
    "https://webmail.aramakisai.com/index.php/login/oauth"
  ]
  
  property_mappings = [
    data.authentik_property_mapping.oauth_scope_openid.id,
    data.authentik_property_mapping.oauth_scope_email.id,
    data.authentik_property_mapping.oauth_scope_profile.id
  ]
}

resource "authentik_application" "roundcube" {
  name              = "Roundcube"
  slug              = "roundcube"
  provider_id       = authentik_provider_oauth2.roundcube.id
  open_in_new_tab   = true
}
```

#### ② ArgoCD - 既存インポート
- `client_id` は既存の `argocd` を指定。
- `client_secret` は ArgoCD の OIDC シークレットと同じものを変数から設定。

```hcl
resource "authentik_provider_oauth2" "argocd" {
  name          = "ArgoCD"
  client_id     = "argocd"
  client_secret = var.argocd_oidc_client_secret
  
  authorization_flow = data.authentik_flow.default_authorization.id
  
  redirect_uris = [
    "https://argocd.aramakisai.com/auth/callback"
  ]
  
  property_mappings = [
    data.authentik_property_mapping.oauth_scope_openid.id,
    data.authentik_property_mapping.oauth_scope_profile.id,
    data.authentik_property_mapping.oauth_scope_email.id,
    data.authentik_property_mapping.oauth_scope_groups.id
  ]
}

resource "authentik_application" "argocd" {
  name              = "ArgoCD"
  slug              = "argocd"
  provider_id       = authentik_provider_oauth2.argocd.id
  open_in_new_tab   = true
}
```

#### ③ Cloudflare Access - 新規作成
- `client_id` / `client_secret` は既存の `var.authentik_cf_client_id` / `var.authentik_cf_client_secret` をそのまま引き渡します。

```hcl
resource "authentik_provider_oauth2" "cloudflare" {
  name          = "Cloudflare Access"
  client_id     = var.authentik_cf_client_id
  client_secret = var.authentik_cf_client_secret
  
  authorization_flow = data.authentik_flow.default_authorization.id
  
  # Cloudflare Access OIDC 連携用エンドポイント
  redirect_uris = [
    "https://idp.aramakisai.com/application/o/cloudflare/authorize/", # Cloudflare側のcallback
  ]
  
  property_mappings = [
    data.authentik_property_mapping.oauth_scope_openid.id,
    data.authentik_property_mapping.oauth_scope_email.id,
    data.authentik_property_mapping.oauth_scope_profile.id
  ]
}

resource "authentik_application" "cloudflare" {
  name              = "Cloudflare Access"
  slug              = "cloudflare"
  provider_id       = authentik_provider_oauth2.cloudflare.id
}
```

---

### 2.4. Docker Mailserver (DMS) / LDAP 連携の定義 (`authentik_ldap.tf`)
DMS と連携するための LDAP プロバイダーおよび LDAP Outpost を定義します。

```hcl
# DMS用のLDAPプロバイダー
resource "authentik_provider_ldap" "dms" {
  name      = "DMS LDAP"
  bind_flow = data.authentik_flow.default_authentication.id
}

# DMS用のLDAPアプリケーション
resource "authentik_application" "dms_ldap" {
  name        = "DMS LDAP"
  slug        = "dms-ldap"
  provider_id = authentik_provider_ldap.dms.id
}

# クラスター内で稼働する LDAP Outpost の登録
# token は動的に生成され、これが Outpost の deployment 等で使用される。
resource "authentik_outpost" "dms_ldap" {
  name = "dms-ldap-outpost"
  type = "ldap"
  
  providers = [
    authentik_provider_ldap.dms.id
  ]
  
  # Outpost の設定値 (JSON文字列で渡す)
  config = jsonencode({
    authentik_host          = var.authentik_url
    authentik_host_browser  = var.authentik_url
    authentik_host_insecure = false
    log_level               = "info"
  })
}
```

---

### 2.5. Discord 連携ソース、Property Mapping、Membership Policy (`authentik_discord.tf`)
Discord ソーシャルログインと、それに紐づくプロパティマッピングおよびログイン時のグループ自動割当ポリシーを設計します。

#### ① Discord 連携ソース (`authentik_source_oauth`)
Discordを認証元として登録します。
- `user_path` で Discord から作成されたユーザーが配置されるパスを指定します。

```hcl
resource "authentik_source_oauth" "discord" {
  name                = "Discord"
  slug                = "discord"
  provider_type       = "discord"
  
  client_id           = var.discord_client_id
  client_secret       = var.discord_client_secret
  
  authentication_flow = data.authentik_flow.default_source_authentication.id
  enrollment_flow     = data.authentik_flow.default_source_enrollment.id
  
  # Property Mapping のアサイン
  user_property_mappings = [
    authentik_property_mapping_source_oauth.discord_username.id,
    authentik_property_mapping_source_oauth.discord_email.id
  ]
}
```

#### ② Discord Property Mapping (`authentik_property_mapping_source_oauth`)
Discord から返される JSON から Authentik のユーザー情報へとマッピングします。
- `expression` を用いて、JSON の特定のフィールドを Python 表記で割り当てます。

```hcl
# ユーザー名マッピング
resource "authentik_property_mapping_source_oauth" "discord_username" {
  name       = "discord-username-mapping"
  expression = "return oauth_user.get('username')"
  object_type = "user"
}

# メールアドレスマッピング
resource "authentik_property_mapping_source_oauth" "discord_email" {
  name       = "discord-email-mapping"
  expression = "return oauth_user.get('email')"
  object_type = "user"
}
```

#### ③ Membership Policy (Group Membership)
Discord でログインしたユーザーを、特定の `discord-users` グループに自動所属させるポリシーを実装します。
- `authentik_group.discord_users` グループを作成。
- Expression Policy (`authentik_policy_expression`) を用いて、ログイン元のソースが Discord（`request.source.slug == 'discord'`）である場合に、ユーザーをグループに追加する処理を登録します。

```hcl
resource "authentik_group" "discord_users" {
  name = "discord-users"
}

# ユーザーが Discord ソース経由でログインした時に true を返し、グループ所属処理をトリガーする Expression Policy
resource "authentik_policy_expression" "discord_membership" {
  name       = "discord-membership-policy"
  expression = <<-EOT
    # ユーザーが Discord からログインしているか検証
    if request.source and request.source.slug == 'discord':
        # ログインユーザーオブジェクトを取得し、discord-users グループに追加
        ak_user = request.user
        if ak_user:
            # グループに所属していなければ追加する
            group_name = "discord-users"
            # (AuthentikのExpression実行環境におけるユーザー操作ヘルパーを使用)
            # 実際には登録/サインインフローの「User Write」ステージ、またはカスタムポリシーとバインドで実現します。
            # 例: Flow の enrollment/login ステージにポリシーをアサイン
        return True
    return False
  EOT
}
```
> [!NOTE]
> 通常の自動グループ割り当て（Membership Policy）は、Discord 連携ソースの `Enrollment Flow` に `Group Member Binding` もしくは `Expression Policy` を適用することで実現します。設計フェーズで詳細なフロー構造を確認してタスク化します。

---

### 2.6. パスワードリカバリーフローの定義 (`authentik_recovery.tf`)
パスワード紛失時に、DMS経由でメールを送信してリセットする簡易リカバリーフローを定義します。

```hcl
# リカバリー用のフロー自体
resource "authentik_flow" "recovery" {
  name        = "Password Recovery Flow"
  slug        = "password-recovery"
  title       = "Password Recovery"
  designation = "recovery"
}

# Eメール送信ステージ (Email Stage)
resource "authentik_stage_email" "recovery_email" {
  name = "recovery-email-stage"
  
  use_global_settings = true # Authentik全体のSMTP設定を使用
  subject             = "Password Reset Request"
  template            = "email/password_reset.html"
}

# パスワード変更入力ステージ
resource "authentik_stage_password" "recovery_password" {
  name = "recovery-password-stage"
  # パスワードの最低条件やバックエンドの指定
}

# フローとステージの紐付け (Flow Binding)
resource "authentik_flow_stage_binding" "recovery_email_bind" {
  target = authentik_flow.recovery.id
  stage  = authentik_stage_email.recovery_email.id
  order  = 10
}

resource "authentik_flow_stage_binding" "recovery_password_bind" {
  target = authentik_flow.recovery.id
  stage  = authentik_stage_password.recovery_password.id
  order  = 20
}
```

---

## 3. インポート手順 (Roundcube, ArgoCD)
既存の Roundcube と ArgoCD は稼働中のため、以下のインポート手順を踏むことでダウンタイムなしでIaCに移行します。

### 3.1. インポートコードの記述
Terraform 1.5+ で導入された `import` ブロックを一時的に `authentik_imports.tf` に記述します。これにより `terraform apply` 時に自動でインポートされます。

```hcl
# terraform/authentik_imports.tf (インポート完了後に削除可能)

# Roundcube Provider のインポート (IDはAuthentik上のUUIDまたはスラッグ)
import {
  to = authentik_provider_oauth2.roundcube
  id = "roundcube" # または実際の UUID
}

import {
  to = authentik_application.roundcube
  id = "roundcube"
}

# ArgoCD Provider のインポート
import {
  to = authentik_provider_oauth2.argocd
  id = "argocd"
}

import {
  to = authentik_application.argocd
  id = "argocd"
}
```

### 3.2. インポート実行手順
1. 環境変数 `AUTHENTIK_URL` と `AUTHENTIK_TOKEN` を HCP Terraform またはローカルシェルにセットします。
2. `terraform plan` を実行し、既存リソースが `import` (取り込み) となり、新規作成が `create` となることを確認します。
3. `terraform apply` を適用して取り込みを完了させます。

---

## 4. 懸念事項・リスク
- **Authentik API トークンの事前発行**: 
  - Terraformを適用する前に、AuthentikのWebUIから管理権限のある「API Token」を生成し、HCP Terraformの環境変数 `AUTHENTIK_TOKEN` にセットしておく必要があります。
- **LDAP Outpost トークンの受け渡し**:
  - `authentik_outpost.dms_ldap` が作成された際、サービストークンが自動生成されます。このトークンの値は Kubernetes 上の `ExternalSecret` (`authentik-ldap-outpost-token`) に登録する `AUTHENTIK_LDAP_OUTPOST_TOKEN` と一致している必要があります。
  - 設計として、Terraformの `output` で Outpost トークンを書き出し、それを Infisical に流し込む運用にするか、既存のトークンをインポート・指定する手段を確認する必要があります。
