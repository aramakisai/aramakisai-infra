# Implementation Plan

本タスクは design.md の Migration Strategy（Phase -1 〜 Phase 4）に対応する。本番にステージングが存在しないため、Phase 0（グローバル変更）は1コミットで一括デプロイし、Phase 1〜4（ML個別の移行）は pr@ を先行適用したあと残り6件を1件ずつ順次トグルする（並行トグル禁止）。

- [x] 1. 事前検証（Phase -1）: LDAP否定フィルタの実機確認
  - 既存の mailserver-service LDAP bind account を使い、Authentik LDAP Outpost に対し ldapsearch で `(!(mailListMigrated=true))` 相当の否定フィルタを評価する
  - 属性が未設定のグループに対して期待通り「真」と評価されることを確認する（本番の statefulset.yaml/configmap.yaml には一切変更を加えない非破壊操作）
  - 想定と異なる評価結果が出た場合は Phase 0 のデプロイを見送り、design.md の見直しに戻ることを記録する
  - Observable: ldapsearch の実行結果ログと、否定フィルタが想定通り動作するという判断記録が残っている
  - **検証結果（2026-06-19実施、詳細は research.md Research Log参照）**: `(&(objectClass=group)(mail=*)(!(mailListMigrated=true)))` は ML 7グループ全件を返却（期待通り「真」）。対比した `(mailListMigrated=true)` の正クエリは0件。否定フィルタは想定通り動作すると判断し、Phase 0 デプロイを継続する。
  - _Requirements: 1.4_

- [ ] 2. グローバル設定変更の実装と一括デプロイ（Phase 0）
- [x] 2.1 Postfix配送フィルタを変更し個人メール受信廃止とfan-out段階停止の仕組みを組み込む
  - `gitops/manifests/prod/mailserver/statefulset.yaml` の `LDAP_QUERY_FILTER_USER` に `(mailListAddress=true)` 条件を追加し、個人メンバーの `mail` 属性ベース配送先一致を無効化する
  - 同ファイルの `LDAP_QUERY_FILTER_GROUP` に `(!(mailListMigrated=true))` 条件を追加し、移行完了グループのみ fan-out 対象外にする仕組みを組み込む
  - グループの `mail` 属性自体は変更せず、Sender Spoof Protection（タスク2.2）の照合対象を維持する
  - Observable: 変更差分が `statefulset.yaml` の該当 env var にのみ閉じていることを git diff で確認できる
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_

- [x] 2.2 送信者なりすまし防止を新設する
  - `gitops/manifests/prod/mailserver/configmap.yaml` に既存 `ldap-groups.cf` を複製した `ldap-senders.cf`（`special_result_attribute=member` / `leaf_result_attribute=mail` / `timeout=30`）を新設する
  - `statefulset.yaml` に `LDAP_QUERY_FILTER_SENDERS` と `SPOOF_PROTECTION=1` を追加し、`ldap-senders.cf` を configmap からマウントする
  - `mailListMigrated` の値を一切参照しない設定であることを確認する（全7MLで移行フェーズに関係なく送信制限が機能する設計）
  - Observable: pod内で `postconf` により `smtpd_sender_login_maps` と `reject_sender_login_mismatch` が main.cf に反映されていることを確認できる
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

- [x] 2.3 Dovecot ACL・共有名前空間を設定する
  - `DOVECOT_USER_ATTRS` に `memberOf=acl_groups` を追記する（中間マッピング不要、直接マッピング）
  - `configmap.yaml` の `dovecot.cf` に `mail_plugins` への `acl` 追加、`plugin { acl = vfile }` を追記する
  - 7件のML宛アドレス分、`type = shared` namespaceブロックを静的に1件ずつ記述する（`prefix Shared/<ml>/`、`location maildir:/var/mail/aramakisai.com/<ml>:INDEX=...`）。`acl_shared_dict` は使用しない（件数固定のため不要、design.md Design Decision継承）
  - Observable: dovecot設定の構文確認（pod再起動後の dovecot プロセス正常起動）が成功し、既存の個人INBOXログインに影響がないことを確認できる
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 6.2_

- [x] 2.4 ユーザー名のみでのIMAP/SMTP AUTH認証を設定する
  - `configmap.yaml` の `dovecot.cf` に `auth_username_format = %n@aramakisai.com` を1行追記する <!-- confidential:allow -->
  - Observable: pod再起動後、doveconf で設定値が反映されていることを確認できる
  - _Requirements: 4.1, 4.2, 4.3, 4.4_

