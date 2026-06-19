# Research & Design Decisions — mailing-list-shared-mailbox

## Summary
- **Feature**: `mailing-list-shared-mailbox`
- **Discovery Scope**: Complex Integration（既存 DMS + Authentik LDAP の配送・認可機構を再構成する変更であり、本番ステージング不在・過去複数回の本番障害履歴を持つコンポーネントへの変更のため Full Discovery を実施）
- **Key Findings**:
  - `LDAP_QUERY_FILTER_SENDERS` は docker-mailserver 公式の正式な env var だが、`SPOOF_PROTECTION=1` を設定しない限り Postfix の `smtpd_sender_login_maps` / `reject_sender_login_mismatch` は組み込まれない。現行 StatefulSet には `SPOOF_PROTECTION` が存在せず、**現状は誰でも任意の From アドレスで送信できる状態**である。
  - Dovecot ACL の正しい識別子構文は `group=<name>`（等号）であり、requirements.md の "`group:<DN>`形式" という表記はそのままでは無効な構文になる。
  - `<name>` は userdb が返す **`acl_groups` という名前の extra field**（comma-separated 文字列）と照合される。DMS の `DOVECOT_USER_ATTRS` で LDAP の `memberOf` を直接 `acl_groups` にマッピングすれば、`plugin{}` ブロックでの再マッピング（`acl_groups = %{userdb:groups}` 等）が不要になる。
  - requirements.md の「fan-out 停止のためにグループの `mail` 属性を消す」という想定実装は、`LDAP_QUERY_FILTER_SENDERS` が同じグループ・同じ `mail` 属性を参照する設計と衝突する（後述）。

## Research Log

