# Requirements Document

> **⚠️ 2026-06-11: 本specは cancelled (supersededed)**
> `mail-server-migration` spec により Stalwart メールサーバー自体が Docker Mailserver (DMS) に
> 置き換えられたため、本spec が前提としていた「Stalwart + Dex」構成は不要になった。
> DMS では Dovecot が Authentik の UserInfo エンドポイントを直接検証することで OAUTHBEARER を
> 実現しており (`gitops/manifests/prod/mailserver/configmap.yaml`)、Dex は導入されていない。
> 本ドキュメントは調査経緯の記録として保持する。実装作業は `mail-server-migration` を参照。

## はじめに

Stalwart メールサーバーの認証を Authentik を唯一のユーザーソースとして統一する。
従来メールクライアント（Thunderbird・Apple Mail・Outlook 等）向けの LDAP パスワード認証と、
Roundcube ウェブメール向けの OAUTHBEARER 認証を、単一の Authentik インスタンスから提供し
すべてのメジャークライアントで同時に動作させる。

**OAUTHBEARER の実装方針**: Stalwart と Authentik の直接 OIDC 連携では OAUTHBEARER が動作しないことが確認済みのため、
**Dex を OIDC ブローカー**として介在させる。Roundcube は Dex から OAuth2 トークンを取得し、
Stalwart は Dex の OIDC エンドポイントに対してトークンを検証する。
Dex はアップストリームとして Authentik OIDC に接続し、ユーザー認証を委譲する。

K3s/ArgoCD/VolSync で構成されたクラスター上で GitOps ワークフローに従って管理・運用できること。

## スコープ

- **対象**: Dex デプロイ、Stalwart 認証設定（Dex への切り替え）、Roundcube OAuth2 設定（Dex への切り替え）、ArgoCD PostSync Job
- **対象外**: メール送受信機能そのもの、DKIM/SPF/DMARC 設定、VolSync バックアップスケジュール設定
- **隣接システム**: Authentik (IdP / Dex のアップストリーム)、Dex (OIDC ブローカー)、Roundcube (Webmail クライアント)、ArgoCD (GitOps)、Infisical/ESO (シークレット管理)

---

## Requirements

### Requirement 1: Authentik 単一ソース認証基盤

**Objective:** インフラ管理者として、Stalwart のユーザー認証を Authentik に一元化したい。そうすることでユーザー管理が単一箇所になり、パスワード変更・アカウント停止が即時すべてのメールアクセスに反映される。

#### Acceptance Criteria

1. The Stalwart shall ユーザー認証バックエンドとして Authentik LDAP Outpost (`authentik-ldap` ディレクトリ) と Authentik OIDC (`authentik-oidc` ディレクトリ) の両方を持つ設定で起動する
2. When Authentik でユーザーのパスワードが変更された場合、the Stalwart shall 次回の認証リクエストから新しいパスワードを受け入れる
3. When Authentik でユーザーアカウントが無効化された場合、the Stalwart shall そのアカウントの IMAP/SMTP 接続を拒否する
4. The Stalwart shall `authentik-ldap` ディレクトリの bindSecret を `STALWART_JOB_TOKEN` 環境変数から取得し、平文でマニフェストに書かない
5. If Authentik LDAP Outpost が一時的に応答しない場合、the Stalwart shall 進行中の認証を拒否し、既に確立済みのセッションは維持する

---

### Requirement 2: LDAP パスワード認証（従来メールクライアント対応）

**Objective:** 一般メンバーとして、Thunderbird・Apple Mail・Outlook などの従来メールクライアントから Authentik アカウントのパスワードで IMAP/SMTP 接続したい。そうすることでウェブブラウザを使わずにメールを送受信できる。

#### Acceptance Criteria