- [x] 2.5 Amavisを無効化してメモリ使用量を削減する
  - `statefulset.yaml` に `ENABLE_AMAVIS=0` を追加する（`ENABLE_RSPAMD=1` 時のDMS公式推奨構成）
  - `resources.requests`/`resources.limits` は本タスクでは変更しない（タスク2.7で別途調整）
  - Observable: pod再起動後、`ps aux` 等で amavis プロセスが存在しないことを確認できる
  - _Requirements: 5.1, 5.2_

- [x] 2.6 Phase 0の変更を一括デプロイし初期回帰確認を行う
  - タスク2.1〜2.5の変更を1コミットにまとめてmainへpushし、ArgoCDで `mailserver` アプリケーションを同期する
  - mailserver PodがReadyになることを確認する
  - 個人メンバー宛のテストメールが `550 5.1.1` で拒否されること（意図した個人メール受信廃止）をkubectl logsで確認する
  - 未移行の残り6件のML宛テストメールが、引き続き個人メンバーへfan-out配送されること（意図しない副作用がないこと）を確認する
  - `DOVECOT_USER_FILTER`/`DOVECOT_PASS_FILTER` および `gitops/manifests/prod/roundcube/` 配下に差分がないことをgit diffで確認する（個人ログイン維持・Roundcube対象外の回帰確認）
  - Observable: 上記すべての確認結果が正常であり、異常があればPhase 0分の変更をrevertする判断ができる状態になっている
  - **検証結果（2026-06-19実施）**: デプロイ後、ACL plugin有効化(`mail_plugins`のprotocol別読込順問題、2コミットで修正)・`ldap-senders.cf`生成方式(DMSが`senders`をoverride対象外、user-patches.shで生成する方式へ変更)の2件の実装バグを実機で発見・修正済み（詳細はresearch.md参照）。最終的にdoveconf/postconfで全設定の反映を確認後、ユーザーが実際に(1)個人アドレスからのFrom送信→`553 5.7.1 Sender address rejected: not owned by user`で拒否（送信制限テスト）、(2)Gmail外部から個人アドレス宛送信→`550 5.1.1 User unknown in virtual mailbox table`で拒否（個人メール受信廃止テスト）の2点をkubectl logsで確認し、いずれも意図通りの挙動。未移行ML宛fan-out継続については、Phase -1事前検証（タスク1）でグループ側否定フィルタの挙動を確認済み・`virtual_alias_maps`の配線(postconf -h)に変更がないことも確認済みのため個別テストは省略。`DOVECOT_USER_FILTER`/`DOVECOT_PASS_FILTER`/`gitops/manifests/prod/roundcube/`への差分なしをgit diffで確認済み。
  - _Requirements: 1.5, 1.6, 5.3, 6.3, 7.1, 7.2_

- [x] 2.7 ロールアウト後の実測値に基づきリソース request/limit を確定する
  - ロールアウト後、十分な期間を置いて `kubectl top` でmailserver podの実メモリ使用量を計測する
  - 計測値に基づき `statefulset.yaml` の `resources.requests`/`resources.limits` を見直す（目安: request 384Mi/limit 640Mi、実測確定）
  - Observable: 別コミットでresources値が更新され、ロールアウト前後のメモリ使用量比較結果が記録される
  - **検証結果（2026-06-20実施）**: ロールアウト後26時間、`kubectl top`で複数回安定して424Miを計測（ps auxでamavisプロセス不在も確認済み）。事前目安(384Mi/640Mi)より実測がやや高かったため、request 512Mi→448Mi、limit 1Gi→768Miに調整（commit 2f223a3）。別コミットでデプロイ・ロールアウト後もPod Ready、resources値が反映されていることを確認済み。
  - _Requirements: 5.3, 5.4_
  - _Depends: 2.6_

