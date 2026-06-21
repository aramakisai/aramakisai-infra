# Requirements Document

## Project Description (Input)
Authentik LDAP Outpost経由のbindが原因不明のInvalid credentials (49)で全滅している障害の調査・修正。docker-mailserver(Postfix/Dovecot)がAuthentik LDAP Outpost(authentik-ldap-outpost.prod.svc.cluster.local)へbindすると、パスワードが100%正しい場合でも常にInvalid credentials (49)で失敗する。Authentik Webログイン(同一ユーザー・同一パスワード)は問題なく成功する。

発生経緯: mailing-list-shared-mailbox spec タスク3.1(pr@ Authentik User作成のldapsearch検証)中に発覚。元はInfisicalのMAILSERVER_LDAP_BIND_PASSWORDとAuthentik実パスワードのドリフトを疑い調査開始したが、パスワードローテーションでは解決せず、より深い問題と判明した。

確定事実(authentik-serverログより):
- authentik.flows.stage.PasswordStageViewがInvalid credentialsを返す。username解決自体は正しい(pk/usernameともログに正しく出る)。
- LDAP Outpost経由のflow実行(/api/v3/flows/executor/<flow-slug>/?goauthentik.io/outpost/ldap=true)でのみ失敗。同じstageへのWebブラウザ経由の実行は成功。
- mailserver-service(pk=10)だけでなく、新規作成したml-pr(pk=19、terraform create時に設定したパスワード、絶対に正しい値)でも同じ失敗。ユーザー固有の問題ではない。

