# Requirements Document

## Project Description (Input)
Wiki.jsをknowledge base / wikiとして導入する。CNPG Postgresクラスタを1個追加(既存のauthentik-db/directus-db等と同じパターン)、認証はAuthentik OIDCで統合(SSO)。OIDC設定はTerraformの信頼性の低いコミュニティ製providerではなく、既存のDirectus schema-apply-job/opendkim-keytableと同じ「ConfigMap+Infisical注入のJobが宣言的にDBへON CONFLICT UPSERT」パターンで管理する。Wiki.jsのauthenticationテーブルスキーマ(key/isEnabled/config/selfRegistration/domainWhitelist/autoEnrollGroups/order/strategyKey/displayName、domainWhitelist・autoEnrollGroupsは{"v":[...]}形式でラップされる点に注意)は事前調査済み。UPSERT後はWiki.js Podのrollout restartが必要(strategiesは起動時に一度だけDBから読み込みメモリキャッシュされるため、Directus schema-apply後のrollout restartパターンと同じ)。リソース制約(ノードmemory実質空き ~1.6-1.8GB)を踏まえ、Outline/BookStack/Docmostとの比較検討の結果、Postgres専用・Redis不要・Object Storage不要・プロセス実メモリ~140MB程度の軽量さとAuthentik OIDCが無料版で使える点からWiki.jsを選定した経緯がある。

## Introduction
荒牧祭実行委員会向けのナレッジベース/Wikiとして Wiki.js を prod クラスタに導入する。既存の CNPG + Authentik OIDC + Infisical/ESO + ArgoCD GitOps の運用パターンに完全準拠させ、単一障害点になりがちな「アプリ内部DBに保存される設定」を Terraform 経由の信頼性の低いサードパーティ provider に依存せず、Directus schema-apply-job / opendkim-keytable と同じ「宣言的 Job による ON CONFLICT UPSERT」パターンで管理する。単一ノード(CX33, 実質空きメモリ ~1.6-1.8GB)というリソース制約下での導入となるため、リソースサイジングと既存ワークロードへの影響も要件に含める。

## Boundary Context (Optional)
- **In scope**: Wiki.js アプリケーション本体のデプロイ、専用 CNPG Postgres クラスタの新設、Authentik OIDC によるSSO統合(Authentik側のOIDC Provider/Application作成 + Wiki.js側 authentication テーブルへの宣言的UPSERT)、バックアップ、外部公開(Cloudflare Tunnel経由)、シークレット管理
- **Out of scope**: コンテンツ移行(既存ドキュメントの取り込み)、ページ権限設計の詳細(グループ/ロール設計)、検索エンジン連携の高度化、Wiki.js以外のwiki候補(Outline/BookStack/Docmost)の再評価
- **Adjacent expectations**: Authentik側のOIDC Provider/Application定義は既存の `terraform/authentik_apps.tf` パターン(Terraform管理)に従う。Wiki.js側のみ、コミュニティ製 `terraform-provider-wikijs` は使用しない(メンテナンス実態が薄く、`wikijs_auth_strategies` がstate平文保存かつ全置換型設計であるため)。外部公開は既存サービス(directus, roundcube, vaultwarden等)と同じく `terraform/tunnel.tf`(`cloudflare_zero_trust_tunnel_cloudflared_config`の`ingress_rule`)+ `terraform/dns.tf`(`cloudflare_record` CNAME)によるCloudflare Tunnel直結パターンに従う(nginx-ingress等のk8s Ingressリソースはこのリポジトリでは使用していない)

## Requirements

### Requirement 1: Wiki.js アプリケーションのデプロイ
**Objective:** As an インフラ担当者, I want Wiki.js を既存のサービス追加パターンに準拠した形でprodにデプロイする, so that 他サービスと同じ運用手順(ArgoCD sync, ExternalSecret, Infisicalシークレット管理)で保守できる

#### Acceptance Criteria
1. The GitOps 構成 shall `gitops/apps/prod/wikijs.yaml` (ArgoCD Application) と `gitops/manifests/prod/wikijs/` 配下のマニフェスト一式で構成される。
2. Where Wiki.js の永続コンテンツ(アップロードファイル等)を保存する必要がある場合, the Wiki.js Deployment shall 既存サービス(roundcube-db等)と同じ `local-path` StorageClass の PersistentVolumeClaim をマウントする。
3. The Wiki.js Deployment shall `requarks/wiki:2` イメージの specific タグ(latestではなく固定バージョン)を使用する。
4. When Wiki.js Pod が起動する, the Wiki.js Deployment shall CNPG Postgres クラスタへの接続情報を環境変数(`DB_TYPE`, `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASS`, `DB_NAME`)経由で受け取る。
5. The Wiki.js Deployment shall CPU/メモリの requests と limits を明示的に設定する(実測値: プロセス単体 ~140MB、初期値は authentik-server 等の既存パターンを参考にした余裕を持つ値とする)。

