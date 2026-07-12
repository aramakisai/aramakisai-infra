# Research & Design Decisions

## Summary
- **Feature**: `wikijs-deploy`
- **Discovery Scope**: Extension(既存のCNPG / Authentik OIDC / Infisical+ESO / ArgoCD GitOpsパターンへの新規サービス追加)
- **Key Findings**:
  - Wiki.jsは他候補(Outline/Docmost)と異なりRedis/S3が不要、CNPG Postgresのみで完結し、無料版でAuthentik OIDCがネイティブ利用できる
  - `authentication`テーブルのスキーマを実際に読み(`server/db/migrations/2.0.0.js`, `2.5.1.js`, `server/models/authentication.js`)、直接SQL UPSERTで宣言的管理可能と確認済み
  - コミュニティ製Terraform provider(`tyclipso/wikijs`)はメンテナンス実態が薄く(最終release 2025-01、star/fork 0)、`wikijs_auth_strategies`がstate平文保存+全置換型設計のため不採用
  - Wiki.jsは認証strategyを起動時に一度だけDBから読み込みメモリキャッシュするため、DB変更後はPod再起動が必須(Directus schema-apply後のrollout restartと同一パターン)

## Research Log

### Wiki.js vs Outline vs BookStack vs Docmost 比較
- **Context**: ノードの実質空きメモリが~1.6-1.8GBと逼迫している状況で、Authentik SSO統合可能なwikiを選定する必要があった
- **Sources Consulted**: 各プロダクト公式ドキュメント、authentik公式integrationドキュメント(`integrations.goauthentik.io`)
- **Findings**:
  - Outline: Postgres+Redis+S3互換ストレージが必須、4vCPU/8GB推奨規模
  - BookStack: MySQL/MariaDBが必要(このインフラのCNPG=Postgres専用方針と不整合、DBエンジンが1つ増える)
  - Docmost: SSO(OIDC/SAML/LDAP)がEnterprise Edition限定(有料)、無料版はemail+password認証のみ
  - Wiki.js: Postgres対応(CNPGにそのまま乗る)、Redis不要、Object Storageも不要(ローカル/DB格納)、AGPL-3.0で機能制限なし、OIDCが無料版でネイティブ動作、プロセス実メモリ実測 ~140MB(ローカルdocker検証)
- **Implications**: Wiki.jsを採用。他候補はDBエンジン追加・有料SSO・重量級依存のいずれかで不適合

### Wiki.js `authentication` テーブルの実スキーマ調査
- **Context**: OIDC設定をTerraform providerではなく宣言的Jobで管理するため、DB書き込み対象の正確なスキーマ確認が必要だった
- **Sources Consulted**: `github.com/requarks/wiki` の `server/db/migrations/2.0.0.js`、`2.5.1.js`、`server/models/authentication.js`、`server/core/auth.js`、`server/modules/authentication/oidc/definition.yml`(すべてGitHub API経由で実ファイル取得)
- **Findings**:
  - `authentication`テーブル: `key`(PK) / `isEnabled` / `config`(json) / `selfRegistration` / `domainWhitelist`(json) / `autoEnrollGroups`(json) / `order` / `strategyKey` / `displayName`
  - `domainWhitelist`・`autoEnrollGroups`は`{"v": [...]}`形式でラップされる(`_.get(str.domainWhitelist, 'v', [])`で読み出し、素の配列を入れると空扱いになる)
  - OIDCモジュールの`config`キー: `clientId`/`clientSecret`/`authorizationURL`/`tokenURL`/`userInfoURL`/`skipUserProfile`/`issuer`/`emailClaim`/`displayNameClaim`/`pictureClaim`/`mapGroups`/`groupsClaim`/`logoutURL`/`acrValues`
  - strategiesは`server/core/auth.js`内で起動時に一度だけ`WIKI.models.authentication.getStrategies()`を呼び出し`WIKI.auth.strategies`にキャッシュ。以降はプロセス再起動までDBの変更を検知しない
  - `local`ストラテジーは削除・無効化不可(root管理者ログインの必須フォールバック)。UIから隠すことはできてもDB上のレコード自体は残す設計
- **Implications**: SQL UPSERT + Pod rollout restart の2段構成であれば完全に宣言的管理が可能。`local`レコードは常に残す

### コミュニティ製Terraform provider (`tyclipso/wikijs`) の評価
- **Context**: `terraform-provider-wikijs`がWiki.js GraphQL Admin APIをフルカバーし`wikijs_auth_strategies`リソースを持つことを確認したが、採用可否を判断するため実態を調査
- **Sources Consulted**: `github.com/tyclipso/terraform-provider-wikijs`(リポジトリメタデータ、README、`internal/provider/auth_strategies_resource.go`ソース、Issue一覧)、Terraform Registry公式ドキュメント
- **Findings**:
  - stars/forks 0、contributor 2名、最終release v1.0.7(2025-01、以降更新なし)、元はStartnext GmbH社内ツールのフォーク
  - 公式ドキュメント自身が「秘密情報がTerraform stateに平文保存される」「本番利用は非推奨」と明記
  - `wikijs_auth_strategies`は`strategies`をList全体で持つ単一リソース設計(個別strategyごとのresourceではない)。HCLに書いた分だけが正となり、UI経由の追加strategyは次回applyで消える全置換挙動
  - open issue #6が「未文書化のresource/data-sourceへのドキュメント追加」要求のまま放置、ドキュメント整備自体が追いついていない
  - Wiki.js公式が保証する契約ではなく、内部GraphQLスキーマへの非公式リバースエンジニアリング
- **Implications**: 不採用。既存のDirectus schema-apply-job/opendkim-keytableと同じ「ConfigMap+Infisical注入のJobによる宣言的SQL UPSERT」パターンを採用する

