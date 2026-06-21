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

### タスク3.1検証時のLDAP接続エラー（2026-06-21実施）
- **Context**: タスク3.1（pr@専用User作成）のObservable検証として、mailserver Pod内からldapsearchで属性値（mail/ak-active/mailListAddress）を確認しようとした。
- **Method**: mailserver-0 Pod内で`ldapsearch -H ldap://authentik-ldap-outpost.prod.svc.cluster.local -D "$LDAP_BIND_DN" -w "$LDAP_BIND_PW" -b "dc=ldap,dc=goauthentik,dc=io" "(&(objectClass=person)(mail=pr@aramakisai.com))"`を実行。 <!-- confidential:allow -->
- **Findings**: `ldap_sasl_interactive_bind: Can't contact LDAP server (-1)`で接続失敗。`kubectl logs mailserver-0`を確認したところ、`warning: dict_ldap_connect: Unable to bind to server ldap://authentik-ldap-outpost.prod.svc.cluster.local with dn cn=mailserver-service,ou=users,dc=ldap,dc=goauthentik,dc=io: 49 (Invalid credentials)`が継続的に記録されていた（2026-06-20T16:03:12〜）。DNS解決は正常（10.43.107.224）でLDAP Service自体は稼働中。
- **Root cause hypothesis**: authentik_ldap.tfの`authentik_flow.ldap_bind`コメントにある通り、LDAP Outpostのbind flow設定（`authentication = "none"`）と`bind_mode = "cached"`/`search_mode = "cached"`の相互作用により、cached bindが失敗している可能性。ただしPhase -1事前検証（2026-06-19）では一時Podから正常にldapsearchが実行できたとの記録があり、mailserver Pod固有の問題（環境変数の不一致、LDAP接続の初期化タイミング等）の可能性もある。
- **Implications**: タスク3.1のldapsearch検証は現在実施不可。ただし`terraform plan -target`が"No changes"を返しており、Terraform stateとAuthentik環境が一致していることは確認済み。タスク3.1は実装完了としてマークし、ldapsearchでの属性値直接検証はLDAP接続問題解決後にタスク3.2（pr@のDN実機確認）で再試行する。タスク4.xの4点確認（IMAPログイン・送信テスト）もLDAP接続が前提のため、本問題の解決がPhase 2以降のブロック要因となる。
- **Next action**: LDAP接続エラーの原因調査（Authentik bind flow設定、mailserver PodのLDAP_BIND_PW環境変数の整合性確認、ESO/infisical-auth経由のシークレット同期状態の確認）を優先実施。memoryの[[project_eso_infisical_auth]]（infisical-auth secretが空になりESO全体停止）との関連も確認する。