1. When IMAP クライアントが IMAPS (port 993) に PLAIN/LOGIN 認証で接続した場合、the Stalwart shall `authentik-ldap` ディレクトリに対してパスワードを検証し、認証が成功したセッションを確立する
2. When SMTP クライアントが Submission (port 587) または SMTPS (port 465) に PLAIN/LOGIN 認証で接続した場合、the Stalwart shall `authentik-ldap` ディレクトリに対してパスワードを検証し、メール送信を許可する
3. The Stalwart shall `Domain.directoryId` に `authentik-ldap` ディレクトリを設定し、ドメインレベルのパスワード認証バックエンドとして使用する
4. When 存在しないユーザー名またはパスワードが送信された場合、the Stalwart shall 認証失敗を返し、エラー詳細をログに記録する
5. The Stalwart shall IMAP/SMTP のパスワード認証において STARTTLS または TLS を必須とし、平文接続での認証情報送信を拒否する

---

### Requirement 3: Dex OIDC ブローカーのデプロイ

**Objective:** インフラ管理者として、Stalwart の OAUTHBEARER 検証用に Dex を K3s クラスター内に OIDC ブローカーとしてデプロイしたい。そうすることで Authentik の OIDC トークンを Stalwart が受け入れられる形式に変換できる。

#### Acceptance Criteria

1. The Dex shall K3s クラスターの `prod` namespace に Deployment として稼働し、ArgoCD が GitOps で管理する
2. The Dex shall Authentik (`https://idp.aramakisai.com`) をアップストリーム OIDC コネクターとして設定し、ユーザー認証を委譲する
3. The Dex shall クラスター内から到達可能な OIDC Discovery エンドポイント（`/.well-known/openid-configuration`）を提供する
4. The Dex shall Roundcube 用の OAuth2 クライアント（client_id / client_secret）を設定し、Roundcube からの認可リクエストを受け付ける
5. The Dex の client_secret shall Infisical + ESO 経由で管理され、マニフェストに平文で書かない
6. The Dex shall `offline_access` スコープをサポートし、Roundcube がリフレッシュトークンを取得できる

---

### Requirement 4: OAUTHBEARER 認証（Dex 経由 Roundcube 対応）

**Objective:** 委員会メンバーとして、ブラウザから webmail.aramakisai.com にアクセスしてメールを送受信したい。そうすることでメールクライアントを設定せずにどのデバイスからもメールを利用できる。

#### Acceptance Criteria

1. When Roundcube が Dex から取得した OAuth2 アクセストークンで IMAP に OAUTHBEARER 認証した場合、the Stalwart shall Dex の OIDC エンドポイントに対してトークンを検証し、認証を許可する
2. When Roundcube が SMTP (port 587) に OAUTHBEARER 認証でメール送信した場合、the Stalwart shall 同じ Dex OIDC ディレクトリでトークンを検証しメール送信を許可する
3. The Stalwart の `dex-oidc` ディレクトリは Dex の issuerUrl と requireAudience で設定され、`Authentication.directoryId` に設定される
4. When 期限切れまたは無効な OAuth2 トークンが送信された場合、the Stalwart shall OAUTHBEARER 認証を失敗させ、Roundcube がトークン更新フローを実行できるよう適切なエラーを返す
5. The Roundcube shall `oauth_provider` として Dex のエンドポイントを指定し、Authentik への直接 OAuth2 接続は使用しない

---

### Requirement 5: LDAP/OAUTHBEARER 認証の共存

**Objective:** インフラ管理者として、LDAP パスワード認証と Dex 経由 OAUTHBEARER 認証が単一の Stalwart インスタンスで同時に機能する設定を確立したい。そうすることで従来クライアントとウェブメールの両方を同時にサポートできる。

#### Acceptance Criteria

1. The Stalwart shall `Authentication.directoryId = #dex-oidc`（グローバル認証: OAUTHBEARER 検証用）と `Domain.directoryId = #authentik-ldap`（ドメイン認証: パスワード検証用）を同時に設定した状態で動作する
2. When IMAP クライアントが OAUTHBEARER で接続した場合、the Stalwart shall `Authentication.directoryId` (Dex OIDC) でトークンを検証する
3. When IMAP クライアントが PLAIN/LOGIN で接続した場合、the Stalwart shall `Domain.directoryId` (Authentik LDAP) でパスワードを検証する
4. The Stalwart shall 同一アカウント (`user@aramakisai.com`) に対して、LDAP パスワード認証と OAUTHBEARER 認証の両方のセッションが同時に存在できる
5. If Dex の設定が更新された場合、the Stalwart shall LDAP パスワード認証の動作に影響を与えない

