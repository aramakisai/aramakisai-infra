# Requirements: directus-sso

## Overview

Authentik を OIDC プロバイダーとして Directus CMS に SSO 連携する。
実行委員と学生団体のアカウントは完全に別系統で管理する。

---

## Functional Requirements

### FR-1: OIDC アプリケーション

**FR-1.1** Authentik に Directus 向け OIDC Provider アプリケーションを作成すること。
**FR-1.2** 本番 (`api.aramakisai.com`) とステージング (`stg-api.aramakisai.com`) で別アプリケーションを作成すること。

### FR-2: グループ構成

**FR-2.1** Authentik に以下のグループを用意すること:

| グループ名 | 対象 | Directus ロール | 備考 |
|---|---|---|---|
| `executive` | 荒牧祭実行委員 | `executive` | 既存 `discord-linked-users` グループのリネーム |
| `student_exhibitor` | 学生団体担当者 | `student_exhibitor` | 新規作成 |

**FR-2.2** 実行委員と学生団体のユーザーは別アカウントとし、同一ユーザーが両グループに所属するケースは想定しないこと。

**FR-2.3** `executive` は新規グループ作成ではなく、既存 `discord-linked-users` グループ (`terraform/authentik_discord.tf`) のリネームとして実装すること。Discord ログイン時に `discord-group-sync-policy` により自動付与される仕組みをそのまま継承する。

**FR-2.4** リネームに伴い、以下のハードコードされたグループ名参照を同時に更新すること:
- `terraform/authentik_discord.tf`: `discord-group-sync-policy` 内の `"discord-linked-users"` 参照（2箇所）
- `terraform/authentik_policies.tf`: `require-discord-link-policy` 内の `ak_is_group_member` 参照

### FR-3: グループクレーム

**FR-3.1** OIDC トークンの `groups` クレームにユーザーが所属するグループ名を含めること。
**FR-3.2** Directus が `groups` クレームを読み取り、対応するロールを自動付与できること。

### FR-4: ロールマッピング

**FR-4.1** Directus SSO 設定で `role_claim = groups` を指定すること。
**FR-4.2** グループ名と Directus ロール名のマッピングを設定すること:
- `executive` → `executive`
- `student_exhibitor` → `student_exhibitor`
**FR-4.3** どちらのグループにも所属しないユーザーはデフォルトロールでログインできないこと (アクセス拒否)。

---

## Non-Functional Requirements

**NFR-1** Authentik 設定は IaC (Terraform) で管理すること。
**NFR-2** Client Secret は Infisical で管理し、コードに直書きしないこと。
**NFR-3** 本番とステージングで Client ID / Secret を分離すること。

---

## Out of Scope

- Directus ロール (`executive`, `student_exhibitor`) の作成・権限設定 (`aramakisai-web` の `directus-schema` spec で管理)
- 学生団体ユーザーの Authentik アカウント作成フロー
- Authentik 自体のインストール・初期設定