### タスク3.2着手時のLDAP Outpost全断（2026-06-21、Phase 1ブロック）
- **Context**: タスク3.2（pr@のDN実機確認 + dovecot-acl設置）に着手。ldapsearchによるDN実機確認・dovecot-aclのgroup=<DN>記述ともにLDAP bind成功が前提。
- **Findings**: mailserver-0ログで`dict_ldap_connect: Unable to bind ... cn=mailserver-service,...: 1 (Operations error)`が継続。fresh pod（ldap-verify）から正パスワードでbindしても同じ`Operations error (1)`、誤パスワードでは`Invalid credentials (49)`。→ mailserver固有でなくOutpost側の全断。3.1記録時の`Invalid credentials (49)`から悪化（49=認証段階まで到達, 1=Outpost内部エラーで認証以前に失敗）。
- **Root cause**: Outpost/Server `2026.5.3`がOperations-errorのbind不具合を持つ（[[project_ldap_outpost_auth_failure]]、X-authentik-outpost-token vs Authorization Bearer不一致）。commit `362c625`(18:00)で`2026.5.2`にピン留めしbind復旧したが、15分後の`96f327c`(18:15)でピンを外し`2026.*`auto-upgradeに戻したため`2026.5.3`へ浮動し再発。[[project_argocd_stable_tag_version_drift_incident]]と同型のunpin→driftパターン。
- **副次影響(live prod incident)**: bind全断によりPostfix `ldap-aliases.cf`等の照会が`lookup error`→ML/個人宛の受信解決とDovecotログインが現在機能不全（tempfail/defer）。タスク3.2のみならず本番メール配送・ログインが停止中。
- **Implications**: タスク3.2はLDAP bind復旧までhard-block。3.1で先送りしたldapsearch検証も同様。
- **再ピン検証結果（2026-06-21、`2026.5.2`は無効と確定）**: `2026.5.2`へ再ピン（commit `ebca13b`）しOutpost podを`2026.5.2`にロールしたが、fresh podからの実bindは依然`Operations error (1)`。version変更は無効と確定し`ebca13b`をrevert（`b5fa915`、`96f327c`のauto-upgrade状態へ復帰）。
- **真因確定（Outpostログ証跡）**: Outpostログに以下のシーケンス。
  - `"Successfully connected websocket"` — Outpost↔Server websocketは確立（`AUTHENTIK_TOKEN`自体は有効）
  - `"event":"User has access"` — LDAP bindのpolicy判定はPASS
  - `"error":"403 Forbidden  (Authentication credentials were not provided.)","event":"failed to get user info"` — Outpostが**user info取得のためServer APIへHTTP呼び出しする際、認証ヘッダが付かず403**
  - → bindは`Operations error (1)`でクライアントへ返る。`took-ms:5487`の初回フル bind失敗後、`authenticated from session`(took-ms:0)のcached bindのみ通る（user info欠落）。
  - 結論: version/token設定の問題でなく、2026.x系Outpost flow executorのAPI呼び出しヘッダ不整合（[[project_ldap_outpost_auth_failure]] と一致）。version pin（2026.5.2/5.3いずれも）では解消しない上流コードバグ。
- **Next action**: タスク3.2/4.x はLDAP bind復旧までhard-block継続。恒久対応は別spec `authentik-ldap-bind-failure`（design-generated, 未実装）で扱う。version pinは打ち切り。

### タスク3.2着手: LDAP bind復旧後に判明した `memberOf=acl_groups` 直接マッピングの設計欠陥（2026-06-21）
- **前提**: LDAP bind障害は別途解消（custom bind flowに`user_login` stage欠落が真因、commit `1ced512`で修正。[[project_ldap_outpost_auth_failure]]）。bind復旧後にタスク3.2のDN実機確認を実施。
- **DN確認結果**: pr@対応のML GroupのDNは `cn=広報,ou=groups,dc=ldap,dc=goauthentik,dc=io`（cnが日本語「広報」、ldapsearchはbase64返却）。他MLも同様にcnが日本語名の見込み。
- **設計欠陥（2点、いずれもdesign.md「`DOVECOT_USER_ATTRS`への`memberOf=acl_groups`直接マッピング」Decisionを無効化）**:
  1. **多値memberOfが単一値に潰れる**: member1 は memberOf 3件（`discord-linked-users`/`広報`/`管理者`）を持つが、`doveadm user member1@aramakisai.com` の `acl_groups` は `cn=discord-linked-users,ou=groups,...` の**1件のみ**。Dovecot LDAP userdb は多値属性を単一フィールドへマップする際1値しか保持しない（`user_attrs = ...,memberOf=acl_groups` を実機確認）。→ `広報` がacl_groupsに乗らず、pr@のACLは原理的にmatchしない。 <!-- confidential:allow -->
  2. **DN中のカンマでacl_groupsが分割される**: acl pluginは`acl_groups`をカンマ区切りでtoken化する。DN `cn=広報,ou=groups,dc=ldap,...` はカンマを含むため `cn=広報`/`ou=groups`/`dc=ldap`/... に砕けて `group=<DN>` と一致しない。仮に多値問題を解決しても、DNをそのままgroup名にする方式は成立しない。