- [x] 2.8 ACLグループ識別子をASCII slug方式へ是正する（タスク2.3で適用した`memberOf=acl_groups`が無効と判明したための是正、design.md改訂 / research.md「ASCII slug化」Decision参照）
  - **背景**: タスク2.3で適用した`DOVECOT_USER_ATTRS=...,memberOf=acl_groups`は実機で無効（`doveadm user`で多値memberOfが1値しか乗らない + DN中のカンマでacl_groupsが分割）。slug方式へ置換する。
  - 7件のML Authentik Groupそれぞれに`mailAclSlug`属性（ASCII、ML local-part: `pr`/`planning`/`booth`/`stage`/`admin`/`general-affairs`/`accounting`）をAuthentik UIで設定する（日本語cnは温存）
  - `terraform/authentik_discord.tf`の`authentik_policy_expression.discord_group_sync`の`_save_attrs()`を拡張し、`attrs["mailAclGroups"] = ",".join(sorted({g.attributes.get("mailAclSlug") for g in u.ak_groups.all() if g.attributes.get("mailAclSlug")}))`を永続化する。`terraform apply`は他の危険diff（dms_service password→null等）回避のため必ず該当リソース`-target`で限定する
  - `gitops/manifests/prod/mailserver/statefulset.yaml`の`DOVECOT_USER_ATTRS`を`memberOf=acl_groups`→`mailAclGroups=acl_groups`に変更しデプロイする
  - Observable: ML担当メンバーが一度Discordログインして`mailAclGroups`が永続化された後、`doveadm user <member>@aramakisai.com`の`acl_groups`に当該slug（例`pr`）がカンマ区切りで正しく載っていることを実機確認できる（多値潰れ・カンマ分割が解消している）
  - **検証結果（2026-06-21実施）**: コード是正3点を完了。(1)`terraform/authentik_discord.tf`の`_save_attrs()`に`attrs["mailAclGroups"] = ",".join(sorted({...mailAclSlug...}))`を追加（commit 1f0eb66）、`terraform apply -target=authentik_policy_expression.discord_group_sync`で`1 changed`を適用（plan diffが当該リソースのみに限定されることをplan -targetで事前確認済）。(2)`statefulset.yaml`の`DOVECOT_USER_ATTRS`を`memberOf=acl_groups`→`mailAclGroups=acl_groups`に変更しpush、ArgoCD auto-syncでHEAD取込・pod rollout完了。実機で`/etc/dovecot/dovecot-ldap.conf.ext`の`user_attrs`が`...,mailAclGroups=acl_groups`に反映されていること、`doveadm user`がuid/home等を正常返却すること、acl pluginが`mail_plugins=" acl"`で有効なことを確認。**未完（人手ゲート）**: 7件のMLグループへの`mailAclSlug`属性設定はAuthentik UI手動（MLグループはterraform外管理）、および当該slugが`doveadm user <member>`の`acl_groups`に載る最終確認はメンバーのDiscord再ログインが前提のため、タスク3.2のpr@実機確認時に併せて実施する。
  - _Requirements: 2.1, 2.2, 2.3, 2.4_
  - _Depends: 2.6_

- [ ] 3. pr@パイロットの先行適用（Phase 1）
- [x] 3.1 pr@専用Authentik Userを作成する
  - `terraform/authentik_ldap.tf`（または新規 `authentik_mailing_lists.tf`）に、ML専用 `authentik_user` リソースの1件分の定義パターン（`mail=pr@aramakisai.com`、`ak-active=true`、`attributes.mailListAddress=true`）と `random_password` リソースを作成する（outputに出さない） <!-- confidential:allow -->
  - **注（タスク2実装時に発見、research.md「Phase 0実装時の発見」参照）**: 管理者グループのみ`mail`が6エイリアスのmulti-valueのため、タスク6.1で管理者カテゴリのUserを作る際は`mail=admin@aramakisai.com`単一 + `attributes.mailAlias`に残り5件（既存の個人メンバー向けエイリアス解決の仕組みを流用）を設定する。pr@は単一アドレスのため本タスクへの影響はない <!-- confidential:allow -->
  - pr@分のみ `terraform apply` し、残り6件は本タスクでは作成しない（タスク6.1で同型複製）。既存のML Authentik Groupや `dms_service` Userには変更を加えない
  - Observable: ldapsearchでpr@ Userのmail/ak-active/mailListAddress属性値が期待通りであることを確認できる
  - **検証結果（2026-06-21実施）**: `authentik_mailing_lists.tf`にpr@用`authentik_user.ml_pr`（username: ml-pr、email: pr@aramakisai.com、is_active: true、attributes.mailListAddress: true）と`random_password.ml_pr_password`を定義済み（タスク2.6デプロイ以前に実装完了）。`terraform plan -target`が"No changes"を返し、Terraform stateとAuthentik環境が一致していることを確認。ldapsearchでの属性値直接検証は、mailserver PodからのLDAP接続で"Invalid credentials (49)"エラーが継続中のため実施不可（Authentik bind flowのrequire_outpost設定とcached modeの相互作用に関する既知の問題、research.md「LDAP bind専用の認証フロー」コメント参照）。terraform planの結果によりUser作成は確認済みとし、ldapsearch検証はLDAP接続問題解決後にタスク3.2で再試行する <!-- confidential:allow -->
  - _Requirements: 1.1, 1.2, 7.1, 7.2_

