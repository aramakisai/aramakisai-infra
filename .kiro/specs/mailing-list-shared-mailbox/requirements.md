# Requirements Document

## Project Description (Input)
DMS(Docker Mailserver)を個人メール運用からメーリングリスト(ML)共有メールボックス運用へ移行する。現状はAuthentik LDAPグループ(mail+discord_role_ids属性、Discordロール連動)を使い、ML宛メールを各メンバー個人のpersonal mailboxへfan-out配送している(ldap-groups.cfのspecial_result_attribute=member+leaf_result_attribute=mailハック、2026-06-11本番検証済み)。これを「個人メールはほぼ不要、MLへのアクセス制御のみで十分」という方針に変更し、ML宛アドレス(企画/会計/出店/出演/広報/管理者/総務の7件、ライブLDAPで実在確認済み)それぞれを「複数人がDiscordロール経由のLDAPグループ所属に基づいて動的にログイン・読み書き・送信できる共有メールボックス」として運用する。

具体的要件:

1. 各ML宛アドレスは専用のメールボックスを持つAuthentik User(mail属性=ML宛アドレス、ak-active=true、パスワードは誰にも配布せず直接ログイン非使用)として作成する。配送はLDAP_QUERY_FILTER_USER経由でそのユーザーのメールボックスへ直接行い、旧来のLDAP_QUERY_FILTER_GROUPによるvirtual_alias_maps fan-out展開は廃止する((|)に設定して無効化)。

2. 受信アクセス制御: Authentik LDAP Outpostがユーザーエントリに既にmemberOf属性(所属グループのDN一覧)をネイティブで露出していることをldapsearchで確認済み。これをDovecotのuserdb LDAP attrsマッピング(DOVECOT_USER_ATTRSにmemberOf=groupsを追加)でDovecotのuserdb groups extra fieldとして取り込み、Dovecot標準のACLプラグイン(group:<グループDN>形式のACL)+ sharedネームスペース機能で、各ML共有メールボックスに対応するLDAPグループのメンバーだけが自分自身の資格情報でログインしてShared名前空間経由でアクセスできるようにする。Discordロールが外れてLDAPグループから外れれば次回ログインから自動的にアクセス不可になる(本来のアクセス制御)。

3. 送信時のなりすまし防止: DMS公式の組み込み機能であるLDAP_QUERY_FILTER_SENDERS(Postfixのsmtpd_sender_login_mapsに直結)を新設し、対応するLDAPグループのmember展開(既存ldap-groups.cfと同じ特殊クエリパターン: special_result_attribute=member, leaf_result_attribute=mail, timeout=30が必要、新規ldap-senders.cfとして実装)を流用して、ML宛Fromアドレスでの送信を許可されたグループメンバーのみに制限する(reject_sender_login_mismatch)。

4. IMAP/SMTP AUTHのログインをユーザー名のみ(例: usernameだけでusername@aramakisai.invalidとして認証)で行えるようにする。Dovecotのグローバル設定auth_username_format = %n@aramakisai.com一行で実現可能と判明している(フルアドレス入力にも後方互換)。これはSASL認証後にPostfixへ報告される正規ユーザー名をsmtpd_sender_login_mapsの比較対象と一致させる効果もある。 <!-- confidential:allow -->

5. メモリチューニング: kubectl topで実測した結果、mailserver podは512Mi request/1Gi limitに対し実使用量513Mi。ps auxでamavisプロセスが約138MB消費しているが、DMS公式ドキュメントはENABLE_RSPAMD=1の場合ENABLE_AMAVIS=0にすることを明記推奨しており、現在の構成はこの推奨設定が未適用だった。ENABLE_AMAVIS=0を追加し、ロールアウト後の実測値を見てresources.requests/limitsを再調整する(目安: request 384Mi/limit 640Mi、要実測確定)。

6. Roundcube(webmail)は削除せず維持する方針(個人INBOXに加えShared名前空間のML共有メールボックスもブラウザで一覧・送受信できるため、今回の運用に有効)。

