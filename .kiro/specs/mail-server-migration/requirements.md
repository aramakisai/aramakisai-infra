# Requirements Document

## はじめに

現在運用中の Stalwart メールサーバーを廃止し、より堅牢で枯れたメールスタックである **Docker Mailserver (DMS)** に移行する。
Stalwart は設定が内部データベースに保持され、APIや CLI を用いた非宣言的な構成管理（ArgoCD PostSync Job など）が必要なため、復旧が難しく AI の誤操作を招きやすい課題があった。
また、Authentik との OIDC/OAUTHBEARER 連携が極めて複雑で、プロキシとして Dex を介在させるなど運用の難易度が高くなっていた。

本移行プロジェクトでは、DMS を採用して設定をすべて Kubernetes マニフェスト（ConfigMap / Secret）で完結させる「完全宣言的」な構成を実現する。
認証は Authentik LDAP Outpost に直接接続する PLAIN/LOGIN 認証と、Roundcube ウェブメール向けの OAUTHBEARER 認証（Authentik OIDC トークン検証）を Dovecot 上で同時に提供し、一元的なユーザー管理を実現する。
さらに、DKIM 送信署名キーや DNS レコードを Terraform で管理するように戻し、スパムおよびウイルス対策（Rspamd, ClamAV 等）を有効化した堅牢なメールサーバーを構築する。
また、Hetzner の Port 25 送信制限を回避するため、既存の Resend SMTP リレー設定はそのまま DMS へ引き継ぐ。
移行完了後は、Stalwart に関連する不要なマニフェスト、PVC、Authentik 設定、環境変数をすべて削除し、クリーンなインフラ状態を確立する。

## スコープ

- **対象**:
  - Stalwart 関連リソース of 廃止（StatefulSet, Service, Ingress, settings-apply-job 等）
  - Docker Mailserver (DMS) の新規デプロイとマニフェスト定義（StatefulSet, Service, ConfigMap, ExternalSecret）
  - DMS と Authentik LDAP Outpost の連携設定（一般メールクライアント向け）
  - DMS (Dovecot) の OAUTHBEARER / XOAUTH2 認証設定（Roundcube 向け）
  - Roundcube と DMS の OAUTHBEARER 連携（Authentik ログインセッションの維持）
  - DMS 向けの VolSync バックアップ・リストア設定（既存の `stalwart-data` PVC もしくは新規の `mailserver-data` PVC への restic 適用）
  - **Resend SMTP リレー設定の引き継ぎ**（外部宛送信のリレー）
  - **旧 Stalwart 環境および関連設定のクリーンアップ**:
    - 旧 `stalwart-data` PVC および VolSync バックアップデータの削除
    - Authentik 内の Stalwart 専用不要設定のクリーンアップ (Dex が不要になる場合は Dex も削除)
    - Infisical 内の不要になった Stalwart 専用環境変数の削除
  - **Terraform (IaC)** による DNS レコードの更新：
    - 新しい DKIM 公開鍵 TXT レコードの宣言的定義
    - Stalwart 特有の不要な SRV レコード（JMAP, CalDAV, CardDAV 等）の削除
    - SPF/DMARC レコードの調整
- **対象外**:
  - Authentik 自体の再構築やユーザーデータの移行
  - メールデータの過去ログ移行
  - Cloudflare Tunnel / DNS レコードの根本的な変更（ドメイン名 `aramakisai.com` および `webmail.aramakisai.com` は維持）

---

## Requirements

### Requirement 1: 完全宣言的なメールサーバー構成

**Objective:** インフラ管理者として、メールサーバーのすべての構成項目を Git 経由で管理したい。そうすることで、手動操作や PostSync API ジョブなどのトリッキーな仕組みを排除し、再現性の高い環境を維持できる。

#### Acceptance Criteria
1. The Mailserver shall データベースや外部 API を介さずに、Kubernetes の ConfigMap、Secret、およびマウントされたファイルのみで動作設定が完結する。
2. The Mailserver shall 設定ファイルの追加や変更があった際、ArgoCD の標準的な同期（Sync）機能のみで構成が更新される（PostSync Job による CLI 実行等は行わない）。
3. The Mailserver shall 状態を持たない設定（ドメイン一覧、LDAP 連携定義、Postfix/Dovecot のポリシーなど）を Git 側のファイルとして完全に定義できる。

---

### Requirement 2: Authentik LDAP 直結による認証の一元化

**Objective:** 従来メールクライアントを使用する一般メンバーとして、自身のメールアプリから Authentik パスワードを入力して IMAP/SMTP 接続を行いたい。

#### Acceptance Criteria
1. The Mailserver shall クラスター内の Authentik LDAP Outpost (`ldap://authentik-ldap-outpost.prod.svc.cluster.local:389`) に直接接続してユーザーのパスワード検証を行う。
2. When メールクライアント（Thunderbird 等）が IMAPS (ポート 993) または Submission (ポート 587) で PLAIN / LOGIN 認証で接続した際、the Mailserver shall Authentik LDAP に対して認証情報を照合し、許可されたユーザーのみアクセスを認める。
3. The Mailserver の LDAP bind 用シークレット（AUTHENTIK_LDAP_OUTPOST_TOKEN）shall Infisical + ESO（ExternalSecret）経由で安全に供給され、マニフェストに平文で露出しない。

---

### Requirement 3: Roundcube (Authentik セッション) 向けの OAUTHBEARER 認証サポート

