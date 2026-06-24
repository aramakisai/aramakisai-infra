# Requirements Document

## Project Description (Input)
smtpを設定できるサービスで設定していないものをauthentikの設定を流用して設定する

## Introduction
現在、Authentik は `mail.aramakisai.com:587` (ユーザー `noreply`, STARTTLS) への SMTP 接続をすでに稼働させ、メール送信を行っている。本仕様は、SMTP 送信機能を持つが現状未設定の他サービス（Vaultwarden, Directus）に対し、この実績ある SMTP リレー設定を流用して有効化することを目的とする。

## Boundary Context
- **In scope**: Vaultwarden（パスワードリセット・招待・通知メール）、Directus（パスワードリセット・ユーザー招待メール）への SMTP 設定追加
- **Out of scope**: Roundcube（ユーザー個別の IMAP/SMTP 資格情報で動作するため対象外）、room-presence（Discord Webhook のみで SMTP 機能を持たない）、ArgoCD（notifications controller 自体が未デプロイ）、mailserver（SMTP サーバー本体であり SMTP クライアント設定の対象外）
- **Adjacent expectations**: 新規 Infisical シークレットキーを追加した場合は `steering/tech.md` のシークレット一覧に追記する（[structure.md](.kiro/steering/structure.md) のドキュメント同期ルールに従う）

## Requirements

### Requirement 1: Vaultwarden の SMTP メール送信設定
**Objective:** As a 委員会管理者, I want VaultwardenがAuthentikと同じSMTPリレーを使ってメールを送信できるようにしたい, so that パスワードリセット・新規メンバー招待・セキュリティ通知メールが自動配信される

#### Acceptance Criteria
1. When Vaultwarden Service が起動する, the Vaultwarden Service shall Authentik が使用する SMTP リレー (`mail.aramakisai.com`, ポート587, ユーザー `noreply`) への接続設定を読み込む
2. Where SMTP 設定が有効化されている, the Vaultwarden Service shall STARTTLS を用いて SMTP サーバーに接続する
3. The Vaultwarden Service shall SMTP 認証情報を ExternalSecret 経由で Infisical から取得し、マニフェストに平文を含めない
4. While Authentik 用の SMTP 認証情報 (`noreply` アカウントのパスワード) が Infisical 上に既に存在する, the Vaultwarden Service shall 同一の認証情報ソースを参照し、値を重複登録しない
5. If 管理者が Vaultwarden 管理画面からテストメール送信を実行する, then the Vaultwarden Service shall メール送信に成功する
6. When 新規ユーザーが Organization に招待される, the Vaultwarden Service shall 招待メールを送信する

### Requirement 2: Directus の SMTP メール送信設定
**Objective:** As a 委員会管理者, I want DirectusがAuthentikと同じSMTPリレーを使ってメールを送信できるようにしたい, so that パスワードリセット・ユーザー招待メールが自動配信される

#### Acceptance Criteria
1. When Directus Service が起動する, the Directus Service shall `EMAIL_TRANSPORT=smtp` および Authentik が使用する SMTP リレー (`mail.aramakisai.com`, ポート587, ユーザー `noreply`) への接続設定を読み込む
2. Where SMTP 設定が有効化されている, the Directus Service shall STARTTLS/TLS を用いて SMTP サーバーに接続する
3. The Directus Service shall SMTP 認証情報を ExternalSecret 経由で Infisical から取得し、マニフェストに平文を含めない
4. While Authentik 用の SMTP 認証情報 (`noreply` アカウントのパスワード) が Infisical 上に既に存在する, the Directus Service shall 同一の認証情報ソースを参照し、値を重複登録しない
5. If 管理者が Directus 管理画面からパスワードリセットを実行する, then the Directus Service shall リセットメールを送信する
6. When 新規ユーザーが Directus にユーザー招待される, the Directus Service shall 招待メールを送信する

### Requirement 3: ドキュメント同期と動作確認
**Objective:** As a インフラ担当者, I want SMTP設定変更が正しく反映され記録されることを確認したい, so that 将来の運用者がシークレット構成と動作状況を正確に把握できる

#### Acceptance Criteria
1. When Vaultwarden または Directus に新規 Infisical シークレットキーが追加される, the Engineer shall `steering/tech.md` のシークレット一覧にキー名を追記する（値は含めない）
2. After Vaultwarden の SMTP 設定が ArgoCD で sync される, the Engineer shall Vaultwarden 管理画面からテストメール送信を実施し送信成功を確認する
3. After Directus の SMTP 設定が ArgoCD で sync される, the Engineer shall パスワードリセットメール送信を実施し送信成功を確認する
4. If メール送信がエラーになる, then the Engineer shall mailserver 側 (`SPOOF_PROTECTION` 等の送信者フィルタ) との不整合を確認する
