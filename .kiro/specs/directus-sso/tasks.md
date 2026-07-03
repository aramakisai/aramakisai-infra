# Implementation Plan

- [x] 1. 既存グループをリネームしてポリシー参照を修正する
- [x] 1.1 `discord-linked-users` グループを `executive` にリネームし、ハードコード参照を全て更新する
  - `terraform/authentik_discord.tf` のリソース名を `authentik_group.discord_linked_users` → `authentik_group.executive` に変更し、`name` 属性を `"executive"` に更新する
  - `moved` ブロックを追加して Terraform がリソースを destroy/recreate せず rename として扱うようにする
  - `discord-group-sync-policy` expression 内の `Group.objects.get(name="discord-linked-users")` を 2 箇所 `"executive"` に更新する
  - `terraform/authentik_policies.tf` の `require-discord-link-policy` で `ak_is_group_member(request.user, name="discord-linked-users")` を `"executive"` に更新する
  - `terraform plan` で既存グループが destroy なし・rename のみの差分として現れることを確認する
  - _Requirements: 2.1, 2.3, 2.4, NFR-1_

- [x] 2. 新規グループと Terraform 変数を追加する
- [x] 2.1 (P) `student_exhibitor` Authentik グループを Terraform で定義する
  - `terraform/authentik_apps.tf` に `resource "authentik_group" "student_exhibitor" { name = "student_exhibitor" }` を追加する
  - `terraform plan` で新規 create のみ差分に現れることを確認する
  - _Requirements: 2.1, NFR-1_
  - _Boundary: terraform/authentik_apps.tf_

- [x] 2.2 (P) Directus OIDC クライアントシークレット用 Terraform 変数を追加する
  - `terraform/variables.tf` に `directus_prod_oidc_client_secret` と `directus_stg_oidc_client_secret` を追加する（`sensitive = true`, `default = ""`）
  - _Requirements: NFR-2, NFR-3_
  - _Boundary: terraform/variables.tf_

- [x] 3. Authentik に Directus 用 OIDC Provider と Application を作成する
- [x] 3.1 prod 用 Provider と Application を `authentik_apps.tf` に追加する
  - `authentik_provider_oauth2.directus_prod`: `client_id = "directus-prod"`, `client_secret = var.directus_prod_oidc_client_secret`, redirect URI `https://api.aramakisai.com/auth/login/authentik/callback`
  - `property_mappings` に既存 `authentik_property_mapping_provider_scope.oauth_scope_groups.id` を含める（`groups` クレーム付与）
  - `authentik_application.directus_prod` を作成し `slug = "directus-prod"` で provider に紐付ける
  - `terraform apply` 後に Authentik UI で "Directus (prod)" アプリが表示されることを確認する
  - _Requirements: 1.1, 1.2, 3.1, NFR-1, NFR-3_

- [x] 3.2 stg 用 Provider と Application を `authentik_apps.tf` に追加する
  - `authentik_provider_oauth2.directus_stg`: `client_id = "directus-stg"`, `client_secret = var.directus_stg_oidc_client_secret`, redirect URI `https://stg-api.aramakisai.com/auth/login/authentik/callback`
  - `authentik_application.directus_stg` を作成し `slug = "directus-stg"` で紐付ける
  - `terraform apply` 後に Authentik UI で "Directus (stg)" アプリが表示されることを確認する
  - _Requirements: 1.1, 1.2, 3.1, NFR-1, NFR-3_

- [ ] 4. Infisical に Directus OIDC シークレットを登録する
- [ ] 4.1 prod/stg の Directus OIDC クライアントシークレットを Infisical に登録する
  - `DIRECTUS_PROD_OIDC_CLIENT_SECRET` を prod 環境、`DIRECTUS_STG_OIDC_CLIENT_SECRET` を stg 環境に登録する
  - Authentik の Provider 画面から client_secret をコピーして使用する
  - `infisical run --env=prod -- terraform plan` で変数が解決されることを確認する
  - _Requirements: NFR-2, NFR-3_

- [x] 5. prod Directus マニフェストに SSO 設定を追加する
- [x] 5.1 prod external-secret に OIDC クライアントシークレットのエントリを追加する
  - `gitops/manifests/prod/directus/external-secret.yaml` の `data` 配列に `AUTH_AUTHENTIK_CLIENT_SECRET` エントリを追加する（`remoteRef.key: DIRECTUS_PROD_OIDC_CLIENT_SECRET`）
  - ExternalSecret の `generation` が更新されクラスター側に反映されることを確認する（tech.md の既知問題に注意し、未反映の場合は operation patch で強制 sync する）
  - _Requirements: NFR-2_
  - _Depends: 4.1_