制約・前提:
- Stalwart Mail Serverへの移行は過去に検証して認証障害(STALWART_API_KEY消失、OIDC backend非対応のcredential type等)で断念した経緯があり、情報不足・宣言的アーキテクチャでないことを理由に今回は完全に対象外と確認済み。Docker Mailserver + Dovecot標準機能の範囲内で実現する。
- 本番環境にステージングが存在せず、過去にLDAP timeoutによるメール消失やStalwart認証障害など複数回の本番障害があるため、Dovecot ACL/sharedネームスペースの正確な構文は実装時に公式Wiki/ドキュメントで最終確認し、まずpr@aramakisai.com 1件のみで段階的に検証してから残り6件のMLに展開する設計とする。 <!-- confidential:allow -->
- 触る予定のファイル: terraform/authentik_ldap.tf(またはML用新規ファイル、7件分のauthentik_userリソース+random_password)、gitops/manifests/prod/mailserver/statefulset.yaml(LDAP_QUERY_FILTER_GROUP/SENDERS env var、DOVECOT_USER_ATTRS、ENABLE_AMAVIS、configmap mount差し替え)、gitops/manifests/prod/mailserver/configmap.yaml(ldap-groups.cf削除、ldap-senders.cf新設、dovecot.cfへのacl plugin/sharedネームスペース/auth_username_format追加、各MLメールボックスのdovecot-acl制御ファイル)。

## はじめに

実行委員会の Docker Mailserver (DMS) は現在、Authentik LDAP グループ(`mail` + `discord_role_ids` 属性、Discord ロール連動)を使い、ML 宛メールを各メンバー**個人の**メールボックスへ fan-out 配送するモデルで運用している(2026-06-11 本番検証済みの `ldap-groups.cf` グループ展開ハック)。

しかし実態として個人メールはほとんど使われておらず、必要なのは「企画・会計・出店・出演・広報・管理者・総務」の 7 件の ML に対するアクセス制御のみである。本仕様では、各 ML 宛アドレスを「複数人が Discord ロール連動の LDAP グループ所属に基づいて動的にログイン・読み書き・送信できる共有メールボックス」として再構成し、合わせてログイン UX の改善とメモリ使用量の削減を行う。

単独メンテナー運用かつ本番環境にステージングが存在せず、過去に LDAP タイムアウトによるメール消失や Stalwart 認証障害など複数回の本番障害が発生している点を踏まえ、最小リスクで段階的に移行することを前提とする。

## Boundary Context

- **In scope**:
  - 7 件の ML 宛アドレス(`planning@` / `accounting@` / `booth@` / `stage@` / `pr@` / `postmastar@` 等 6 エイリアスの管理者グループ / `general-affairs@`)それぞれの共有メールボックス化
  - 受信アクセス制御(LDAP グループ所属に基づく動的な閲覧・読み書き権限)
  - 送信時のなりすまし防止(LDAP グループ所属に基づく送信元アドレスの使用制限)
  - IMAP / SMTP AUTH のユーザー名のみログイン
  - メールサーバーのメモリ使用量削減
  - Roundcube Webmail の継続提供確認
  - 個人メールアドレス宛の送受信機能の廃止(配送・送信元としての使用の両方を停止する。ただし ML 共有メールボックスへアクセスするための本人認証(ログイン)自体は Requirement 2 のために維持する)
- **Out of scope**:
  - Stalwart Mail Server への再移行・再検討(過去の認証障害により対象外と確定済み)
  - DMS 以外のメールサーバーソフトウェアの新規導入
  - ステージング環境の新設
  - 7 件未満・以外の新規 ML の追加作成(将来の ML 追加時の手順整備は対象外)