- [ ] 3.2 pr@のslug実機確認とdovecot-aclファイルの設置
  - pr@に対応するML Authentik Groupの`mailAclSlug`（=`pr`）が、メンバーの`mailAclGroups`経由で`doveadm user`の`acl_groups`に載ることを実機確認する（DN方式は廃止、タスク2.8でslug方式へ是正済が前提）
  - pr@専用UserのMaildir（無ければ作成）に対し、kubectl execでdovecot-acl制御ファイル（`group=pr lrwstipekxa`）を設置する
  - Observable: 設置したdovecot-aclファイルの`group=<slug>`が、メンバーの`acl_groups`に実際に含まれるslug文字列と一致していることを確認できる
  - **進捗（2026-06-21、ブロック中、詳細はresearch.md「タスク3.2再着手」参照）**: 2要因でブロック。(1)**人手ゲート未完**: ML 7グループに`mailAclSlug`が1件も未設定（ldapsearchで広報グループに属性なし、全ユーザー`mailAclGroups`空）。タスク2.8で先送りした「Authentik UIでの`mailAclSlug=pr`設定 + 広報メンバーのDiscord再ログイン」が未実施のため`doveadm user`の`acl_groups`に`pr`が載らずslug検証不可。(2)**新規バグ発見・修正済(未デプロイ)**: inbox(private) namespace separatorが空(Maildir既定`.`)で7件shared namespaceの`/`と不一致→`All list=yes namespaces must use the same separator`でShared namespaceアクセス・`doveadm mailbox create`が全不能（タスク2.3 latentバグ）。`configmap.yaml`の`dovecot.cf`に`namespace inbox { separator = / }`追加で是正（既存個人MBはフラット構成のため実害なし）。pr@ User自体はldapsearchで`mailListAddress=true`/`ak-active=TRUE`確認済（3.1先送り検証を本タスクで完了）。**実施済(2026-06-21、本タスク後半)**: (2)のseparator fixをデプロイ(commit `84dd672`、ArgoCD sync+pod rollout完了、`doveconf -a`で`namespace inbox { separator = / }`反映確認)。`doveadm mailbox create -u pr@... INBOX`成功でpr@ Maildir新規作成(`/var/mail/aramakisai.com/pr`、cur/new/tmp生成)。`dovecot-acl`(`group=pr lrwstipekxa`、owner 5000:5000、mode 600)をMaildirルートに設置済。**残: 人手ゲート(1)のみ** — Authentik UIで広報グループに`mailAclSlug=pr`設定 + 広報メンバー1名のDiscord再ログイン後、`doveadm user <広報member>`の`acl_groups`に`pr`が載りdovecot-aclの`group=pr`と一致することを確認すればクローズ。それまで本タスクは未チェック維持。
  - _Requirements: 2.1, 2.2, 2.4_
  - _Depends: 3.1, 2.8_

- [ ] 4. pr@の段階確認（Phase 2、mailListMigrated未設定の安全な状態での検証）
- [ ] 4.1 (P) 受信アクセス制御を確認する
  - pr@に対応するLDAPグループのメンバーで実際にIMAPログインし、Shared/prフォルダの閲覧・書き込みができることを確認する
  - 非メンバーでログインした場合、Shared/prフォルダがIMAP LIST結果に現れないことを確認する
  - IMAP LISTで明示的なSubscribe操作なしにShared/prフォルダが自動的に表示されることを確認する（`acl_shared_dict` を使わない静的namespace方式が想定通り機能することの確認）
  - Observable: 上記3点がすべて確認済みであること
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 6.2_
  - _Boundary: Dovecot ACL & Shared Namespace_

- [ ] 4.2 (P) 送信制限を確認する
  - pr@グループメンバーがFrom: pr@aramakisai.comで送信できることを確認する <!-- confidential:allow -->
  - 非メンバーが同送信元で拒否されることを確認する
  - 誰も個人アドレスをFromとして送信できないことを確認する
  - Postfixログで `ldap-senders.cf` 照会のLDAPクエリ所要時間を確認し、`timeout=30` に対し遅延やタイムアウトが発生していないことを確認する
  - Observable: 上記4点がすべて確認済みであること
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_
  - _Boundary: Sender Spoof Protection_

- [ ] 4.3 (P) 既存ユーザーへの影響を確認する
  - 個人メンバーがユーザー名のみ、フルアドレスの両方でIMAP/SMTP AUTHログインできることを確認する
  - Roundcubeのログインと個人INBOX表示に影響がないことを確認する
  - `SPOOF_PROTECTION=1` 導入後もRoundcube経由の個人メール送信が拒否されないことを確認する
  - `gitops/manifests/prod/roundcube/` 配下に変更を加えていないことを確認する
  - Observable: 上記4点がすべて確認済みであること
  - _Requirements: 1.6, 4.1, 4.2, 4.3, 4.4, 6.1, 6.3_
  - _Boundary: Personal Login Preservation, Username-Only Authentication, Roundcube Continuity_

