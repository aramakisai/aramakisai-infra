# Implementation Plan

- [ ] 1. Zitadel基盤のk3d検証環境構築とブートストラップ
- [ ] 1.1 Zitadelをk3d検証環境へデプロイする
  - namespace・StatefulSet(api/login)・Service・CNPG DBクラスタ・ExternalSecret相当のマニフェストをk3d向けに用意する
  - login UI(Node.js)を含めた4コンポーネント合計のメモリ実測値を記録する
  - k3dクラスタで`docker stats`/`kubectl top`相当により全コンポーネントがRunning状態になることを確認する
  - _Requirements: 1.1, 1.2, 1.3, 1.4_

- [ ] 1.2 Zitadel初回admin/PATのAnsibleブートストラップを実装する
  - `infisical-auth`と同型の例外ブートストラップとして、Zitadel初回起動後に組織・管理者ユーザーを作成しTerraform provider用Service User/PATを発行するAnsibleロールを作成する
  - 発行したPAT/Service User TokenをInfisicalへ登録する
  - 再実行時に既発行トークンをスキップまたは再発行できる冪等な手順として動作することを確認する
  - _Requirements: 11.1, 11.2, 11.3_
  - _Depends: 1.1_

- [ ] 2. Zitadel Terraform IaC定義
- [ ] 2.1 (P) Project/Roleとフラットロール配布を定義する
  - プロジェクト単位のフラットロール(キー・表示名のみ)を定義し、permission行列は導入しない
  - 「Assert Roles on Authentication」を有効化しロールがOIDC ID Token/Userinfoのクレームへ配布されることを確認する
  - authentikのrbac_role/permission_role相当の細粒度権限管理は移行対象に含めない
  - _Requirements: 2.2, 8.1, 8.2, 8.3_
  - _Boundary: Zitadel Terraform Provider定義_
  - _Depends: 1.2_

- [ ] 2.2 (P) RPアプリ向けOIDC Application定義を作成する
  - CMS/Vaultwarden/Roundcubeそれぞれに対応するOIDC Client(Authorization Code Flow + PKCE)をTerraformで定義する
  - 各Clientのclient_id/secret/redirect_uriが払い出されることを確認する
  - _Requirements: 2.1, 2.2, 2.3_
  - _Boundary: Zitadel Terraform Provider定義_
  - _Depends: 1.2_

- [ ] 2.3 (P) Actions v2のwebhook Target/Executionを定義する
  - ロール/グループ変更イベントを外部エンドポイントへ通知するTarget・Executionリソースを定義する
  - `ZITADEL-Signature`署名キーが払い出されることを確認する
  - _Requirements: 4.1_
  - _Boundary: Zitadel Terraform Provider定義_
  - _Depends: 1.2_

- [ ] 2.4 Discordソーシャルログイン用OAuth2 IdP連携を設定する
  - 単純なOAuth2ログインのみを提供するIdP設定を追加する(ロール同期・アバター同期・動的グループ判定は実装しない)
  - Discord経由の認可コードフローが最後までOIDCトークン発行に到達することを確認する
  - _Requirements: 5.1, 5.2_
  - _Depends: 1.2_

- [ ] 3. Dovecot Lua Auth Bridge実装
- [ ] 3.1 lua passdbによるZitadel Session API認証委譲を実装する
  - Dovecotのpassdbからパスワードを受け取りZitadel Session API(`POST /v2/sessions`)へ都度問い合わせる処理を実装する
  - パスワードのJSONエンコードは正規のエスケープ処理(文字列連結禁止)を用いる
  - 正しいパスワードでのbind成功・誤ったパスワードでの拒否がそれぞれ観測できることを確認する
  - _Requirements: 3.1, 3.3, 3.4_
  - _Boundary: Dovecot Lua Auth Bridge_

- [ ] 3.2 (P) ACLグループ判定ロジックを実装する
  - Session成功後にManagement APIでuser_grant(ロール)を取得し、mail属性・ACLグループへマッピングする処理を実装する
  - Zitadelのproject role割り当てを唯一の真実源泉とし、Dovecot側に独立した第二のグループ台帳を持たせない
  - 複合ANDフィルタ相当の検索結果としてmail属性・ACLグループが正しく返ることを確認する
  - _Requirements: 3.3, 5.3_
  - _Boundary: Dovecot Lua Auth Bridge_
  - _Depends: 3.1_

- [ ] 3.3 (P) Zitadel API障害時のフェイルモードを実装する
  - タイムアウト・5xx応答時にDovecot標準の一時エラー(temporary failure)として扱い、認証失敗と区別する処理を実装する
  - API障害を模擬した際にクライアントが誤ったパスワード変更を誘発されないことを確認する
  - _Requirements: 3.4_
  - _Boundary: Dovecot Lua Auth Bridge_
  - _Depends: 3.1_