否定済みの仮説(すべて試して効果なし):
1. Infisical⇄k8s Secretのパスワードドリフト(根治後も再現)
2. パスワードの文字種(base64特殊文字 vs 英数字のみ)
3. Authentikのbrute-force/reputationブロック(Eventsログで2回の失敗のみ確認、ロックアウト表示なし)
4. authentik_provider_ldap.mfa_support(true→false、効果なし)
5. bind_flowにAuthenticator Validationステージが含まれる問題(LDAP bind専用の最小flow=identification+passwordのみ、MFAステージなしに切り替えても再現)
6. LDAP OutpostとAuthentik Serverのバージョン不一致(2026.5.2→2026.5.3に統一、再現)
7. flowのauthentication設定(none→require_outpostに変更すると別の新エラー"Flow not applicable to current user"+"Attempted remote-ip override without token"が発生、noneに戻すと元のInvalid credentialsに復帰)
8. Provider/Application/Outpost/Tokenの完全削除→再作成(GitHub issue #14210の「全部作り直したら直った」という解決例に倣ったが再現)
9. TokenBackend経由のApp Password(authentik_token, intent=app_password)でのbind(同じく失敗)

現在のterraform状態(commit 4fc7826, push済み):
- terraform/authentik_ldap.tfにLDAP bind専用flow(authentik_flow.ldap_bind、slug=ldap-bind-flow、authentication=none、identification+passwordのみ)を新設し、authentik_provider_ldap.dmsのbind_flow/unbind_flowをこれに向けている。mfa_support=falseも設定。
- これらの変更自体は妥当な簡素化(MFAステージ除外)として維持する。
- Provider/Application/Outpost(authentik_outpost.dms_ldap)は一度destroy→再作成済み(新ID)。Outpost Tokenも新規発行・Infisical(AUTHENTIK_LDAP_OUTPOST_TOKEN)・k8s Secret(authentik-ldap-outpost-token)に反映済み。

未検証・次の手がかり:
- goauthentik Discord/GitHub Discussionsで同一症状(Web成功・LDAP失敗・パスワード確実に正しい)を検索し質問を立てる
- Authentik server本体のログをDEBUGレベルに上げて、PasswordStageViewが実際にどのbackendsをどう評価しているか(authenticate()呼び出しの詳細)を直接確認する
- authentik-serverをexec/Django shellで直接User.objects.get(username="ml-pr").check_password(<known_password>)を実行し、ハッシュ比較自体がTrueになるか確認(flow実行を経由しない直接検証)
- LDAP Outpostのbind_modeをcachedに変更して挙動が変わるか確認
- ネットワーク経路(Cilium NetworkPolicy等)がOutpost→authentik-server間の特定リクエストだけ何か変質させていないか確認

影響: mailing-list-shared-mailbox spec タスク3(pr@パイロット)はこの障害でブロック中。タスク3.1のterraform側(authentik_user.ml_pr作成)は完了しているが、ldapsearchでの検証ができない。タスク3.2(DN確認・ACL設置)も同様に着手不可。

本番影響: 現在DMSのPostfix/Dovecot LDAP問い合わせ(配送判定・送信者制限・IMAPログイン)が全滅中の可能性が高い(要再確認)。

## はじめに

docker-mailserver (DMS) の Postfix/Dovecot が Authentik LDAP Outpost (`authentik-ldap-outpost.prod.svc.cluster.local`) へ bind する際、パスワードが100%正しい場合でも常に `Invalid credentials (49)` で失敗する障害が発生している。同一ユーザー・同一パスワードでの Authentik Web ログインは問題なく成功しており、認証情報自体に問題はない。

本障害は `mailing-list-shared-mailbox` spec のタスク3 (`pr@` パイロット) で `ldapsearch` 検証を行った際に発覚した。当初は Infisical の `MAILSERVER_LDAP_BIND_PASSWORD` と Authentik 実パスワードのドリフトを疑ったが、ローテーションでは解決せず、既存サービスアカウント (`mailserver-service`, pk=10) だけでなく新規作成した `ml-pr` (pk=19、Terraform 作成時に設定した既知の正しいパスワード) でも同一症状が再現することから、ユーザー固有の問題ではなく Outpost / Provider / Flow に共通する問題であると判明している。

既に9件の仮説 (Infisical⇄k8s Secret のパスワードドリフト、パスワードの文字種、brute-force/reputation ブロック、`mfa_support` 設定、bind_flow の Authenticator Validation ステージ混入、LDAP Outpost/Server バージョン不一致、flow の `authentication` 設定変更、Provider/Application/Outpost/Token の完全再作成、TokenBackend 経由 App Password) を検証済みだが、いずれも症状を解消していない。現在 Terraform 上では LDAP bind 専用の最小 Flow (`authentik_flow.ldap_bind`, identification + password のみ, `authentication = none`) に切り替え済みだが、これは構成の簡素化として妥当なものの根本原因の解消には至っていない (`terraform/authentik_ldap.tf`, commit `4fc7826`)。

本障害により `mailing-list-shared-mailbox` spec のタスク3 (タスク3.1 の `ldapsearch` 検証、タスク3.2 の DN 確認・ACL 設置) がブロックされており、本番の Postfix/Dovecot による LDAP 問い合わせ (配送判定・送信者制限・IMAP ログイン) も機能不全の可能性が高い。

## Boundary Context

- **In scope**:
  - LDAP Outpost 経由の bind が `Invalid credentials (49)` で失敗する根本原因の特定
  - 根本原因に基づく恒久的な修正の実装・適用 (Terraform 管理下の Authentik リソースが中心)
  - `mailserver-service` (pk=10) および `ml-pr` (pk=19) を用いた `ldapsearch` での bind 成功検証
  - 本番 Postfix/Dovecot の LDAP 問い合わせ機能 (配送判定・送信者制限・IMAP ログイン) への影響評価と復旧確認
  - 調査で得た知見 (否定済み仮説・根本原因・回避策の限界) の記録
- **Out of scope**:
  - `mailing-list-shared-mailbox` spec タスク3.2 の DN 確認・ACL 設置作業そのもの (本spec はそのブロック解除のみを担う)
  - Authentik 本体や LDAP Outpost イメージのメジャーバージョンアップグレード・別 IdP への移行検討
  - Web ログイン・OIDC/SAML 等の他の認証経路への新規機能追加 (回帰がないことの確認のみが対象)
- **Adjacent expectations**:
  - `mailing-list-shared-mailbox` spec のタスク3 は本spec の修正完了をもってブロック解除される前提
  - `terraform/authentik_ldap.tf` の既存リソース (`authentik_flow.ldap_bind`, `authentik_provider_ldap.dms`, `authentik_outpost.dms_ldap` 等) は維持しつつ変更を加える前提とし、無関係な再設計は行わない

## Requirements

### Requirement 1: 根本原因の特定
**Objective:** インフラ担当者として、LDAP Outpost 経由の bind が `Invalid credentials (49)` で失敗する根本原因を特定したい。そうすることで対症的な回避策ではなく恒久的な修正を適用できる。

#### Acceptance Criteria
1. While 根本原因が未確定の場合、the 調査プロセス shall 既に否定済みの9件の仮説 (Infisical⇄k8s Secret パスワードドリフト、パスワードの文字種、brute-force/reputation ブロック、`mfa_support` 設定、Authenticator Validation ステージ混入、LDAP Outpost/Server バージョン不一致、flow の `authentication` 設定変更、Provider/Application/Outpost/Token の完全再作成、TokenBackend 経由 App Password) を再試行対象から除外する
2. The 調査プロセス shall `mailserver-service` (pk=10) と `ml-pr` (pk=19) の両方で症状が再現することを根拠に、調査対象をユーザー固有要因ではなく Outpost / Provider / Flow に共通する要因へ限定する
3. When 新たな仮説が立てられた場合、the 調査担当者 shall Authentik flow 実行を経由しない手段 (例: authentik-server の DEBUG ログでの `authenticate()` 呼び出し詳細確認、Django shell での `User.objects.get(username=...).check_password(...)` 直接実行、ネットワーク経路上の検証等) で当該仮説を裏付ける、または反証するエビデンスを取得する
4. When 根本原因が確定した場合、the 調査担当者 shall その原因と再現条件 (発生する条件・発生しない条件) を本spec の design.md またはタスク記録に明文化する

### Requirement 2: LDAP bind 成功の実現 (恒久修正)
**Objective:** メールサーバー運用者として、DMS (Postfix/Dovecot) が Authentik LDAP Outpost 経由で正しいパスワードによる bind に成功するようにしたい。そうすることでメール配送判定・送信者制限・IMAP ログインの LDAP 問い合わせが機能する。

#### Acceptance Criteria
1. When `mailserver-service` (pk=10) が正しいパスワードで LDAP Outpost (`authentik-ldap-outpost.prod.svc.cluster.local`) へ bind した場合、the Authentik LDAP Outpost shall bind 成功 (LDAP result code 0) を返す
2. When `ml-pr` (pk=19) が正しいパスワードで LDAP Outpost へ bind した場合、the Authentik LDAP Outpost shall bind 成功を返す
3. If 誤ったパスワードで bind が試行された場合、the Authentik LDAP Outpost shall `Invalid credentials (49)` を返す (失敗系の正規動作は維持する)
4. The 修正 shall Terraform (`terraform/authentik_ldap.tf` 等) または Ansible/GitOps 管理下の構成変更として適用され、Authentik WebUI 上でのみ存在する暫定対応に留めない

### Requirement 3: 既存認証経路への回帰防止
**Objective:** 委員会メンバーとして、LDAP bind 修正後も Authentik Web ログインや既存の OIDC 連携が変更前と同様に動作してほしい。そうすることで認証基盤全体の安定性が損なわれない。

#### Acceptance Criteria
1. When 修正適用後にユーザーが Authentik Web ブラウザ経由でログイン (同一ユーザー・同一パスワード) した場合、the Authentik shall 修正前と同様にログインに成功する
2. The 修正 shall `default-authentication-flow` (Web/SSO 共通フロー) の `authentication` 設定 (`none`) を変更しない
3. When 修正適用後に他のアプリケーション (ArgoCD, Discord 連携等) の OIDC ログインが行われた場合、the Authentik shall 修正前と同様に認証を成功させる

### Requirement 4: ldapsearch による検証とブロック解除
**Objective:** インフラ担当者として、修正後に `ldapsearch` を用いた実機検証で bind 成功を確認したい。そうすることで `mailing-list-shared-mailbox` spec タスク3 のブロックを解除できる。

#### Acceptance Criteria
1. When 検証担当者が `mailserver-service` (pk=10) の認証情報で `ldapsearch` を実行した場合、the LDAP Outpost shall bind に成功し検索結果を返す
2. When 検証担当者が `ml-pr` (pk=19) の認証情報で `ldapsearch` を実行した場合、the LDAP Outpost shall bind に成功し検索結果を返す
3. The 検証 shall `mailing-list-shared-mailbox` spec タスク3.1 (`ldapsearch` 検証) およびタスク3.2 (DN 確認・ACL 設置) が着手可能な状態であることの確認を含む

### Requirement 5: 本番影響の評価と復旧確認
**Objective:** メールサービス利用者として、本障害で停止している可能性がある Postfix/Dovecot の LDAP 問い合わせ機能 (配送判定・送信者制限・IMAP ログイン) が修正後に復旧してほしい。そうすることでメール配送・ログインが本来の挙動に戻る。

#### Acceptance Criteria
1. The 調査プロセス shall 修正適用前に、本番の Postfix (配送判定・送信者制限) と Dovecot (IMAP ログイン) の LDAP 問い合わせの実際の失敗状況をログ (`mail.log` 等) から確認する
2. When 修正が適用された場合、the docker-mailserver shall Postfix/Dovecot の LDAP 問い合わせ (`virtual_alias_maps` 等) が正常に成功する状態に復旧する
3. If 修正適用後も Postfix/Dovecot の LDAP 問い合わせに失敗が残る場合、the 調査担当者 shall 残存する失敗の原因を切り分けて記録する

### Requirement 6: 知見の記録と運営チームへの引き継ぎ
**Objective:** 将来の運営チームとして、本障害の調査過程・否定済み仮説・根本原因・対処方法を記録として残したい。そうすることで同種障害の再発時に同じ調査を繰り返さずに済む。

#### Acceptance Criteria
1. The 調査担当者 shall 否定済み仮説の一覧と根本原因の結論を本spec内 (design.md 等) に記録する
2. If 本障害が goauthentik 本体の既知 issue (#14210 等) に起因することが確定した場合、the 調査担当者 shall 当該 issue への参照と回避策の限界を記録する
3. When 修正が `terraform/authentik_ldap.tf` の構成変更を伴う場合、the 調査担当者 shall CLAUDE.md の「変更時更新ナビゲーション」チェックリストに従い `.kiro/steering/tech.md` の該当セクションへ知見を追記する