- **影響**: タスク3.2（dovecot-acl設置）は現設計のままでは機能するACLを作れない。**design.md の改訂が必要**（Revalidation Trigger「Authentik LDAP Outpost のスキーマ/`memberOf`挙動」に該当）。タスク3.2は未完了のままブロック。
- **修正方向（design改訂で要決定、未実装）**: Authentik側でユーザーの所属グループを「カンマを含まない識別子のカンマ区切り単一文字列」として公開するproperty mappingを新設し（例: group cn または専用slugを `,` 連結した1属性 `mailAclGroups`）、`DOVECOT_USER_ATTRS` で `mailAclGroups=acl_groups` にマップする。dovecot-aclは `group=広報`（または slug）で記述する。これにより (1)多値→単一カンマ区切り文字列化 (2)カンマ衝突回避（cnにカンマ無し）の両方を解消する。group cnが日本語のままで良いか（ASCII slug化すべきか）も併せて決める。
- **Next action**: mailing-list-shared-mailbox の design.md / tasks.md をこの制約に合わせて改訂してから 3.2 を再設計・再実行する。

### タスク3.2再着手: slug方式の前提未充足 + namespace separator不整合バグ（2026-06-21）
- **前提**: タスク2.8でASCII slug方式へ是正済（`DOVECOT_USER_ATTRS=mailAclGroups=acl_groups`、`_save_attrs()`に`mailAclGroups`計算を追加、commit `1f0eb66`適用済）。bind復旧後にslug方式の実機確認を実施。
- **検証手法**: mailserver-0からの`doveadm user`、および一時Pod `ldap-verify`（alpine+openldap-clients、Phase -1と同手法、確認後削除）から`mailserver-service` bindでのldapsearch。
- **発見1（前提未充足・人手ゲート未完）**: ML 7グループに`mailAclSlug`属性が**1件も設定されていない**。`ldapsearch (mail=pr@aramakisai.com)` on `ou=groups` が`mailAclSlug`を返さず、`mail`/`cn`(=広報)のみ返却。結果`(mailAclGroups=*)`に**該当ユーザーが0件**（member1含む全員`mailAclGroups`空）。→ slugが無いため`_save_attrs()`が空集合を計算し`mailAclGroups`を永続化できない。`doveadm user member1@aramakisai.com`に`acl_groups`フィールドが出ない（空）ことと整合。タスク2.8で「人手ゲート未完」と記録した(1)Authentik UIでの`mailAclSlug`設定、(2)メンバーのDiscord再ログイン、の**両方が未実施**。pr@ User自体は`mailListAddress=true`/`ak-active=TRUE`でldapsearch確認OK（3.1の先送り検証を本タスクで実施・成功）。 <!-- confidential:allow -->
- **発見2（新規実装バグ・タスク2.3の潜在不具合）**: `doveadm mailbox create -u pr@aramakisai.com INBOX`が`Error: namespace configuration error: All list=yes namespaces must use the same separator`で失敗。`doveconf -a`で既定inbox(private) namespaceの`separator`が空（Maildir既定の`.`）、対して7件のshared namespaceは`separator = /`を明示。Dovecotの「list=yes namespace間でseparator統一必須」制約に抵触。**daemon起動はするが共有メールボックスへのアクセス・doveadm mailbox操作が一切できない**latentバグ（タスク2.3でShared未テストのため見逃し、タスク4.1の受信アクセス確認到達前に発覚）。pr@ Maildirも未作成（`/var/mail/aramakisai.com/`に`test`/`member1`のみ）。 <!-- confidential:allow -->
  - **Fix（configmap.yaml、未デプロイ）**: `dovecot.cf`に`namespace inbox { inbox = yes; separator = / }`を追加しinbox側を`/`へ統一。既存個人メールボックス（member1）は`^\.[^.]`なネストフォルダ0件のフラット構成のため、IMAP区切り文字変更の実害なし（確認済）。