- [ ] 3.4 Roundcube向けoauth2 passdbのintrospection先をZitadelへ切り替える
  - Dovecotのoauth2 passdb設定のintrospection_urlをZitadelのtoken introspectionエンドポイントへ変更する
  - RoundcubeからのOAUTHBEARER/XOAUTH2認証がintrospection成功後にログイン許可されることを確認する
  - _Requirements: 3.2_
  - _Boundary: Dovecot Lua Auth Bridge_

- [ ] 3.5 Session API呼び出し用PATの最小権限スコープを実機検証する
  - IAM_OWNER相当ではなくSession API呼び出しに必要な最小スコープへ絞れるかZitadel実機で検証する
  - 絞り込んだ最小スコープのPATでSession API呼び出しが成功することを確認する
  - _Requirements: 3.4_
  - _Depends: 3.1_

- [ ] 4. vaultwarden-rbac-syncのwebhook常駐化
- [ ] 4.1 Actions v2 webhook受信エンドポイントを実装する
  - `ZITADEL-Signature`ヘッダ(HMAC)を検証し、未検証リクエストを拒否する処理を実装する
  - 同一イベントの再送(at-least-once配信)に対して冪等に処理されることを確認する
  - authentik固有APIへの依存を除去しZitadel Management/User APIへ問い合わせる処理に置き換える
  - _Requirements: 4.1, 4.2_
  - _Boundary: vaultwarden-rbac-sync_

- [ ] 4.2 CronJob方式から常駐Deploymentへ構成変更する
  - 既存CronJob + Trigger Receiver定義を削除し、常駐Deployment + クラスタ内Serviceへ置き換える(外部公開は行わない)
  - 常駐PodがZitadelからのwebhookをクラスタ内Service経由で受信できることを確認する
  - _Requirements: 4.2_
  - _Boundary: vaultwarden-rbac-sync_
  - _Depends: 4.1_

- [ ] 4.3 Falco誤検知除外ルールを新プロセス形態に合わせて更新する
  - 常駐Deploymentのプロセス名・イメージに合わせて`gitops/helm-values/prod/falco.yaml`の除外ルールを更新する
  - 更新後、正常なwebhook受信動作でFalcoアラートが誤発報しないことを確認する
  - _Requirements: 4.2_
  - _Depends: 4.2_

- [ ] 5. RPアプリのOIDC Client切替
- [ ] 5.1 (P) CMSのOIDC Client設定をZitadelへ切り替える
  - CMSのclient_id/secret/redirect_uri設定をZitadel発行のものへ更新する
  - CMSからZitadel経由でログインしセッションが開始されることを確認する
  - _Requirements: 2.3_
  - _Boundary: CMS_
  - _Depends: 2.2_

- [ ] 5.2 (P) VaultwardenのOIDC Client設定をZitadelへ切り替える
  - Vaultwardenのclient_id/secret/redirect_uri設定をZitadel発行のものへ更新する
  - VaultwardenからZitadel経由でログインしセッションが開始されることを確認する
  - _Requirements: 2.3_
  - _Boundary: Vaultwarden_
  - _Depends: 2.2_

- [ ] 5.3 (P) RoundcubeのOIDC/introspection設定をZitadelへ切り替える
  - RoundcubeのOAuth2クライアント設定をZitadel発行のものへ更新する
  - RoundcubeからZitadel経由でログインしセッションが開始されることを確認する
  - _Requirements: 2.3_
  - _Boundary: Roundcube_
  - _Depends: 2.2_

- [ ] 6. 既存ユーザー・グループデータの招待ベース移行
- [ ] 6.1 既存authentikユーザーの招待コード一括発行手順を実装する
  - 既存authentikの全ユーザー(email・グループ所属含む)をZitadelユーザーとして作成し招待コード(`CreateInviteCode`/`ResendInviteCode`)を発行する手順を実装する
  - 移行後にログインできないユーザーが発生した場合の原因調査手順とロールバック手順を運用ドキュメントとして提供する
  - 招待メール受信からユーザーが初回ログインに成功するまでを確認する
  - _Requirements: 6.1, 6.2, 6.3_