### Requirement 2: 専用 CNPG Postgres クラスタ
**Objective:** As an インフラ担当者, I want Wiki.js専用のCNPG Postgresクラスタを既存クラスタ(authentik-db/directus-db等)と同じパターンで新設する, so that DBの障害分離とバックアップ運用が既存の仕組みに統一される

#### Acceptance Criteria
1. The CNPG Cluster リソース shall `gitops/manifests/prod/wikijs/db-cluster.yaml` に定義され、`instances: 1`(シングルノード運用に合わせる)とする。
2. The CNPG Cluster リソース shall `bootstrap.recovery` を用いたDR方式に統一する(既存クラスタとの一貫性維持)。
3. The CNPG Cluster リソース shall Hetzner Object Storage 上の専用パス(`s3://aramakisai-backups/cnpg/wikijs-db`)へバックアップする `barmanObjectStore` 設定を持つ。
4. The CNPG Cluster リソース shall `retentionPolicy` を明示的に設定する。
5. The ScheduledBackup リソース shall `schedule` フィールドに秒付き6フィールドcron形式(例: `0 0 2 * * *`)を使用する。
6. If `resources.limits.memory` の初期値でバックアップ処理時(barman-cloud-backup/wal-archive)にOOMが発生した場合, the CNPG Cluster リソース shall directus-dbと同様の対応(limit引き上げ)を適用できるようコメントで運用上の注意を明記する。

### Requirement 3: Authentik OIDC によるSSO統合
**Objective:** As a 委員会メンバー, I want Authentikアカウントで Wiki.js にログインする, so that サービスごとに別アカウントを覚える必要がなくなる

#### Acceptance Criteria
1. The Authentik OIDC Provider/Application 定義 shall 既存パターン(`terraform/authentik_apps.tf` に準拠した新規 `.tf` ファイルまたは追記)でTerraform管理される。
2. The Wiki.js 側の認証ストラテジー設定 shall Terraform ではなく、宣言的 Kubernetes Job による `authentication` テーブルへの `ON CONFLICT (key) DO UPDATE` SQL UPSERT で管理される。
3. The UPSERT Job shall `config` 列に `clientId`/`clientSecret`/`authorizationURL`/`tokenURL`/`userInfoURL`/`issuer`/`emailClaim`/`displayNameClaim`/`mapGroups`/`groupsClaim` を含む JSON を書き込む。
4. The UPSERT Job shall `domainWhitelist` と `autoEnrollGroups` 列に `{"v": [...]}` 形式でラップされたJSONを書き込む。
5. The UPSERT Job shall Infisical経由の ExternalSecret から `clientId`/`clientSecret` を環境変数として受け取り、平文をマニフェストに含めない。
6. The Wiki.js 側の `local` 認証ストラテジー shall 無効化されず、管理者アカウントのフォールバックログイン手段として残される。
7. When UPSERT Job が正常終了する, the ArgoCD PostSync hook 構成 shall Wiki.js Deployment の rollout restart を自動的にトリガーする(Directus schema-apply後のrollout restartパターンと同様)。
8. The UPSERT Job shall `ttlSecondsAfterFinished` を設定し、完了後に自動でクリーンアップされる。
9. If Wiki.jsのメジャーバージョンアップ等により `authentication` テーブルの列構成が本Job作成時点で想定したスキーマと一致しない場合, the UPSERT Job shall SQL UPSERTを実行せず、Discord Ops Webhook(`DISCORD_OPS_WEBHOOK_URL`)へシグナル通知を送信した上で失敗として終了する(既存のrollout restartをトリガーしないゲート条件に従う)。

### Requirement 4: シークレット管理
**Objective:** As an インフラ担当者, I want Wiki.js関連のシークレットを既存のInfisical/ESOパターンで管理する, so that マニフェストに平文シークレットが混入しない