- [x] 5.2 prod Directus deployment に AUTH_* 環境変数を追加する
  - `gitops/manifests/prod/directus/deployment.yaml` の `env` セクションに以下を追加する:
    - `AUTH_PROVIDERS: authentik`
    - `AUTH_AUTHENTIK_DRIVER: openid`
    - `AUTH_AUTHENTIK_CLIENT_ID: directus-prod`
    - `AUTH_AUTHENTIK_CLIENT_SECRET` は `secretKeyRef` (key: `AUTH_AUTHENTIK_CLIENT_SECRET`) で参照
    - `AUTH_AUTHENTIK_ISSUER_URL: https://auth.aramakisai.com/application/o/directus-prod/`
    - `AUTH_AUTHENTIK_IDENTIFIER_KEY: email`
    - `AUTH_AUTHENTIK_ALLOW_PUBLIC_REGISTRATION: "true"`
    - `AUTH_AUTHENTIK_DEFAULT_ROLE_ID: ""` (空文字: 未所属ユーザーのアクセス拒否)
    - `AUTH_AUTHENTIK_ROLE_CLAIM: groups`
  - ArgoCD sync 後に Directus pod が再起動して AUTH_* が反映されることを確認する
  - _Requirements: 3.2, 4.1, 4.2, 4.3_
  - _Depends: 5.1_

- [x] 6. (P) stg Directus マニフェストに SSO 設定を追加する
- [x] 6.1 (P) stg external-secret に OIDC クライアントシークレットのエントリを追加する
  - `gitops/manifests/staging/directus/external-secret.yaml` の `data` 配列に `AUTH_AUTHENTIK_CLIENT_SECRET` エントリを追加する（`remoteRef.key: DIRECTUS_STG_OIDC_CLIENT_SECRET`）
  - _Requirements: NFR-2, NFR-3_
  - _Boundary: gitops/manifests/staging/directus/_
  - _Depends: 4.1_

- [x] 6.2 (P) stg Directus deployment に AUTH_* 環境変数を追加する
  - `gitops/manifests/staging/directus/deployment.yaml` の `env` セクションに prod と同様の AUTH_* を追加する（`AUTH_AUTHENTIK_ISSUER_URL` は `directus-stg`、`AUTH_AUTHENTIK_CLIENT_ID` は `directus-stg` に変更）
  - _Requirements: 3.2, 4.1, 4.2, 4.3, NFR-3_
  - _Boundary: gitops/manifests/staging/directus/_
  - _Depends: 6.1_

- [x] 7. E2E 動作検証
- [x] 7.1 Terraform apply で Authentik リソースが意図通り作成されることを確認する
  - ⚠️ **既知問題**: 新規作成 Provider は grant_types=[] になる (Terraform provider 非サポート)。apply 後に以下で手動修正が必要:
    ```bash
    infisical run --env=prod -- bash -c 'curl -s -X PATCH -H "Authorization: Bearer $AUTHENTIK_TOKEN" -H "Content-Type: application/json" -d "{\"grant_types\":[\"authorization_code\",\"implicit\",\"client_credentials\",\"password\",\"refresh_token\"]}" "https://idp.aramakisai.com/api/v3/providers/oauth2/<ID>/"'
    ```
  - `infisical run --env=prod -- terraform plan` で `executive` グループが destroy なし、新規リソース（`student_exhibitor`・Provider 2 件・Application 2 件）のみが差分に現れることを確認する
  - `infisical run --env=prod -- terraform apply` を実行する
  - Authentik UI でグループ一覧に `executive`・`student_exhibitor` が存在し、Directus (prod/stg) アプリが Applications 画面に表示されることを確認する
  - _Requirements: 1.1, 2.1, 2.3_

- [ ] 7.2 Directus SSO ログインで正しいロールが付与されることを確認する
  - `executive` グループのユーザーで Directus に "Authentik でログイン" を実行し、Directus ユーザーが `executive` ロールで作成されることを確認する
  - どのグループにも属さないユーザーでのログイン試行がアクセス拒否されることを確認する
  - 実行委員と学生団体が同一アカウントに重複しないことを確認する（FR-2.2）
  - _Requirements: 2.2, 3.2, 4.1, 4.2, 4.3_
  - _Depends: 5.2, 6.2_