- **影響**: タスク3.2は2要因でブロック。(A)発見2のseparator fix → commit+ArgoCD sync+pod rollout後に`doveadm mailbox create`でpr@ Maildir作成 → `dovecot-acl`(`group=pr lrwstipekxa`)設置が可能になる（コード側で完結、デプロイ要）。(B)発見1のslug前提 → Authentik UIで広報グループに`mailAclSlug=pr`設定 + 広報メンバー1名のDiscord再ログイン（人手、terraform外管理）の後でないと、`doveadm user`の`acl_groups`に`pr`が載らずタスク3.2のObservable（ACLの`group=pr`がメンバーacl_groupsと一致）を確認できない。
- **Next action**: (A)separator fixをデプロイしpr@ Maildir+dovecot-acl設置。(B)ユーザーにmailAclSlug設定+再ログインを依頼。両完了後に`doveadm user <広報member>`で`acl_groups`に`pr`が載ることを確認し3.2クローズ。

### タスク3.2クローズ: slugパイプライン end-to-end成立 + Outpostキャッシュ知見（2026-06-21）
- **mailAclSlug設定はAPIで実施（UI不要と判明）**: ML GroupはTerraform外管理だが、Authentik Core API `PATCH /api/v3/core/groups/{pk}/` で属性設定可能。広報の実ML Group pkは `d2382993-61c7-47d2-ac1d-55864c757e46`（同名の空属性グループ `a005ec59...` が別に存在、誤爆注意）。**attributesはPATCHで丸ごと置換**されるため既存`mail`/`discord_role_ids`を含めた全体を送る必要あり。`{"attributes":{"mail":["pr@..."],"discord_role_ids":["..."],"mailAclSlug":"pr"}}`をPATCHしHTTP200。 <!-- confidential:allow -->
- **検証用にmember1 `mailAclGroups=pr` をAPI一時セット**（Discord再ログインを待たずに即検証するため）: `_save_attrs()`は過去ログイン時に空文字`mailAclGroups=""`を既にセット済（=ldapsearchで未公開＝空のため出てこなかった真因）。group側にslug設定済なので、次回再ログインの再計算でも`pr`になり一時値と一致＝永続的に整合。**curlハマり**: userのattributesに巨大なavatar(base64 ~150KB)が含まれ、`-d "$BODY"`のargv渡しが`Argument list too long`で失敗→`--data @file`方式に変更で解決。
- **Outpostキャッシュが即時反映しない（重要・Req 2.3に影響）**: API PATCH後もLDAP Outpost(`search_mode=cached`)は`mailAclGroups`を空のまま返し、`doveadm user`の`acl_groups`も空。**Outpost pod再起動で強制リフレッシュ**したところ、Outpost直ldapsearchが`mailAclGroups: pr`を公開、`doveadm auth cache flush`後の`doveadm user member1@...`が`acl_groups	pr`を返した。 <!-- confidential:allow --> → dovecot-aclの`group=pr`と一致しタスク3.2のObservable達成。
- **教訓**: 「Discordロール失効→次回ログインでアクセス失効」(Req 2.3)はOutpostの`search_mode=cached`キャッシュTTL分の遅延を伴う。権限即時失効が要件になる場合はキャッシュ設定の見直しが必要。cutover/残り6ML展開(タスク6)でも属性変更後のOutpostキャッシュ反映タイミングに注意。
- **残**: 実IMAPログインでのShared/pr表示・読み書き＝タスク4.1。残り6MLのslug設定＝タスク6.2（同じくAPI PATCHで可能、各pk要特定）。