**Objective:** 委員会メンバーとして、Authentik のシングルサインオン（SSO）セッションを利用して Roundcube にログインし、パスワードを再入力することなくシームレスにメールの閲覧・送受信を行いたい。

#### Acceptance Criteria
1. The Roundcube shall 引き続き Authentik OIDC / OAuth2 ログインを利用し、ログイン成功時に取得したアクセストークンを用いて DMS へ接続する。
2. The Mailserver (Dovecot および Postfix) shall 認証メカニズムとして **OAUTHBEARER** および **XOAUTH2** をサポートし、Roundcube から渡されたアクセストークンを Authentik OIDC エンドポイント（UserInfo または Introspection エンドポイント）に照合して検証する。
3. When トークン検証が成功した場合、the Mailserver shall トークン内のメールアドレスに対応するメールボックスへのアクセス（IMAP）およびメールの送信（SMTP）を許可する。

---

### Requirement 4: 災害復旧 (DR) およびリストアプロセスの簡素化

**Objective:** インフラ管理者として、ノード障害やデータ消失時に、バックアップからメールデータを迅速かつ確実に復旧したい。

#### Acceptance Criteria
1. The Mailserver のメールデータ（Maildir 形式）shall 単一の PVC（例: `mailserver-data`）に集約され、VolSync / restic を用いて Backblaze B2 へ定期的にバックアップされる。
2. When クラスター復旧時、the Mailserver shall PVC のリストア（ReplicationDestination）が完了した後に Pod を起動するだけで、追加の設定スクリプト実行やデータベースのインポートなしに完全復旧する。
3. The Mailserver shall RocksDB のような複雑なデータベース構造に依存せず、ディスク上の標準的な Maildir ディレクトリとプレーンなテキスト設定ファイルのバックアップのみでリストア可能とする。

---

### Requirement 5: DKIM / SPF / DMARC レコードの IaC 管理 (Terraform)

**Objective:** インフラ管理者として、メールの到達性を確保しつつ、送信ドメイン認証に必要な DNS レコードをすべて Terraform 上で宣言的に定義・管理したい。

#### Acceptance Criteria
1. The Terraform configuration shall DMS で生成した DKIM 公開鍵の TXT レコードを `dns.tf` に定義し、Cloudflare DNS に自動適用する。
2. Stalwart の自動 DNS 管理機能によって登録されていた不要な DNS レコード（JMAP、CalDAV、CardDAV の SRV レコードなど、DMS に不要なもの）は、Terraform の差分検出・適用によりクリーンアップされる。
3. The DKIM 秘密鍵ファイル shall クラスター内に安全に配備され（Secret 等で管理）、DMS が送信メールに自動で署名を行える状態にする。

---

### Requirement 6: セキュリティおよびスパム/ウイルス対策の統合

**Objective:** インフラ管理者および一般メンバーとして、受信メールに含まれるスパムやコンピュータウイルスを検知・フィルタリングし、サーバーのセキュリティを維持したい。

#### Acceptance Criteria
1. The Mailserver shall **Rspamd**（または同等のセキュリティエンジン）を有効化し、受信メールのスパムスコア判定、SPF / DKIM / DMARC レコードの検証を自動で行う。
2. The Mailserver shall ウイルススキャンエンジン（**ClamAV** 等）の有効化オプションをサポートする。ただし、ノードのリソース（メモリ）制限に応じて有効/無効を切り替え可能な設計とする。
3. The Mailserver Pod shall 適切な CPU/メモリのリソース制限（limits）および要求（requests）が設定され、スパム/ウイルスチェック実行時でも他のインフラコンポーネント（Authentik 等）の安定動作を妨げない（ノードスペックは CX33: 4vCPU / 8GB RAM とする）。

---

### Requirement 7: Resend による送信メールリレーの引き継ぎ

**Objective:** インフラ管理者として、Hetzner VPS の Port 25 送信ポート制限を回避しつつ、信頼性の高い経路で外部宛のメールを送信したい。

#### Acceptance Criteria
1. When DMS が外部ドメイン宛てのメールを送信しようとする際、the Mailserver shall 直接送信せず、すべて `smtp.resend.com:587` を経由する SMTP リレー（relayhost）として送信する。
2. The Mailserver shall Resend の SMTP 認証情報（ユーザー名 `resend`、パスワードは `RESEND_API_KEY` 環境変数）を使用してリレー接続を確立する。
3. The Resend API キー（`RESEND_API_KEY`）shall Infisical + ESO 経由で安全に供給され、DMS の Secret から環境変数として取得する。

---

### Requirement 8: 旧 Stalwart 環境のクリーンアップ

**Objective:** インフラ管理者として、インフラ構成内の技術的負債や不要になった設定を完全に排除し、リソースの無駄遣いと AI の混乱を防ぎたい。

#### Acceptance Criteria
1. The GitOps configuration shall `gitops/manifests/prod/stalwart/` 配下の古いマニフェスト、ArgoCD Application 定義（`apps/prod/stalwart.yaml`）など、旧 Stalwart に直接関連するリソース定義をすべて削除する。
2. The Kubernetes cluster shall 移行完了後に古い `stalwart-data` PVC、および旧 Stalwart 用の VolSync Backup / Restore 用リソースを安全に削除している。
3. The Authentik configuration shall Stalwart の OIDC directoryID 制限を迂回するために導入されていた Dex 関連の構成や、不要になった Stalwart 専用 OAuth クライアント設定をクリーンアップする。
4. The Infisical configuration shall `STALWART_ADMIN_SECRET` など、DMS で使用しない Stalwart 専用の環境変数を安全に削除できる状態にする。