#### Acceptance Criteria
1. The Wiki.js DB接続パスワード shall `ExternalSecret` リソース経由でInfisicalから注入される。
2. The Authentik OIDC Client Secret shall `ExternalSecret` リソース経由でInfisicalから注入される。
3. The Infisical シークレット一覧 shall `WIKIJS_DB_PASSWORD` および `WIKIJS_OIDC_CLIENT_SECRET` を含むよう更新される。
4. The UPSERT Job shall スキーマ不一致シグナル通知のため、既存キー `DISCORD_OPS_WEBHOOK_URL`(新規作成なし、Falcosidekick等と共有)を `ExternalSecret` 経由で環境変数として受け取る。
5. The `WIKIJS_DB_PASSWORD` の初期値 shall `openssl rand -base64 32`(またはこのリポジトリの既存パターン、`secrets.tfvars.example`の`cf_tunnel_secret`と同様)等の暗号論的に安全な乱数生成コマンドで生成され、辞書的に推測可能な値を用いない。

### Requirement 5: 外部公開
**Objective:** As a 委員会メンバー, I want ブラウザから wiki.aramakisai.com にアクセスする, so that Tailscaleなしで社内Wikiを閲覧・編集できる

#### Acceptance Criteria
1. The `terraform/tunnel.tf` 内 `cloudflare_zero_trust_tunnel_cloudflared_config` の `ingress_rule` shall `wiki.aramakisai.com` を Wiki.js Service(`http://wikijs.prod.svc.cluster.local:<port>`)へ直接ルーティングする設定を追記する(既存サービスと同じくk8s Ingressリソースは使用しない)。
2. The `terraform/dns.tf` 内 `cloudflare_record` shall `wiki` サブドメインのCNAMEレコードを`local.tunnel_cname`へ向けて追記する(既存 `api`/`vault`/`presence`等と同じパターン)。

### Requirement 6: リソース制約下での安全性
**Objective:** As an インフラ担当者, I want Wiki.js導入後もノード全体のメモリ逼迫や既存サービスの可用性低下を起こさない, so that 単一ノードクラスタの安定運用を維持できる

#### Acceptance Criteria
1. The Wiki.js 関連リソース(App + DB)合計 shall 導入前のノード実質空きメモリ(~1.6-1.8GB)の範囲内に収まるサイジングとする。
2. When Wiki.js デプロイ後にノードメモリ使用率を確認する, the インフラ担当者 shall `make kubectl top nodes`/`free -h` で実測し、既存ワークロードへの影響がないことを確認する。
3. If 実測メモリ使用量がrequests/limitsの想定を大きく超過した場合, the インフラ担当者 shall リソース制限の見直しを行う。

### Requirement 7: ドキュメント同期
**Objective:** As an インフラ担当者, I want Wiki.js導入に伴う変更を既存ドキュメントに反映する, so that プロジェクトメモリが最新状態を保つ

#### Acceptance Criteria
1. When 実装が完了する, the ドキュメント同期プロセス shall `README.md` のデプロイされるサービス一覧に Wiki.js を追加する。
2. When 実装が完了する, the ドキュメント同期プロセス shall `.kiro/steering/tech.md` のInfisicalシークレット一覧に `WIKIJS_DB_PASSWORD`/`WIKIJS_OIDC_CLIENT_SECRET` を追記する。

### Requirement 8: 実装時の公式ドキュメント・ソース精査
**Objective:** As an インフラ担当者, I want 実装フェーズ中もWiki.js公式ドキュメント/ソースコードを都度精査する, so that research.md調査時点では見えなかった不測の挙動・破壊的変更を実装中に検知できる

#### Acceptance Criteria
1. Wiki.jsはCNPG/Directus等の既存統合先と比べ運用実績が浅いソフトウェアであるため、the 実装担当者 shall `auth-strategy-configmap.yaml`のSQL、`deployment.yaml`の環境変数、`db-cluster.yaml`のバックアップ設定等、research.mdで調査済みの前提に依存するコンポーネントを実装する際、着手前に該当する公式ドキュメント/ソースコードを再確認する。
2. Where 公式ドキュメントサイト(`docs.requarks.io`)がJavaScriptレンダリングのSPAでありコンテンツ取得ツールが本文を取得できない場合, the 実装担当者 shall 代替としてMarkdownソースリポジトリ`github.com/requarks/wiki-docs`(`raw.githubusercontent.com/requarks/wiki-docs/main/<path>.md`)、またはアプリ本体リポジトリ`github.com/requarks/wiki`のソースコードを直接参照する。
3. If 実装中にresearch.md/design.mdの前提(`authentication`テーブルのスキーマ、config.yml解決タイミング、strategyキャッシュ挙動等)と実際のWiki.js挙動に差異を発見した場合, the 実装担当者 shall design.mdの該当箇所を更新した上で実装を継続する(前提の誤りを実装で黙って回避しない)。