- **Adjacent expectations**:
  - Authentik LDAP Outpost が `memberOf` 属性をユーザーエントリに公開する現行動作に依存する。Authentik 側のスキーマ変更があれば本仕様の前提が崩れる
  - Roundcube (`gitops/manifests/prod/roundcube/`) は本仕様の対象外コンポーネントだが、OAUTHBEARER 認証経路を共有しており継続動作の確認が必要
  - VolSync によるメールデータバックアップ(`replication-source.yaml`)は対象構成の変更後も同一 PVC を対象とし続ける前提で、バックアップ設定自体への変更は行わない

## Requirements

### Requirement 1: ML宛メールの直接配送化と個人メール受信の廃止

**Objective:** メールサーバー運用者として、ML宛メールを個人メールボックスへ fan-out させず ML 自身の共有メールボックスへ直接配送し、個人メールアドレス宛の受信機能自体を廃止したい。そうすることで、配送経路をシンプルにし個人メール依存を完全に解消できる。

#### Acceptance Criteria
1. The メールサーバー shall 各 ML 宛アドレスに対応する専用のメールボックスを保持する。
2. When ML 宛アドレスにメールが届いた場合, the メールサーバー shall そのメールを ML 専用の共有メールボックスへ直接配送する。
3. The メールサーバー shall ML 宛メールを個人メンバーの個人メールボックスへ複製・転送しない。
4. While 旧来のグループ展開による fan-out 配送設定が残存する ML について, the メールサーバー shall その ML 宛メールについても fan-out 配送を行わない(新方式への切り替えが完了した ML が対象)。
5. The メールサーバー shall 個人メンバーの個人メールアドレス宛に送られたメールを配送可能な宛先として扱わない(個人メールアドレスへの受信機能を廃止する)。
6. The メールサーバー shall 個人メンバーの IMAP / SMTP AUTH 認証(ログイン)機能自体は維持し、Requirement 2 で定める ML 共有メールボックスへのアクセスに使用できる状態を保つ(個人メールボックスの受信廃止と本人認証の維持は両立させる)。

---

### Requirement 2: 共有メールボックスへの受信アクセス制御

**Objective:** ML メンバーとして、自分自身の資格情報でログインし、所属する ML の共有メールボックスを読み書きしたい。そうすることで、共有パスワードを使わずに安全に ML メールへアクセスできる。

#### Acceptance Criteria
1. When ML メンバーが自分自身の資格情報で IMAP にログインした場合, the メールサーバー shall そのメンバーが所属する LDAP グループに対応する ML 共有メールボックスへのアクセスを許可する。
2. If ログインしたユーザーが対象 ML の LDAP グループに所属していない場合, then the メールサーバー shall その ML 共有メールボックスへのアクセスを許可しない。
3. When メンバーが Discord ロール失効により対応する LDAP グループから外れた場合, the メールサーバー shall 次回ログイン以降そのメンバーに対する当該 ML 共有メールボックスへのアクセスを取り消す。
4. The メールサーバー shall ML 共有メールボックスへのアクセス制御を LDAP グループ所属情報に基づいて動的に判定し、静的な共有パスワードに依存しない。

---

### Requirement 3: 送信者なりすまし防止と個人メール送信の廃止

**Objective:** メールサーバー運用者として、ML 宛アドレスを送信元(From)として使えるユーザーを LDAP グループ所属者に限定し、個人メールアドレスを送信元とした送信そのものを廃止したい。そうすることで、ML アドレスを使ったなりすまし送信を防止し、個人メール依存を送信側でも完全に解消できる。

#### Acceptance Criteria
1. When ユーザーが ML 宛アドレスを送信元(From)としてメール送信を試みた場合, the メールサーバー shall そのユーザーが対応する LDAP グループに所属しているかを確認する。
2. If 送信元に指定された ML アドレスに対応する LDAP グループにユーザーが所属していない場合, then the メールサーバー shall その送信を拒否する。
3. When LDAP グループに所属するユーザーが対応する ML 宛アドレスを送信元として送信した場合, the メールサーバー shall その送信を許可する。
4. The メールサーバー shall 個人メンバーの個人メールアドレスを送信元(From)とした送信を、送信元の本人・所属 LDAP グループの有無に関わらず常に拒否する。
5. The メールサーバー shall 送信元(From)として使用可能なアドレスを ML 宛アドレスの集合のみに制限する。

