# Technical Design: directus-sso

## Overview

Authentik OIDC → Directus SSO 連携の設計。
グループクレームによるロール自動マッピングを実現する。

## アーキテクチャ

```
学生 / 実行委員
    │ ブラウザ
    ▼
Directus ログイン画面
    │ "Authentik でログイン" クリック
    ▼
Authentik (OIDC Authorization Endpoint)
    │ 認証・認可
    │ groups クレーム付き ID Token 発行
    ▼
Directus (OIDC Callback)
    │ groups クレームを読み取り → Directus ロール付与
    ▼
Directus 管理画面 / API
```

## Authentik 設定

### OIDC Provider

| 項目 | 値 |
|---|---|
| Name | `directus-prod` / `directus-stg` |
| Client type | Confidential |
| Redirect URIs | `https://api.aramakisai.com/auth/login/authentik/callback` (prod) / `https://stg-api.aramakisai.com/auth/login/authentik/callback` (stg) |
| Signing Key | Authentik デフォルト証明書 |
| Scopes | `openid`, `profile`, `email`, `groups` |

### Property Mappings (groups クレーム)

既存の `authentik_property_mapping_provider_scope.oauth_scope_groups` (`terraform/authentik_apps.tf`) を再利用する。新規作成不要。

トークン内容:
```json
{
  "sub": "...",
  "email": "user@example.com",
  "groups": ["executive"]
}
```

### グループ

| グループ名 | 説明 | 実装方針 |
|---|---|---|
| `executive` | 荒牧祭実行委員。`executive` ロールにマッピング | 既存 `discord-linked-users` グループのリネーム |
| `student_exhibitor` | 学生団体担当者。`student_exhibitor` ロールにマッピング | 新規作成 |

### 既存グループのリネーム

`discord-linked-users` は Discord ログイン時に `discord-group-sync-policy` が自動付与するグループ。
このグループを `executive` にリネームすることで、Discord 連携済み全員 = 実行委員の意味論を正確に表現する。

リネームに伴い以下を更新する:

**`terraform/authentik_discord.tf`**:
```hcl
# Before
resource "authentik_group" "discord_linked_users" {
  name = "discord-linked-users"
}

# After
resource "authentik_group" "executive" {
  name = "executive"
}
```

`discord-group-sync-policy` expression 内のハードコード参照（2箇所）:
```python
# Before
new_pks.add(Group.objects.get(name="discord-linked-users").pk)
managed_pks.add(Group.objects.get(name="discord-linked-users").pk)

# After
new_pks.add(Group.objects.get(name="executive").pk)
managed_pks.add(Group.objects.get(name="executive").pk)
```

**`terraform/authentik_policies.tf`**:
```python
# Before
is_linked = ak_is_group_member(request.user, name="discord-linked-users")

# After
is_linked = ak_is_group_member(request.user, name="executive")
```

## Directus 設定

Directus 11.1.2 を使用。現状は内部認証のみ（OIDC 未設定）。

### 環境変数 (`gitops/manifests/prod/directus/deployment.yaml` に追加)

```env
AUTH_PROVIDERS=authentik

AUTH_AUTHENTIK_DRIVER=openid
AUTH_AUTHENTIK_CLIENT_ID=<Infisical管理>
AUTH_AUTHENTIK_CLIENT_SECRET=<Infisical管理>
AUTH_AUTHENTIK_ISSUER_URL=https://auth.aramakisai.com/application/o/directus-prod/
AUTH_AUTHENTIK_IDENTIFIER_KEY=email
AUTH_AUTHENTIK_ALLOW_PUBLIC_REGISTRATION=true
AUTH_AUTHENTIK_DEFAULT_ROLE_ID=
AUTH_AUTHENTIK_ROLE_CLAIM=groups
```

`DEFAULT_ROLE_ID` を空にすることで、どのグループにも属さないユーザーはログイン不可（FR-4.3）。

### ロールマッピング

Directus は `groups` クレームの値を Directus ロール名と照合する。

| `groups` クレーム値 | Directus ロール |
|---|---|
| `executive` | `executive` |
| `student_exhibitor` | `student_exhibitor` |

## IaC 管理方針

既存 `terraform/authentik_apps.tf` に追記する。

### 追加リソース

```hcl
# prod
resource "authentik_provider_oauth2" "directus_prod" {
  name          = "directus-prod"
  client_id     = var.directus_prod_oidc_client_id
  client_secret = var.directus_prod_oidc_client_secret
  signing_key   = data.authentik_certificate_key_pair.default.id

  authorization_flow = data.authentik_flow.default_authorization.id
  invalidation_flow  = data.authentik_flow.default_invalidation.id

  allowed_redirect_uris = [{
    matching_mode = "strict"
    url           = "https://api.aramakisai.com/auth/login/authentik/callback"
  }]

  property_mappings = [
    data.authentik_property_mapping_provider_scope.oauth_scope_openid.id,
    data.authentik_property_mapping_provider_scope.oauth_scope_profile.id,
    data.authentik_property_mapping_provider_scope.oauth_scope_email.id,
    authentik_property_mapping_provider_scope.oauth_scope_groups.id,  # 既存リソース再利用
  ]
}

resource "authentik_application" "directus_prod" {
  name              = "Directus (prod)"
  slug              = "directus-prod"
  protocol_provider = authentik_provider_oauth2.directus_prod.id
  open_in_new_tab   = true
}

# stg (同構成、URL と変数のみ差し替え)

# student_exhibitor グループ (新規作成)
resource "authentik_group" "student_exhibitor" {
  name = "student_exhibitor"
}
```

### Terraform 変数 (`terraform/variables.tf` に追加)

```hcl
variable "directus_prod_oidc_client_id" {
  description = "Directus prod OIDC Client ID"
  type        = string
  sensitive   = true
  default     = ""
}

variable "directus_prod_oidc_client_secret" {
  description = "Directus prod OIDC Client Secret"
  type        = string
  sensitive   = true
  default     = ""
}
```

### シークレット管理 (Infisical)

```
DIRECTUS_PROD_OIDC_CLIENT_ID
DIRECTUS_PROD_OIDC_CLIENT_SECRET
DIRECTUS_STG_OIDC_CLIENT_ID
DIRECTUS_STG_OIDC_CLIENT_SECRET
```

`gitops/manifests/prod/directus/external-secret.yaml` に `AUTH_AUTHENTIK_CLIENT_ID` / `AUTH_AUTHENTIK_CLIENT_SECRET` のエントリを追加する。

## 関連 Spec

- `aramakisai-web/.kiro/specs/directus-schema` — Directus ロール権限定義
- `aramakisai-infra/.kiro/specs/authentik-iac` — Authentik IaC 基盤