### 既存CNPG/Authentik OIDC統合パターンの踏襲
- **Context**: 新規CNPG Clusterおよび Authentik OIDC Provider/Applicationの定義方法を既存サービスと揃える必要があった
- **Sources Consulted**: `gitops/manifests/prod/directus/db-cluster.yaml`、`gitops/manifests/prod/authentik/scheduled-backup.yaml`、`terraform/authentik_apps.tf`(リポジトリ内既存ファイル)
- **Findings**:
  - CNPG Cluster: `instances: 1`、`bootstrap.recovery`、Hetzner Object Storageへの`barmanObjectStore`、`retentionPolicy`必須
  - ScheduledBackupの`schedule`は秒付き6フィールドcron形式が必須(5フィールドで書くと"毎時"に誤動作する既知の問題を本セッション中に発見・修正済み。[[project_directus_schema_apply_custom_migration_idempotency]]系の教訓)
  - Authentik OIDC Provider/Applicationは`terraform/authentik_apps.tf`パターンに追記する形でTerraform管理するのが標準
- **Implications**: 新規db-cluster.yaml/scheduled-backup.yamlは既存ファイルをテンプレートとして流用。schedule書式は初回から正しく実装する

## Architecture Pattern Evaluation

| Option | Description | Strengths | Risks / Limitations | Notes |
|--------|-------------|-----------|---------------------|-------|
| Terraform provider (`tyclipso/wikijs`) | GraphQL API経由で全設定をTerraform管理 | 完全宣言的、state管理 | 非公式・メンテ実態薄い・secret平文state・全置換設計 | 不採用 |
| 手動Admin UI設定 | 初回セットアップ時にブラウザでOIDC strategyを設定 | 実装コスト最小 | DR時に手順書通り再現する必要があり、GitOpsの「Gitが正」原則から外れる | 部分採用(初回セットアップウィザードのみこの方式) |
| **宣言的SQL UPSERT Job(採用)** | ConfigMapのSQLテンプレート+Infisical secretをJobで`psql`実行、ON CONFLICT UPSERT | 完全宣言的、既存Directus/opendkimパターンと同一、state不要 | Wiki.js内部スキーマへの依存(バージョンアップで変わる可能性) | 採用。スキーマ変更検知のため定期的な動作確認が必要 |

## Design Decisions

### Decision: OIDC設定はJobによるDirect SQL UPSERTで管理する
- **Context**: Authentik SSO統合の設定(client_id/secret含む)を宣言的に管理したいが、信頼できるTerraform providerが存在しない
- **Alternatives Considered**:
  1. `tyclipso/wikijs` Terraform provider — メンテナンス・セキュリティ両面でリスクあり
  2. 手動Admin UI設定のみ — GitOps原則(Gitが正)から外れ、DR再現性が低い
- **Selected Approach**: `gitops/manifests/prod/wikijs/auth-strategy-configmap.yaml`にSQLテンプレートを保持し、`auth-strategy-job.yaml`(Kubernetes Job、PostSync hook)がInfisical由来の環境変数を用いて`authentication`テーブルへ`ON CONFLICT (key) DO UPDATE`で反映する。適用後にWiki.js Deploymentのrollout restartをトリガーする。
- **Rationale**: 既存のDirectus schema-apply-job / opendkim-keytableパターンと完全に一致し、Terraform state管理の複雑さ・サードパーティprovider依存を回避できる
- **Trade-offs**: Wiki.js内部スキーマ(非公開契約)に依存するため、Wiki.jsのメジャーバージョンアップ時にスキーマ変更がないか確認が必要
- **Follow-up**: Wiki.jsアップグレード時は`authentication`テーブルのカラム構成に変更がないか事前確認する運用ルールをdr.md等に追記することを検討

### Decision: CNPGクラスタは既存パターンに完全準拠
- **Context**: 新規DBクラスタのバックアップ設定で、本セッション中に発見したhourly誤発火バグを再発させない必要がある
- **Alternatives Considered**: なし(既存パターンからの逸脱を検討する理由がない)
- **Selected Approach**: `instances: 1`、`bootstrap.recovery`、秒付き6フィールドcron、明示的`retentionPolicy`
- **Rationale**: 既存4クラスタ(authentik-db/directus-db/vaultwarden-db/presence-db)と運用を統一し、今回のインシデントの教訓を新規クラスタに反映
- **Trade-offs**: なし
- **Follow-up**: デプロイ後、`ScheduledBackup.status.nextScheduleTime`が意図した間隔になっているか確認する

## Risks & Mitigations
- Wiki.jsメジャーバージョンアップで`authentication`テーブルスキーマが変わり、UPSERT Jobが失敗する — アップグレード前に該当マイグレーションファイルを確認する運用ルールを設ける
- ノードメモリがすでに逼迫している状態への追加デプロイ — requests/limitsを保守的に設定し、デプロイ後に`make kubectl top nodes`で実測確認する
- schedule cron書式ミスの再発 — 秒付き6フィールド形式をテンプレート段階で固定し、レビュー時に必ずフィールド数を確認する

## References
- [Configuration - Wiki.js](https://docs.requarks.io/install/config) — config.yml/env var挙動
- [Integrate with Wiki.js | authentik](https://integrations.goauthentik.io/documentation/wiki-js/) — OIDC統合の一般手順
- [WikiJS Provider - Terraform Registry](https://registry.terraform.io/providers/tyclipso/wikijs/latest/docs) — 不採用としたTerraform provider
- `github.com/requarks/wiki` — `server/db/migrations/2.0.0.js`, `2.5.1.js`, `server/models/authentication.js`, `server/core/auth.js`, `server/modules/authentication/oidc/definition.yml`(実ソース確認)