### タスク4.1着手: 共有namespace INDEXパス不可書きバグ発見・修正 + ACLゲーティング実証（2026-06-21）
- **手法**: mailserver-0で`doveadm`（admin）による非対話検証。member=member1（`acl_groups=pr`）/ non-member=test（`acl_groups`空）。実IMAPセッションは各メンバーのパスワード（Authentik LDAP/OAUTHBEARER、master user未設定）が必要なため本検証では`doveadm acl rights`等のACLエンジン評価で代替。
- **新規バグ発見（タスク2.3の潜在不具合、Shared namespace全断）**: `doveadm acl get/status -u member1 Shared/pr`が`mkdir(/var/indexes/aramakisai.com/pr) failed: Permission denied (euid=5000(docker) ... /var owned by 0:0 mode=0755)`で失敗。design.md/タスク2.3の静的namespaceブロックは`location=maildir:/var/mail/.../<ml>:INDEX=/var/indexes/aramakisai.com/<ml>`としていたが、`/var`はルート所有(0755)でdovecot実効uid 5000がmkdirできず、**Shared名前空間が一切開けない**（mailbox list/status/acl get全滅）。タスク3.2は`doveadm mailbox create`（Maildir作成＝INDEX不要）まで到達したためこのバグは見逃された。
  - **Fix（configmap.yaml、commit `d8e7fd8`）**: INDEXを`/var/indexes/...`→`/var/mail/.indexes/aramakisai.com/<ml>`（7ブロック全て）。メールPVCルート`/var/mail`は`0777 docker`所有で書込可。DMSは全ユーザー単一uid 5000のため共有indexで問題なく、design元の「単一共有index＝共有seen状態」意図も保持。ArgoCD hard refresh→sync(`d8e7fd8`)→statefulset rollout restartでデプロイ済。
- **修正後のACLゲーティング実証（Observable 3点中2点をエンジンレベルで達成）**:
  - (a) member閲覧・書込権: `doveadm acl rights -u member1 Shared/pr` = `lookup read write write-seen write-deleted insert post expunge create delete admin`（フル）。`mailbox status Shared/pr`が`messages=0`を返し共有メールボックスが正常にopenできることを確認（INDEXバグ解消）。
  - (b) non-member非表示: `doveadm acl rights -u test Shared/pr` = **空（権限ゼロ）**。lookup権限が無いためfail-closedでLIST除外される（Req 2.2のメカニズム実証）。**注**: `doveadm mailbox list/status`はadmin権限でACLをバイパスするため非memberにもShared/prが見えるが、これはdoveadm固有の挙動であり実IMAPセッションには当てはまらない。権威ある判定は`acl rights`（=空）。
  - (c) subscribe不要の自動表示: `subscriptions=no`の静的namespace、admin全LISTで`Shared/pr`含む7件が無操作で出現。
- **dovecot-acl確認**: `/var/mail/aramakisai.com/pr/dovecot-acl` = `group=pr lrwstipekxa`（タスク3.2設置分）。member acl_groups=`pr`と一致。
- **残（人手ゲート、実認証セッション必須）**: タスク4.1の最終スモーク（実member/実non-memberの**実IMAPクライアントでのLIST・読み書き**）、タスク4.2（実SMTP AUTHでのFrom:pr@送信成功/非member拒否）、タスク4.3（個人ログインのusername-only/フルアドレス両UX・Roundcube疎通）はいずれもメンバーのパスワードを要するためユーザー実施。doveadmで検証可能な配送マップ・ACLエンジン・senderマップは下記4.4含め全て確認済。

