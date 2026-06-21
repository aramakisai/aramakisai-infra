# Requirements Document

## Introduction
本仕様書は、荒牧祭実行委員会のメンバー間でパスワードおよび資格情報を安全に共有・管理するための認証情報管理サービス（Vaultwarden）のデプロイに関する要件を定義する。

## Boundary Context
- **In scope**:
  - K3s クラスター上への認証情報管理サービス（コンテナ）のデプロイ
  - リレーショナルデータベース（PostgreSQL）クラスターの構築とサービスからの接続
  - データベースデータのオブジェクトストレージへの自動バックアップ
  - アタッチメント等のファイルデータを保存するための永続ストレージのプロビジョニングと、そのリモートバックアップ
  - 外部公開用リバースプロキシを介した `vault.aramakisai.com` ドメインでの外部アクセス（HTTPS、WebSocket対応）の提供
  - シークレット管理システムを介した、コンテナ設定およびシークレットの安全なインジェクション
  - 共有アカウント（SNS、Googleアカウント）や口座情報に対するロールベースアクセス制御（RBAC：閲覧、編集、使用）の定義
  - IdP（Authentik）とのシングルサインオン（SSO/OIDC）連携
- **Out of scope**:
  - クライアントアプリケーション（ブラウザ拡張機能やモバイルアプリ）自体のインストールと設定
- **Adjacent expectations**:
  - 外部公開用リバースプロキシプロバイダー（Cloudflare）でのDNS設定およびルーティング設定
  - シークレット管理システム（Infisical）における必要な環境変数やトークンの事前登録

## Requirements

### Requirement 1: ロールベースアクセス制御（RBAC）と共有
**Objective:** 管理者として、共有された資格情報（SNS、Googleアカウント、銀行口座など）へのアクセスをロールベースアクセス制御（RBAC）によって管理し、ユーザーが最小限の必要な権限（閲覧、編集、または自動入力のみ）だけを持つようにしたい。

#### Acceptance Criteria
1. The credential management system shall 共有されたシークレットを論理的なコレクション（例：「SNSアカウント」「Googleアカウント」「口座情報」）にグループ化することをサポートする。
2. The credential management system shall 各コレクションに対して、ユーザーまたはグループに特定のアクセス権限（閲覧のみ、自動入力のみ、または編集）を割り当てることをサポートする。
3. While ユーザーが特定のコレクションに対して「閲覧のみ」の権限を持っている間, the system shall ユーザーがそのコレクション内のシークレットを編集または削除することを防止する。
4. While ユーザーが特定のコレクションに対して「自動入力のみ」の権限を持っている間, the system shall ユーザーが自動入力のために資格情報を使用することを許可するが、ユーザーインターフェースからプレーンテキストのパスワードや詳細情報を非表示にする。

### Requirement 2: デプロイとストレージ
**Objective:** 管理者として、資格情報管理サービスを永続データベースおよび永続ストレージとともにデプロイし、ユーザーの資格情報やアセットが安全に保存され、復旧可能にしたい。

#### Acceptance Criteria
1. The credential management service shall データベースレコードを永続的なリレーショナルデータベースに保存する。
2. While 本サービスが稼働している間, the service shall データベース以外のアセット（アタッチメントや組織アイコンなど）を保存するために、永続ボリュームをマウントする。
3. The database system shall データベースの状態をリモートのオブジェクトストレージに自動的にバックアップする。
4. The volume backup mechanism shall 定期的なスケジュールで永続ボリュームをリモートストレージにバックアップする。

### Requirement 3: ネットワーキングと外部アクセス
**Objective:** ユーザーとして、資格情報管理システムにHTTPS経由で安全にアクセスし、リアルタイムで同期を行うことで、通信が暗号化され、更新が即座に反映されるようにしたい。

#### Acceptance Criteria
1. When クライアントが本サービスのホスト名にアクセスしたとき, the external proxy shall 通信を本サービスに転送する。
2. The external proxy shall リアルタイム同期を有効にするためにWebSocket接続をサポートする。
3. The external proxy shall すべての着信リクエストに対してHTTPS接続を強制する。

### Requirement 4: シークレット管理と設定
**Objective:** 管理者として、すべての機密性の高い資格情報をシークレット管理システムから動的に読み込ませることで、資格情報がハードコードされないようにしたい。

#### Acceptance Criteria
1. The secret management system shall データベース接続情報や管理者トークンなどの機密設定をクラスターに安全に同期する。
2. The service container shall 同期されたシークレットから環境変数として設定を読み込む。

### Requirement 5: アクセス制御とシステムセキュリティ
**Objective:** 管理者として、ユーザー登録とシステム管理を厳格に制御し、システムへの不正アクセスを防止したい。

#### Acceptance Criteria
1. The credential management system shall 一般ユーザーの新規登録（サインアップ）を無効にする。
2. While 一般登録が無効化されている間, the system shall 管理者からの招待経由でのみ新規ユーザーの登録を許可する。
3. When 管理者が管理者ポータルにアクセスしたとき, the system shall セキュアな管理者トークンによる認証を要求する。

### Requirement 6: SSO/OIDC 連携（Authentik）
**Objective:** ユーザーとして、既存の Authentik アカウントを使って Vaultwarden にログインしたい。管理者として、ユーザーのアクセスを Authentik 経由で一元管理したい。

#### Acceptance Criteria
1. The credential management system shall Authentik を OpenID Connect Provider として使用し、SSO ログインを有効にする。
2. When ユーザーがログイン画面で SSO を選択したとき, the system shall Authentik の認可フローにリダイレクトし、認証後に Vaultwarden に自動ログインする。
3. The system shall SSO ログイン時に既存の Vaultwarden アカウントとメールアドレスで紐付ける（`SSO_SIGNUPS_MATCH_EMAIL=true`）。
4. When SSO 連携が有効な間, the system shall パスワードログインを無効化し、SSO のみを許可する（`SSO_ONLY=true`）。
5. The system shall SSO 連携に必要なクライアント認証情報（client_id, client_secret）を Infisical 経由で安全に注入する。
6. While Vaultwarden Collection 権限は Vaultwarden 内部で管理される, the system shall Authentik グループを RBAC の真の情報源（SSOT）として扱い、Vaultwarden Collection 名と Authentik グループ名を一致させて手動で権限をマッピングする。