- [ ] 7. 段階的カットオーバーとロールバック手順の整備
- [ ] 7.1 新旧並行稼働可能な切替順序を定義する
  - authentikとZitadelが並行稼働する期間中のRPアプリ・メール認証の切替順序をGitOps原則(`gitops/`配下変更→ArgoCD sync)に従って定義する
  - 定義した切替順序通りに1つのRPアプリを切り替えても他のRPアプリ・メール認証に影響がないことを確認する
  - _Requirements: 7.1, 7.3_
  - _Depends: 2, 3, 4, 5_

- [ ] 7.2 authentik構成への切り戻し手順を整備する
  - Zitadel切替後に重大な認証障害が発生した場合の、旧authentik構成への切り戻し手順を作成する
  - _Requirements: 7.2_
  - _Depends: 7.1_

- [ ] 8. セキュリティ検証(モンキーテスト)
- [ ] 8.1 (P) ブルートフォース対策とユーザー列挙耐性を検証する
  - 誤ったパスワードでの連続ログイン試行に対するレート制限/アカウント一時ロックアウトを検証する
  - 存在しないユーザー名でのログイン試行が実在ユーザーの誤パスワード試行と区別不能なレスポンスを返すことを確認する
  - _Requirements: 9.1, 9.2_
  - _Boundary: Zitadel Core_
  - _Depends: 1.1_

- [ ] 8.2 (P) 認可コードreplay・PKCE不一致・redirect_uri改ざんを検証する
  - 発行済み認可コードの2回目使用が拒否されることを確認する
  - PKCE code_verifier不一致でのトークン交換が拒否されることを確認する
  - redirect_uri改ざんによる認可コード発行が拒否されエラーが返ることを確認する
  - _Requirements: 9.3, 9.4, 9.5_
  - _Boundary: Zitadel Core_
  - _Depends: 2.2_

- [ ] 8.3 (P) 招待コードの期限切れ・再利用防止を検証する
  - 有効期限切れまたは2回目使用の招待コードで招待完了(パスワード設定)が拒否されることを確認する
  - _Requirements: 9.6_
  - _Boundary: Zitadel Core_
  - _Depends: 6.1_

- [ ] 8.4 セキュリティ検証結果を記録として残す
  - 上記モンキーテストのテストスクリプトと結果をk3d検証環境の記録として保存する
  - _Requirements: 9.7_
  - _Depends: 8.1, 8.2, 8.3_

- [ ] 9. 機能検証(正常系動作確認)
- [ ] 9.1 (P) OIDC Authorization Code Flow + PKCEのE2E成功を確認する
  - 正しいユーザー名・パスワードで認可コード発行・トークン交換・Userinfo取得までEnd-to-Endで成功することを確認する
  - _Requirements: 10.1_
  - _Boundary: Zitadel Core_
  - _Depends: 2.2_

- [ ] 9.2 (P) 各RPアプリの実ログインを確認する
  - CMS/Vaultwarden/Roundcubeそれぞれで実際にOIDCログインしセッションが開始されることを確認する
  - _Requirements: 10.2_
  - _Boundary: CMS, Vaultwarden, Roundcube_
  - _Depends: 5.1, 5.2, 5.3_

- [ ] 9.3 (P) Dovecot認証(一般IMAP/POP3・Roundcube)の動作を確認する
  - 一般IMAP/POP3クライアントが正しいパスワードで認証しmail属性・ACLグループが正しく返ることを確認する
  - RoundcubeのOAUTHBEARER/XOAUTH2認証がintrospection成功後にログイン許可されることを確認する
  - _Requirements: 10.3, 10.4_
  - _Boundary: Dovecot Lua Auth Bridge_
  - _Depends: 3.2, 3.4_

- [ ] 9.4 (P) vaultwarden-rbac-syncの反映を確認する
  - ユーザーのグループ/ロール割り当て変更がActions v2 webhook経由で検知されVaultwarden Collection権限へ反映されることを確認する
  - 反映までの実測遅延を記録する
  - _Requirements: 10.5_
  - _Boundary: vaultwarden-rbac-sync_
  - _Depends: 4.2_

- [ ] 9.5 招待フローのE2E成功を確認する
  - 招待メール受信からリンク遷移・パスワード設定・初回ログインまでEnd-to-Endで成功することを確認する
  - _Requirements: 10.6_
  - _Depends: 6.1_

- [ ] 9.6 ロールクレームの反映を確認する
  - ユーザーにロールを付与した際、OIDC ID Token/Userinfoのクレームへ設計通り反映されることを確認する
  - _Requirements: 10.7_
  - _Depends: 2.1_

- [ ] 9.7 ロールバック手順を実地検証する
  - 旧authentik構成への切り戻し手順を実際に実行し、切り戻し後に既存アプリのログインが復旧することを確認する
  - _Requirements: 10.8_
  - _Depends: 7.2_