### タスク4.1/4.3: 共有メールボックスがクライアントに自動表示されない → subscriptions=yes化（2026-06-21）
- **症状（ユーザー軽テスト報告）**: INDEXバグ修正デプロイ後、メンバーが実IMAPクライアントでログインできるが Shared/* フォルダが自動表示されず「とても不便」。
- **ログ確認**: mailserver-0ログで実クライアント(PLAIN, rip=124.155.16.5)・Roundcube(OAUTHBEARER, 10.42.0.1)双方がログイン成功、`LIST finished`記録あり。送受信も成立（`orig_to=<pr@>`→`to=<member1@>` INBOX stored、mailListMigrated未設定でfan-out継続中＝想定通り）。
- **根本原因**: 全7 shared namespaceが`subscriptions = no`。大半のIMAPクライアントは購読フォルダのみ表示（LSUB / LIST-SUBSCRIBED）するため、購読されていないShared/*は出ない。`doveadm mailbox list -s -u member1`の購読リストに個人フォルダ(Drafts/Sent/Junk/Trash/Mailspring)のみでShared/*無しを確認。doveadm無印LISTには出る（ACLゲートも正常）が、クライアントは購読分しか出さない。design.mdの「subscribe不要で自動表示」前提が実クライアントで崩れていた。
- **購読保存先の実機確認**: `subscriptions = no`下で`doveadm mailbox subscribe -u member1 Shared/pr`すると、購読は**member1個人の**`/var/mail/aramakisai.com/member1/subscriptions`に保存される（private namespaceへfallback、per-user）。全員自動表示にはこれでは不足。
- **採用方式（ユーザー選択肢1: 自動購読+ACL filter、commit `857617d`）**: 7 shared namespaceを`subscriptions = yes`化。`type=shared`かつ`location`が固定パス(`/var/mail/aramakisai.com/<ml>`)のため、購読状態は**共有メールボックスルートの単一`subscriptions`ファイル**に保存され全アクセスユーザーで共有される（DMS単一uid 5000）。
  - **実証**: デプロイ後`doveadm mailbox subscribe -u member1 Shared/pr`を1回実行 → 共有ルート`/var/mail/aramakisai.com/pr/subscriptions`に保存され、**別ユーザー`test`の`doveadm mailbox list -s`にもShared/prが購読表示**された（=1回subscribeで全メンバーに購読伝播）。member1の旧個人購読エントリ`Shared/pr/`はdoveadmが自動クリーン。
  - **ACLゲート維持**: `acl rights` member1=フル / test=空（変化なし）。`protocol imap { mail_plugins = " acl" }`でcore acl pluginがIMAPロード済。LIST/LIST-SUBSCRIBEDの可視性フィルタはcore aclが担うため、実IMAPでは非メンバー(lookup権限無)からShared/prは除外される見込み（fail-closed, Req 2.2）。
- **運用手順への含意**: ML追加（タスク6）では各MLメールボックス作成後に`doveadm mailbox subscribe -u <任意user> Shared/<slug>`を1回実行すれば全メンバーに購読が反映される（dovecot-acl設置とセットの一回限り操作）。
- **実機確認結果（2026-06-21、ユーザー実クライアント）**: (1)メンバー = Shared/pr が自動表示され開ける ✅。(2)非メンバー = **Shared/pr の存在(名前)は見えるが閲覧トグルがグレーアウトで開けない**（ACLが読取拒否、Req 2.2のアクセス制御は成立）。事前警告通りグローバル購読由来でフォルダ名のみ非メンバーに露出する（中身は守られる）。
- **設計判断（ユーザー決定 2026-06-21）**: 非メンバーへの名前露出は**許容**（現状維持、subscriptions=yes継続）。理由: アクセス制御(開けない)は満たされ中身機密は守られる、メンバー/新規メンバー(Discordロール付与)の自動表示が動的アクセスモデル(Req 2.4)と整合、ML名は委員会内で周知の情報。名前も隠す代替（per-member個別購読）は新規メンバーの手動subscribeが必要で動的モデルから後退するため不採用。→ タスク4.1クローズ。

### タスク4.2/4.3クローズ: 実SMTP/IMAP確認 + Roundcube identity運用知見（2026-06-21、ユーザー実機）
- **IMAP LIST実機（LIST-EXTENDEDクライアント、member1=広報メンバー）**: `LIST "" "*"`応答で `Shared/pr` = `(\HasNoChildren)`（選択可）、他6件 `Shared/{booth,stage,admin,planning,accounting,general-affairs}` = `(\Noselect \HasNoChildren)`。**`\Noselect`＝「名前は出るが開けない」がユーザーの見たグレーアウトの正体**。fail-closedがLISTレベルで動作（acl pluginがlookup権限の無い共有MBを\Noselectでマーク、グローバル購読下でも開けない）。Req 2.2成立。`Shared/admin`も`\Noselect`＝member1は管理者グループ所属だが管理者グループに`mailAclSlug`未設定（広報=`pr`のみ設定済）→ acl_groups=`pr`のみ。**複数ML所属の挙動が実証**: slug設定済グループのみ選択可。管理者を開くには管理者グループに`mailAclSlug=admin`設定（タスク6.2）。
- **username-onlyログイン実証（Req 4、タスク4.3）**: IMAP `LOGIN member1`（ドメイン無し）→`Logged in`、`LOGIN "member1@aramakisai.com"`（フル）→`Logged in` 両方成功。SMTP `AUTH PLAIN`もusername-only(`member1\0member1\0pw`)/フル両方`235 Authentication successful`。`auth_username_format = %n@aramakisai.com`の後方互換成立。 <!-- confidential:allow -->
- **送信制限実証（Req 3、タスク4.2）**: member1(広報メンバー)認証で `MAIL FROM:<pr@aramakisai.com>`→`250 Ok`（メンバーML送信許可）。`MAIL FROM:<member1@aramakisai.com>`（個人アドレス）→2セッションとも`553 5.7.1 Sender address rejected: not owned by user member1@aramakisai.com`（個人送信廃止 Req 3.4）。senders mapにSASL名が無ければreject、の機構が実証。非メンバー拒否は同一機構のため明示テスト省略可。 <!-- confidential:allow -->
- **Roundcube identity運用知見（重要・引き継ぎ）**: Roundcubeは`oauth_identity_fields=['email']`でOIDCログイン者のemail(member1@)のidentityのみ自動生成する。メンバーがRC上でML(pr@)アドレスをFromに使うには **設定→識別情報でpr@ identityを1回手動追加**する必要がある（`identities_level`未設定＝デフォルト0でUI追加可、`gitops/manifests/prod/roundcube/`変更不要でReq 6.3維持）。追加後はSASL認証=member1(広報メンバー)でsenders mapがpr@を許可するため送信が通り、From表示だけpr@になる。共有メールボックスをWebmailで送信運用する標準パターン。identity自動プロビジョニング（LDAP連携プラグイン等）はRC manifest変更を要しスコープ外。 <!-- confidential:allow -->
- **4点確認（Req 7.3）完了状況**: 配送切替準備(4.4)✅ / 受信アクセス制御(4.1)✅ / 送信制限(4.2)✅ / 既存ユーザー影響(4.3)✅。タスク4（Phase 2）クローズ → Phase 3（タスク5.1 cutover）へ進行可能。

### タスク4.4検証: cutover準備の両立確認（2026-06-21、autonomous完了）
- `postmap -q pr@aramakisai.com ldap:/etc/postfix/ldap-users.cf` → `pr@aramakisai.com`（`mailListAddress=true`一致でML専用Userが直接配送先として解決。cutover後の直接配送の前提成立）。 <!-- confidential:allow -->
- `postmap -q pr@aramakisai.com ldap:/etc/postfix/ldap-groups.cf` → `member1@aramakisai.com`（`mailListMigrated`未設定のためfan-out継続、メンバー個人アドレスを返す。意図通り）。 <!-- confidential:allow -->
- `postmap -q pr@aramakisai.com ldap:/etc/postfix/ldap-senders.cf` → `member1@aramakisai.com`（送信許可メンバー）。 <!-- confidential:allow -->
- 2照会が両立（fan-out継続＋直接配送解決可能）し重複配送なし。タスク4.4のObservable達成・完了。広報グループの現メンバーはmember1単独。

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

### Decision: `DOVECOT_USER_ATTRS` への `memberOf=acl_groups` 直接マッピング 【SUPERSEDED 2026-06-21】
- **⚠️ この Decision は実機検証で無効と判明し、下の「ACL グループ識別子を ASCII slug 化する」Decision で置換された。** 残置は経緯記録のため。
- **Context（当時）**: requirements.md は `memberOf=groups` 中間属性名経由の想定だったが、Dovecot `acl_groups` は userdb extra field から直接設定できると判断した。
- **Selected Approach（当時）**: `DOVECOT_USER_ATTRS` に `,memberOf=acl_groups` を追記。
- **なぜ無効だったか（2026-06-21 実機検証）**: (1) Dovecot LDAP userdb は多値属性を単一フィールドへマップする際1値しか保持せず、3グループ所属でも `acl_groups` に memberOf が1件しか乗らない（`doveadm user` 実測）。(2) memberOf の値は DN（`cn=広報,ou=groups,...`）でカンマを含むため、acl plugin のカンマ区切り token 化で DN が砕けて `group=<DN>` と一致しない。詳細は上の Research Log「`memberOf=acl_groups` 直接マッピングの設計欠陥」参照。

### Decision: ACL グループ識別子を ASCII slug 化し、Authentik 側で単一カンマ区切り属性として公開する
- **Context**: 上記 SUPERSEDED Decision の2欠陥（多値潰れ・DNカンマ衝突）を両方解消する必要がある。`acl_groups` は「カンマを含まない識別子の、カンマ区切り単一文字列」でなければ機能しない。
- **Alternatives Considered**:
  1. LDAP Provider の expression property mapping で bind 時に動的計算 — authentik LDAP Provider の expression mapping 対応が不確実、かつ bind 毎評価でレイテンシ増（既に full bind ~10s の問題あり、[[project_ldap_outpost_auth_failure]]）。不採用。
  2. group cn（日本語「広報」等）をそのまま slug に使う — cn にカンマは無いため技術的には可だが、設定ファイル・kubectl exec・doveconf 等で UTF-8 を持ち回る運用脆弱性がある。業務 UI 表示用に日本語 cn は残しつつ、機械照合は ASCII にしたい（ユーザー要望 2026-06-21）。不採用（cn は人間用に温存）。
  3. （採用）ML Group に ASCII の `mailAclSlug` 属性を持たせ、ユーザーの所属 ML グループの slug を**カンマ区切り単一文字列**にまとめた User 属性 `mailAclGroups` を Discord 同期時に計算・永続化する。
- **Selected Approach**:
  - ML Authentik Group に属性 `mailAclSlug`（ASCII、ML アドレスの local-part と一致: `pr`/`planning`/`booth`/`stage`/`admin`/`general-affairs`/`accounting`）を付与（Authentik UI 手動、`mailListMigrated` と同じ運用）。日本語 cn（広報 等）は業務表示用にそのまま残す。
  - 既存の `discord-group-sync-policy`（`authentik_policy_expression.discord_group_sync`、authentik_discord.tf）の `_save_attrs()` を拡張し、グループ membership 再計算後に `attrs["mailAclGroups"] = ",".join(sorted({g.attributes.get("mailAclSlug") for g in u.ak_groups.all() if g.attributes.get("mailAclSlug")}))` を永続化する。membership 変更点と同一トランザクションで更新されるため acl_groups と実所属が常に一致する。
  - `DOVECOT_USER_ATTRS` を `memberOf=acl_groups` → `mailAclGroups=acl_groups` に変更（statefulset.yaml）。User.attributes は authentik LDAP outpost が LDAP 属性として自動公開する（既存の `mailListAddress` と同経路）。
  - `dovecot-acl` ファイルは `group=<slug> lrwstipekxa`（例 `group=pr lrwstipekxa`）で記述。slug は Shared namespace の folder 名（`Shared/pr/` 等、タスク2.3で採用済）と一致し命名が揃う。
- **Rationale**: (1) 単一カンマ区切り文字列なので Dovecot の多値潰れを回避。(2) slug は ASCII でカンマ無しのため acl plugin の token 化で壊れない。(3) 更新を Discord 同期（membership の唯一の変更点）に同居させるので「Discord ロール失効→次回ログインで失効」の動的モデル（Requirement 2.4）を維持しつつ acl_groups と membership の不整合が起きない。(4) slug=local-part=folder名で命名が一貫。
- **Trade-offs**: User 属性 `mailAclGroups` は Discord 同期時にのみ更新されるため、既存ユーザーへの初期反映は各自の次回 Discord ログインを要する（動的モデルと整合、許容）。ML Group ごとに `mailAclSlug` を手動設定する運用が1項目増える（`mailListMigrated` と同種、許容）。
- **Follow-up**: タスク2.3 は `memberOf=acl_groups` で既にデプロイ済のため是正タスクが必要（タスク2.8 新設）。タスク3.2/6.2 の dovecot-acl 記述を DN から slug に変更。`mailAclGroups` が LDAP 属性として公開されることを ldapsearch / `doveadm user` で実機確認する。

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