---

### Requirement 4: ユーザー名のみでの認証

**Objective:** メールサーバー利用者として、ドメイン部分を省略したユーザー名のみで IMAP / SMTP AUTH にログインしたい。そうすることで、毎回フルメールアドレスを入力する手間を省ける。

#### Acceptance Criteria
1. When ユーザーがドメイン部分を含まないユーザー名のみで IMAP にログインした場合, the メールサーバー shall そのユーザーを正しく認証する。
2. When ユーザーがドメイン部分を含まないユーザー名のみで SMTP AUTH を行った場合, the メールサーバー shall そのユーザーを正しく認証する。
3. The メールサーバー shall 既存のフルメールアドレスでのログインについても引き続き認証を成功させる(後方互換)。
4. The メールサーバー shall 認証後に Postfix へ報告する正規化済みユーザー名を、送信者なりすまし防止チェック(Requirement 3)で使用する識別子と一致させる。

---

### Requirement 5: メモリ使用量の削減

**Objective:** インフラ管理者として、メールサーバーの実メモリ使用量を削減したい。そうすることで、シングルノードクラスター全体のメモリオーバーコミットを緩和できる。

#### Acceptance Criteria
1. The メールサーバー shall Rspamd によるスパム判定機能を有効にしたまま稼働する。
2. The メールサーバー shall Rspamd と機能が重複する不要なコンテンツフィルタ処理を行わない。
3. When 本変更をロールアウトした後, the メールサーバー shall ロールアウト前と比較して実メモリ使用量(`kubectl top` 計測値)を削減する。
4. The メールサーバー の Pod リソース定義(requests/limits) shall ロールアウト後の実測メモリ使用量に基づいて見直された値を反映する。

---

### Requirement 6: Roundcube Webmailの継続利用

**Objective:** ML メンバーとして、ブラウザの Roundcube から自分の個人 INBOX と所属 ML の共有メールボックスの両方を確認・送信したい。そうすることで、IMAP クライアント設定なしでも ML メールを扱える。

#### Acceptance Criteria
1. Where Roundcube が有効である場合, the メールサーバー shall Authentik OIDC セッションでログインしたユーザーに対し、引き続き個人 INBOX への IMAP アクセスを提供する。
2. Where Roundcube が有効である場合, the メールサーバー shall ログインユーザーが所属する ML の共有メールボックスを Shared 名前空間として提示する。
3. The 移行作業 shall Roundcube 関連マニフェスト(`gitops/manifests/prod/roundcube/`)への変更を本仕様の対象外とし、既存の OAUTHBEARER 認証設定を変更しない。

---

### Requirement 7: 段階的な移行と検証

**Objective:** 単独メンテナーとして、ステージング環境がない本番環境で新方式を段階的に検証したい。そうすることで、過去に複数回発生したメール障害の再発リスクを最小化できる。

#### Acceptance Criteria
1. When 新方式を初めて適用する場合, the 移行作業 shall 7 件の ML のうち `pr@aramakisai.com` 1 件のみを対象に先行適用する。 <!-- confidential:allow -->
2. While `pr@aramakisai.com` の動作確認が完了していない間, the 移行作業 shall 残り 6 件の ML について既存の配送・アクセス方式を維持する。 <!-- confidential:allow -->
3. The 移行作業 shall 各 ML を新方式へ切り替える際、配送・受信アクセス制御・送信制限・既存ユーザーへの影響の 4 点について確認結果を記録する。
4. If 先行適用した ML で配送・受信・送信のいずれかに不具合が確認された場合, then the 移行作業 shall 残り ML への展開を中止し旧方式への切り戻しを検討する。
