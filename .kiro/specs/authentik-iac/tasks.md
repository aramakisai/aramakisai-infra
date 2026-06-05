# タスク一覧 (Tasks) - Authentik IaC化

## フェーズ 1: 事前準備 & プロバイダー定義
- [ ] 1.1 Authentik 管理画面からの API トークン取得
  - Authentik WebUI（Admin）にログインし、Terraform管理用の API トークンを生成する。
  - 発行されたトークンを環境変数 `AUTHENTIK_TOKEN` にセットする（HCP Terraform およびローカル環境用）。
- [ ] 1.2 `providers.tf` への Authentik プロバイダーの追加
  - `goauthentik/authentik` を `required_providers` に追加し、接続設定を定義する。
  - _Boundary: terraform/providers.tf_
- [ ] 1.3 `variables.tf` への変数追加
  - Authentik 接続用、および Discord 認証用の変数を定義する。既存の Cloudflare 関連変数が競合しないか確認する。
  - _Boundary: terraform/variables.tf_

## フェーズ 2: 既存リソースのインポート (Roundcube & ArgoCD)
- [ ] 2.1 インポート定義ファイル (`authentik_imports.tf`) の作成
  - Roundcube と ArgoCD の Provider / Application を取り込むための `import` ブロックを一時的に記述する。
  - _Boundary: terraform/authentik_imports.tf_
- [ ] 2.2 移行対象リソースコード (`authentik_apps.tf`) の定義
  - Roundcube と ArgoCD の `authentik_provider_oauth2` および `authentik_application` のコードを記述する。
  - クライアントIDやシークレットは既存の環境変数/シークレット（例: `var.roundcube_oauth2_client_secret` 等）から引き渡す。
  - _Boundary: terraform/authentik_apps.tf_
- [ ] 2.3 インポートの実行
  - `terraform init` および `terraform plan` を実行し、2つのアプリケーションが「Import」として認識されることを確認する。
  - `terraform apply` を実行し、既存の手動設定に影響を与えることなく取り込みが完了したことを確認する。
  - 適用後、`authentik_imports.tf` を削除してコミット対象から外す。

## フェーズ 3: 新規リソースの定義と作成 (Cloudflare Access, DMS LDAP, Discord, Recovery)
- [ ] 3.1 Cloudflare Access 連携の作成
  - `authentik_apps.tf` に Cloudflare Access 用の OIDC Provider と Application の定義を追加する。
  - _Boundary: terraform/authentik_apps.tf_
- [ ] 3.2 DMS LDAP 連携 & LDAP Outpost の作成
  - `authentik_ldap.tf` を新規作成し、LDAP Provider, Application, および Outpost を定義する。
  - _Boundary: terraform/authentik_ldap.tf_
- [ ] 3.3 Discord 連携ソース、Property Mapping、Membership Policy の作成
  - `authentik_discord.tf` を新規作成し、`authentik_source_oauth.discord`、Property Mapping、および Expression Policy を定義する。
  - _Boundary: terraform/authentik_discord.tf_
- [ ] 3.4 リカバリーフローの作成
  - `authentik_recovery.tf` を新規作成し、パスワードリカバリーフロー、Emailステージ、Passwordステージ、およびそれぞれのバインド設定を定義する。
  - _Boundary: terraform/authentik_recovery.tf_
- [ ] 3.5 新規リソースの適用
  - `terraform plan` で新規リソースが正常に作成予定に並ぶことを確認。
  - `terraform apply` を実行して各リソースを構築する。

## フェーズ 4: 動作検証 & クリーンアップ
- [ ] 4.1 静的チェックの実行
  - `make lint` または `terraform fmt` を実行し、コードのフォーマットとリンターのエラーがないことを確認する。
- [ ] 4.2 LDAP Outpost トークンの確認
  - `authentik_outpost` によって自動生成されたトークンを確認し、DMS の LDAP Outpost 接続で正常に利用可能かを確認（必要に応じてクラスター側の `ExternalSecret` とトークン値を同期）。