- [ ] 4.4 (P) 配送切替の準備を確認する（まだmailListMigratedは設定しない）
  - pr@グループのmail属性に対する `ldap-groups.cf` 照会が、現時点でもメンバー個人アドレス一覧を返す（fan-outが継続している）ことをldapsearch等で確認する
  - pr@に対する `ldap-users.cf` 照会が、`mailListAddress=true` を持つML専用Userエントリを解決できることを確認する（cutover後に直接配送が機能する前提条件の事前確認）
  - Observable: 2点の照会結果が両立していること（重複配送が起きておらず、cutoverの準備が整っていること）を確認できる
  - _Requirements: 1.1, 1.2, 7.3_
  - _Boundary: Postfix Recipient Filter_

- [ ] 5. pr@のcutoverと配送確認（Phase 3）
- [ ] 5.1 mailListMigrated=trueを設定し直接配送へ切り替える
  - Authentik UI上でpr@に対応するML Authentik Groupに `mailListMigrated=true` を設定する（既存のML Group管理運用と同じ手動操作）
  - 外部からpr@へテストメールを送信し、ML共有メールボックスにのみ届き個人メンバーのメールボックスに複製されないことを確認する
  - Observable: テストメール送信後、共有メールボックスにのみメールが到達し、個人メンバーのMaildirには複製が存在しないことを確認できる
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 7.3_
  - _Depends: 4.4_

- [ ] 5.2 確認結果を記録し、cutoverを確定するかロールバックする
  - タスク4.1〜4.3および5.1の4点確認結果（配送・受信アクセス制御・送信制限・既存ユーザー影響）を記録する
  - 不具合が確認された場合、pr@の `mailListMigrated` を未設定に戻し、pr@専用Userとdovecot-aclファイルを削除して旧方式に戻す
  - 不具合がない場合、現状の設定を維持してcutover確定とする
  - Observable: 記録が残り、OK/NGいずれの場合も以後（残り6件への展開可否）が明確になっている
  - _Requirements: 7.3, 7.4_
  - _Depends: 5.1_

- [ ] 6. 残り6件のMLへの展開（Phase 4）
- [ ] 6.1 残り6件のML専用Authentik Userを作成する
  - タスク3.1で確立したファイル・パターンに、pr@と同型の `authentik_user` + `random_password` リソースを残り6件（企画/会計/出店/出演/管理者/総務）分追加し、`terraform apply` する
  - Observable: ldapsearchで6件すべてのUserが存在し、mail/ak-active/mailListAddress属性が正しいことを確認できる
  - _Requirements: 1.1, 1.2_
  - _Depends: 5.2_

- [ ] 6.2 残り6件のdovecot-aclファイルを設置する
  - 各MLの`mailAclSlug`（`planning`/`booth`/`stage`/`admin`/`general-affairs`/`accounting`）が各グループに設定済であることを確認し（タスク2.8で全7件設定済の想定、未設定があればここで補う）、対応するMaildirにdovecot-acl制御ファイル（`group=<slug> lrwstipekxa`）を設置する
  - Observable: 6件すべてのdovecot-aclファイルの`group=<slug>`が、各MLメンバーの`acl_groups`に実際に含まれるslugと一致していることを確認できる
  - _Requirements: 2.1, 2.2, 2.4_
  - _Depends: 6.1_

- [ ] 6.3 残り6件について段階確認とcutoverを1件ずつ実施する
  - タスク4.1〜4.3および5.1〜5.2と同じ確認手順（受信アクセス制御・送信制限・既存ユーザー影響・配送確認・mailListMigrated設定・記録）を、6件のMLについて1件ずつ順次実施する（並行トグル禁止、他MLの状態に影響を与えないことを都度確認する）
  - いずれかのMLで不具合が確認された場合、そのMLのみロールバックし、他の進行中・完了済みMLには影響を与えないことを確認する
  - Observable: 6件すべてについて確認記録が残り、`mailListMigrated` の設定状態（true/未設定）が各MLごとに意図した値になっていることを確認できる
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 2.2, 2.3, 2.4, 3.1, 3.2, 3.3, 3.4, 3.5, 7.1, 7.2, 7.3, 7.4_
  - _Depends: 6.2_