### LDAP_QUERY_FILTER_SENDERS と SPOOF_PROTECTION の関係
- **Context**: Requirement 3（送信者なりすまし防止）が `LDAP_QUERY_FILTER_SENDERS` を新設する前提で書かれているが、現行 StatefulSet にこの仕組みを有効化する `SPOOF_PROTECTION` フラグが存在するか不明だった。
- **Sources Consulted**: [docker-mailserver Provisioner (LDAP) ドキュメント](https://docker-mailserver.github.io/docker-mailserver/latest/config/account-management/provisioner/ldap/)
- **Findings**:
  - `LDAP_QUERY_FILTER_SENDERS` 未設定時は USER/ALIAS/GROUP フィルタの論理和にフォールバックする。
  - `smtpd_sender_login_maps` への配線および `reject_sender_login_mismatch` の有効化は `SPOOF_PROTECTION=1` が前提。
  - 現行 `gitops/manifests/prod/mailserver/statefulset.yaml` には `SPOOF_PROTECTION` が存在しない。
- **Implications**: 本変更で `SPOOF_PROTECTION=1` を新規追加する必要がある。これは送信者制限を初めて有効化するという意味で、**既存の送信動作に対する新規の制約追加**であり、ML 以外の既存送信経路（Resend リレー経由の通知メール等）への影響有無を pr@ パイロット時に確認すべきリスク項目として扱う。

### Dovecot ACL ファイルの識別子構文と acl_groups
- **Context**: requirements.md は「`group:<グループDN>`形式の ACL」と記載しているが、Dovecot の実際の構文を公式ドキュメントで確認する必要があった。
- **Sources Consulted**: [Dovecot Access Control Lists (2.3)](https://doc.dovecot.org/2.3/configuration_manual/acl/), [acl plugin settings (2.3)](https://doc.dovecot.org/2.3/settings/plugin/acl-plugin/), [Shared Mailboxes (Dovecot CE)](https://doc.dovecot.org/main/core/config/shared_mailboxes.html)
- **Findings**:
  - `dovecot-acl` ファイルの行形式は `<identifier> <rights>`。有効な identifier は `owner` / `user=<name>` / `group=<name>` / `group-override=<name>` / `authenticated` / `anyone`（コロン区切りではなく等号）。
  - `group=<name>` の `<name>` は userdb から返る `acl_groups` extra field（comma-separated）の値と文字列一致で照合される。UNIX グループは無関係。
  - `acl_groups` は userdb 設定（今回は LDAP）から動的に設定するのが標準パターンであり、DMS の `DOVECOT_USER_ATTRS=...,memberOf=acl_groups` のような直接マッピングで実現できる。
  - Shared namespace は `type = shared` の namespace ブロックに `location` を `%%u` / `%%d`（二重 % でアクセス先ユーザーの変数として実行時に遅延展開）で記述する標準パターンが存在する。
- **Implications**:
  - `acl_groups` の値は Authentik LDAP Outpost が返す `memberOf` の DN 文字列（例: `cn=pr,ou=groups,dc=ldap,dc=goauthentik,dc=io` 想定）になる。dovecot-acl ファイルにはこの DN 文字列をそのまま `group=<DN>` として書く。**正確な DN 形式は pr@ パイロット時に `ldapsearch` で実測確認する**（Authentik の LDAP Outpost が group エントリをどの OU 配下に置くかは現時点で推定であり、確証ではない）。
  - `memberOf=groups` という中間名 → `plugin{}` での再マッピングという requirements.md の想定経路は不要。`memberOf=acl_groups` の直接マッピングで十分（Design Synthesis: Simplification）。

### Phase -1 事前検証: `LDAP_QUERY_FILTER_GROUP` 否定フィルタの実機確認（2026-06-19実施）
- **Context**: design.md Risks 記載のとおり、Authentik LDAP Outpost は OpenLDAP 等の標準実装ではない独自実装であり、属性未設定エントリに対する否定フィルタ `(!(attr=value))` が標準 LDAP 仕様どおり「真」と評価されるかは未確認だった。Phase 0 一括デプロイ前のゲートとして実機確認した。
- **Method**: `prod` namespace に一時 `alpine:3.20` Pod（`ldap-verify`）を作成し `openldap-clients` を導入。既存の `mailserver-service` bind account（`cn=mailserver-service,ou=users,dc=ldap,dc=goauthentik,dc=io`）で `authentik-ldap-outpost.prod.svc.cluster.local` に対し3パターンの ldapsearch を実行（本番マニフェストは無変更、確認後に Pod は削除）。
  1. `(&(objectClass=group)(mail=*))` — baseline
  2. `(&(objectClass=group)(mail=*)(!(mailListMigrated=true)))` — 否定フィルタ
  3. `(&(objectClass=group)(mail=*)(mailListMigrated=true))` — 正フィルタ（対照）
- **Findings**:
  - (1) baseline: ML 7グループ全件（企画/会計/出店/出演/広報/管理者/総務、`mail`属性で確認）がヒット。
  - (2) 否定フィルタ: baselineと同じ7件全件がヒット（`mailListMigrated`未設定の現状で「真」と評価された）。
  - (3) 正フィルタ: 0件（`mailListMigrated`未設定の現状で「偽」と評価された）。
  - (2)と(3)が排他的かつ(2)がbaselineと一致したことから、属性未設定時の否定フィルタは標準 LDAP 仕様どおりに動作することを確認した。
  - 副次情報: ML Group の DN は `ou=groups,dc=ldap,dc=goauthentik,dc=io` 配下（design.md Dovecot ACL セクションの「`ou=groups,...`想定」と一致）。
- **Implications**: design.md Migration Strategy の Phase -1 ゲートを通過。Phase 0（`LDAP_QUERY_FILTER_USER`/`GROUP`の変更含む一括デプロイ）を実施してよいと判断する。

### Phase 0 実装時の発見: 管理者グループの`mail`属性は6エイリアスのmulti-value（2026-06-19実施）

- **Context**: タスク2.3（Dovecot ACL・共有名前空間）の静的7namespaceブロックを記述するにあたり、design.mdの`Shared/pr/`例にある「7件のML宛アドレス」が文字通り7つの異なるアドレスなのか確認するため、`ldapsearch`で全7MLグループの`mail`属性を実機確認した。
- **Method**: `ldap-verify`一時Pod（Phase -1と同じ手法）で`(&(objectClass=group)(mail=*))`をmail/cn属性のみ取得して再実行（本番マニフェスト無変更、確認後Pod削除）。
- **Findings**: 企画/会計/出店/出演/広報/総務の6グループは`mail`が単一値（`planning@`/`accounting@`/`booth@`/`stage@`/`pr@`/`general-affairs@`、いずれも`@aramakisai.com`）だったが、**管理者グループのみ`mail`が6値**（`postmastar@`/`webmastar@`/`abuse@`/`admin@`/`administrator@`/`www@`）だった。design.mdは暗黙に「1ML=1アドレス」を前提にしているが、実データはこの前提を満たさない。
- **Implications（タスク3.1/6.1で要対応、本タスク2では未確定のまま進めて良い）**:
  - タスク2（Phase 0グローバル変更）の env var 自体（`LDAP_QUERY_FILTER_USER`/`GROUP`/`SENDERS`）は`%s`に対する単純な属性一致のみで、multi-value `mail` に対しても標準LDAP仕様どおり「いずれかの値に一致すれば真」と評価されるため**変更不要**。タスク2.3の静的namespaceブロックも、「管理者」カテゴリの canonical local-part を1つ選べば7ブロックのまま成立する（本実装では`admin`を採用、folder名 `Shared/admin/` として記述済み）。
  - タスク3.1/6.1（ML専用Authentik User作成）では、管理者カテゴリのUserに対し既存の `mailAlias`（個人メンバーの追加アドレス用に既に存在する仕組み、`LDAP_QUERY_FILTER_ALIAS`で解決）を流用するのが既存実装と一貫する: primary `mail` = `admin@aramakisai.com`（`mailListAddress=true`、`ak-active=true`）、`attributes.mailAlias` に残り5件（`postmastar@`/`webmastar@`/`abuse@`/`administrator@`/`www@`）を設定する。これにより `LDAP_QUERY_FILTER_ALIAS` がいずれの別名宛メールも `admin@` の正規アドレスへ解決し、`LDAP_QUERY_FILTER_USER`（`mailListAddress=true`一致）経由で同一メールボックスへ配送される（2段ホップ、既存の個人メンバー向けalias解決と同じ経路を再利用するのみで新規メカニズムは不要）。 <!-- confidential:allow -->
  - `postmaster@aramakisai.com`は既存のDMARC/TLS-RPTレポート送付先（`terraform/dns.tf`）として外部的に重要なアドレスだが、内部Maildir/フォルダ名の選択（`admin`）とは独立しており影響しない。 <!-- confidential:allow -->
- **Why not blocking Task 2**: タスク2の各サブタスクのObservableはいずれもグローバルなenv var/dovecot.cf構文のみを対象とし、ML専用Userがまだ存在しない時点（Phase 1以前）でも検証可能なため、この発見はタスク2の完了判定に影響しない。

### タスク2.6デプロイ時に発見した実装バグ2件（2026-06-19実施）

- **バグ1: ACL pluginがprotocol imap向けに有効化されない**
  - **Context**: design.md/タスク2.3は`mail_plugins = $mail_plugins acl`をdovecot.cf（→`/etc/dovecot/local.conf`、`!include conf.d/*.conf`の後に読込まれる）に書くだけで足りる想定だった。
  - **Findings**: `doveconf -n`実機確認で`protocol imap { mail_plugins = }`が空のまま（`conf.d/20-imap.conf`が`protocol imap { mail_plugins = $mail_plugins }`を先に確定させ、後段でのグローバル変更が反映されない。doveconf自身が`Global setting mail_plugins won't change the setting inside an earlier filter`と警告）。`local.conf`側で`protocol imap {}`を再宣言する対処も無効だった（再宣言してもこの「filterの確定値は変更不可」という挙動自体は変わらない、実機で2回確認）。
  - **Fix**: `conf.d/*.conf`全体より前に読まれるファイル名（`05-acl-plugin.conf`、configmap.yamlの新規キー）を`/etc/dovecot/conf.d/05-acl-plugin.conf`に直接マウントし、そちらで`mail_plugins = $mail_plugins acl`を設定する方式に変更（commit 9cd280c）。`doveconf -n`で`protocol imap { mail_plugins = " acl" }`になることを確認済み。
  - **教訓**: Dovecotの`protocol {}` filterブロックは「そのfilterが一度確定した値は、後から書いたグローバル変更どころか同じfilterの再宣言でも変更不可」という直感に反する挙動を持つ。`!include`順序より前に変更を注入するしかない。

- **バグ2: `ldap-senders.cf`がDMSのoverride対象外でカスタム内容が無視される**
  - **Context**: design.md/タスク2.2は`ldap-groups.cf`と同じパターン（`/tmp/docker-mailserver/ldap-senders.cf`をマウント）でそのまま動く想定だった。
  - **Findings**: 実機の`/etc/postfix/ldap-senders.cf`を確認すると、`query_filter`はLDAP_QUERY_FILTER_SENDERS環境変数の値に正しく置換されていたが、`result_attribute`がDMSイメージ標準の`mail, uid`のままで、`special_result_attribute`/`leaf_result_attribute`/`timeout`が一切存在しなかった（こちらのカスタム内容が反映されていない）。DMSの`setup.d/ldap.sh`を確認すると、`/tmp/docker-mailserver/ldap-{users,groups,aliases,domains}.cf`の4種類のみを`/etc/postfix/`へコピーするループになっており、`senders`は対象外（コードで直接確認、`for i in 'users' 'groups' 'aliases' 'domains'`）。
  - **検討した代替案**: `/etc/postfix/ldap-senders.cf`への直接マウント（ConfigMapボリュームはread-only）→ 同じ`ldap.sh`が全FILES配列に対し`_replace_by_env_in_file`（sed -i相当）を無条件実行するため、read-onlyマウントでは起動が壊れる。デプロイ前にロジックを読んで気づき、未デプロイで回避。
  - **Fix**: DMS公式の拡張フック`/tmp/docker-mailserver/user-patches.sh`（`_setup`完了後、daemon起動前に実行）でLDAP_*環境変数を直接参照し`/etc/postfix/ldap-senders.cf`をcatヒアドキュメントで生成する方式に変更（commit 37dfe8f）。実機で`bind_dn`/`bind_pw`/`query_filter`が正しい値に展開され、`special_result_attribute=member`等も反映されることを確認済み。
  - **教訓**: DMSの`LDAP_QUERY_FILTER_*`系env varは４種（USER/GROUP/ALIAS/DOMAIN）のみが`/tmp/docker-mailserver/`からの直接override対応で、`SENDERS`はその対象外（DMS実装の非対称性、ドキュメントには明記されておらずソース確認が必要だった）。同様の非対称性が他のDMS機能にもある可能性があるため、新規のLDAP_QUERY_FILTER_*系設定を追加する際は`setup.d/ldap.sh`のFILES配列を都度確認すること。

- **最終確認（2026-06-19、ユーザー実施）**: 上記2件修正後、ユーザー自身が(1)個人アドレスからのFrom送信（Roundcube経由）→`553 5.7.1 Sender address rejected: not owned by user`で拒否、(2)外部Gmailから個人アドレス宛送信→`550 5.1.1 User unknown in virtual mailbox table`で拒否、の2点を実トラフィックで確認。kubectl logsで両エラーとも想定通りの拒否理由・拒否コードであることを確認済み。タスク2.6完了。

## Architecture Pattern Evaluation

| Option | Description | Strengths | Risks / Limitations | Notes |
|--------|-------------|-----------|---------------------|-------|
| Dovecot ACL + Shared namespace（採用） | LDAP `memberOf` → `acl_groups` → ACL ファイルでの動的認可 | Dovecot 標準機能のみで実現、追加ミドルウェア不要、Discord ロール失効が次回ログインで即時反映 | dovecot-acl ファイルをメールボックスごとに手動設置する運用が必要（7件) | 既存の `ldap-groups.cf` ハックと同系統の「LDAP 属性を流用した制御」という設計言語に揃う |
| Mailman 3 等専用 ML ソフトウェア導入 | 専用 OSS で ML を一から構築 | 機能が豊富、運用ノウハウが世間に存在 | シングルノード CX33 のメモリ制約で新規常駐プロセスの追加が困難（[[project_mailing_list]] で既に却下判断済み） | Out of scope（過去判断を継承） |
| Postfix virtual_alias_maps による fan-out 継続 + 個人メールボックス共有 | 現状維持 | 変更コスト最小 | 個人メール依存が残り、Requirement 1/2 の意図（共有メールボックス化）を満たさない | 不採用 |

## Design Decisions

### Decision: fan-out 停止と送信者制限の参照対象を分離する2フラグ方式
- **Context**: requirements.md は「ML グループの `mail` 属性を消すことで fan-out を停止する」という実装方針を暗示しているが、`LDAP_QUERY_FILTER_SENDERS`（送信者制限）も同じグループの `mail` 属性 + `member` 展開という同一パターンを流用する計画になっている。グループの `mail` を消すと fan-out は止まる一方、送信者制限の照合先も同時に失われ、移行直後の ML メンバー全員が送信不可になる（Requirement 3 と Requirement 7 の段階移行が両立しない）。
- **Alternatives Considered**:
  1. グループの `mail` 属性削除をそのまま fan-out 停止の唯一のスイッチとして使う — Requirement 3 の送信判定が壊れるため不採用
  2. `LDAP_QUERY_FILTER_SENDERS` をグループの `mail` 属性ではなく別の固定識別子（例: グループの `cn`）で照合する — Authentik Group オブジェクトに ML アドレスと `cn` の対応関係を別途人手で管理する必要が生じ、既存の「`mail` 属性 = ML アドレスそのもの」という直感的な対応関係が崩れる
  3. （採用）独立した2つの真偽値フラグで関心を分離する
- **Selected Approach**:
  - `mailListAddress = true` — 新設する ML 専用 Authentik User（7件）に Terraform で付与。`LDAP_QUERY_FILTER_USER` に `(mailListAddress=true)` を追加し、個人アドレスを Postfix の配送先候補から除外する（Requirement 1.5）。
  - `mailListMigrated = true` — 既存の ML Authentik Group に移行完了時点で1件ずつ付与（Authentik UI、既存の ML グループ作成と同じ手動運用）。`LDAP_QUERY_FILTER_GROUP` に `(!(mailListMigrated=true))` を追加し、移行済みグループだけを fan-out 展開（`virtual_alias_maps`）の対象から外す。グループの `mail` 属性自体は変更しない。
  - `LDAP_QUERY_FILTER_SENDERS`（新設）はグループの `mail` 属性 + `member` 展開という既存パターンをそのまま使い、`mailListMigrated` の値を一切参照しない。これにより送信者制限は全7 ML で移行フェーズに関係なく初日から一貫して機能する。
- **Rationale**: Postfix 側の既存実装は `LDAP_QUERY_FILTER_USER`（→ `virtual_mailbox_maps`、配送先決定）と `DOVECOT_USER_FILTER`/`DOVECOT_PASS_FILTER`（→ Dovecot 専用ファイル、ログイン可否決定）を最初から分離している。この分離を利用して「個人メール受信廃止」と「個人ログイン維持」を無改造で両立させられる（Requirement 1.5 と 1.6）。同様に「fan-out 停止」と「送信者制限」の参照先を分離することで、Requirement 3 と Requirement 7 の段階移行が両立する。
- **Trade-offs**: フラグが2種類に増える分、Authentik 側の運用ドキュメント（どの属性をどのオブジェクトに付けるか）の管理コストはわずかに増える。ただし既存の `discord_role_id` / `discord_role_ids` のように複数の真偽値・属性をオブジェクトごとに使い分ける運用は既存パターンに合致しており、許容範囲。
- **Follow-up**: pr@ パイロット適用後、`mailListMigrated=true` 設定前後で `LDAP_QUERY_FILTER_GROUP` の挙動（fan-out 停止）と `LDAP_QUERY_FILTER_SENDERS` の挙動（送信許可）の両方を実トラフィックで確認する。

### Decision: `DOVECOT_USER_ATTRS` への `memberOf=acl_groups` 直接マッピング
- **Context**: requirements.md は `memberOf=groups` という中間属性名を経由する想定だったが、Dovecot の `acl_groups` plugin setting は userdb extra field から直接設定できることが判明した。
- **Alternatives Considered**:
  1. `memberOf=groups` でユーザーDB拡張フィールドに取り込み、`dovecot.cf` の `plugin{}` ブロックで `acl_groups = %{userdb:groups}` のように再マッピングする
  2. （採用）`memberOf=acl_groups` で直接マッピングする
- **Selected Approach**: `DOVECOT_USER_ATTRS` の既存値に `,memberOf=acl_groups` を追記する。
- **Rationale**: Design Synthesis の Simplification レンズに従い、不要な中間層を1つ削減する。`plugin{}` 側の追加設定が不要になり、設定ファイルの行数・可動部分が減る。
- **Trade-offs**: なし（機能的に完全に等価で、設定がシンプルになるのみ）。
- **Follow-up**: なし。

## Risks & Mitigations
- **`LDAP_QUERY_FILTER_USER`/`LDAP_QUERY_FILTER_GROUP` の変更は ML 単位の段階トグルが存在しない** — `mailListAddress`/`mailListMigrated` フラグはいずれも参照される側の値であり、フィルタ式自体（`statefulset.yaml`）は単一の env var として全7ML・全メンバーに同時適用される。デプロイした瞬間に個人メール受信が全員分停止し、全7MLのグループ展開フィルタが切り替わるため、Phase -1（`ldapsearch`による否定フィルタの事前実機確認、本番非破壊）を Phase 0 デプロイ前に必須とする（design.md Migration Strategy 参照）。
- **ML Authentik Group の正確な DN 形式が未確認** — pr@ パイロット時に `ldapsearch` で実測し、`dovecot-acl` ファイルに正しい DN を書く。誤った DN では ACL が常に不一致となり「アクセス不可」側に安全側で倒れる（fail closed）ため、誤設定時の被害は機能不全に留まる。
- **ML Authentik Group のネスト運用は未検証** — `special_result_attribute=member`による展開は1階層分のみを前提とする。グループをメンバーとして追加する運用は行わず、ユーザーを直接所属させる既存運用（[[project_mailing_list]]）を維持する。
- **`random_password`（ML 専用 User 用）が Terraform Cloud の tfstate に平文保存される** — ML 専用 User はログイン用途を持たないため実害は低いが、`terraform output` に値を一切出さない運用を徹底する。
- **`SPOOF_PROTECTION=1` の初回有効化が既存の送信経路に影響しないか未検証** — Resend リレー経由の通知メール等、ML 以外の送信フローが `LDAP_QUERY_FILTER_SENDERS` のチェック対象に意図せず含まれないか pr@ パイロット時に確認する。
- **`special_result_attribute=member` 展開のLDAPタイムアウト再発リスク** — `ldap-senders.cf` は `ldap-groups.cf` と同じ `timeout=30` を最初から設定し、過去の本番障害（メッセージ消失）の再発を防ぐ。

## References
- [Account Management | Provisioner (LDAP) - Docker Mailserver](https://docker-mailserver.github.io/docker-mailserver/latest/config/account-management/provisioner/ldap/) — `LDAP_QUERY_FILTER_SENDERS` / `SPOOF_PROTECTION` の仕様
- [Access Control Lists — Dovecot documentation (2.3)](https://doc.dovecot.org/2.3/configuration_manual/acl/) — dovecot-acl ファイル形式・識別子構文
- [acl plugin — Dovecot documentation (2.3)](https://doc.dovecot.org/2.3/settings/plugin/acl-plugin/) — `acl_groups` / `acl_shared_dict` 設定
- [Shared Mailboxes | Dovecot CE](https://doc.dovecot.org/main/core/config/shared_mailboxes.html) — shared namespace の標準構成パターン
- [[project_mailing_list]] — 既存 fan-out 方式の実装経緯・本番検証記録（2026-06-11）