---

### Requirement 6: ArgoCD PostSync Job による設定自動適用

**Objective:** インフラ管理者として、Stalwart の認証設定変更を GitOps ワークフロー（ArgoCD sync）で自動適用したい。そうすることで手動での stalwart-cli 実行が不要になり、設定の一貫性が保たれる。

#### Acceptance Criteria

1. When ArgoCD が stalwart Application を sync した場合、the PostSync Job shall `settings-update.ndjson` を Stalwart HTTP API に適用する
2. The PostSync Job shall `STALWART_ADMIN_SECRET` 環境変数（`stalwart-secrets` Secret 経由）を使って Stalwart に認証し、`Authentication.directoryId = authentik-oidc` の状態でも認証が成功する
3. The PostSync Job shall `settings-update.ndjson` 適用後に `Domain.directoryId` を `authentik-ldap` に動的に設定する（LDAP パスワード認証の維持）
4. When Stalwart が起動してから HTTP API が応答可能になるまでの間、the PostSync Job shall 最大 120 秒間リトライし続け、応答確認後に設定を適用する
5. If PostSync Job が失敗した場合、the PostSync Job shall エラーログを出力し、JobがFailed状態で残存することでデバッグを可能にする
6. The PostSync Job shall 設定適用後に `stalwart-0` Pod を削除し、StatefulSet による自動再起動でゲートウェイ設定をロードさせる
7. The PostSync Job shall `STALWART_RECOVERY_ADMIN` 環境変数が StatefulSet に常設されていることを前提とし、API キー方式は使用しない

---

### Requirement 7: VolSync バックアップとアカウント ID 整合性

**Objective:** インフラ管理者として、Stalwart の settings.ndjson を再適用した後もVolSync バックアップからのデータ復元でメールが正常に参照できることを保証したい。そうすることで DR 後にメールデータが消失するリスクを排除できる。

#### Acceptance Criteria

1. The Stalwart shall `Directory` の destroy + create（settings.ndjson 再適用）後に `stalwart-cli query Account` でアカウントが列挙できる状態になるまで、VolSync バックアップを信頼できるバックアップとして扱わない
2. When `settings.ndjson` が適用されて `Directory` が再作成された場合、the Stalwart shall 既存メールデータのアカウント ID と新しいディレクトリ設定のアカウント ID が一致している
3. The Stalwart の設定ドキュメント（settings-configmap.yaml）shall `Domain` の destroy は再適用時に実行しない旨を明示し、メールデータ消失リスクを防ぐ
4. While VolSync ReplicationSource が稼働中の場合、the Stalwart shall PVC への書き込みを正常に続け、バックアップと現在のデータが一致する

---

### Requirement 8: 管理者アクセス回復性

**Objective:** インフラ管理者として、`Authentication.directoryId = authentik-oidc` が設定されている状態でも確実に Stalwart 管理 API にアクセスできるようにしたい。そうすることで認証設定変更後に管理者ロックアウトが発生しない。

#### Acceptance Criteria

1. The Stalwart StatefulSet shall `STALWART_RECOVERY_ADMIN` 環境変数を常設し、`admin:$(STALWART_ADMIN_SECRET)` 形式で展開された Recovery Admin を常時有効にする
2. The Stalwart shall `STALWART_ADMIN_SECRET` が Infisical から ESO 経由で `stalwart-secrets` Secret に同期されており、Stakater Reloader が Secret 変更を検知して Pod を自動再起動する
3. When `STALWART_ADMIN_SECRET` が Infisical で更新された場合、the Stalwart shall Reloader による Pod 再起動後に新しいパスワードで Recovery Admin 認証が成功する
4. If Recovery Admin 認証が失敗した場合、the Stalwart shall ログに `"Unsupported credentials type"` 等の診断情報を出力し、問題の原因を特定できる
5. The Stalwart shall `STALWART_API_KEY` による API キー認証方式を使用せず、Recovery Admin 認証のみを PostSync Job の認証手段とする
