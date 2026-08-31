# Implementation Plan

- [x] 1. 基盤: ローカルK3s(k3d)検証環境でのAuthelia/LLDAP最小構成確立
  - **このtaskのスコープ**: LLDAP/AutheliaがLDAP認証・OIDC認証として技術的に機能するかの最小疎通確認のみを行う(汎用テストクライアント・単一テストユーザーでの検証)。
  - **スコープ外(後続taskで検証)**: 本番authentikの実際の構成の再現は本taskでは行わない。以下の観点は後続taskで段階的に検証する。
    - 本番相当のカスタム属性スキーマ(`ak-active`/`mail-list-address`/`mail-alias`/`mail-acl-groups`)の定義・投入 → task 2.1
    - 本番6 RP分(CMS prod, Roundcube, Vaultwarden, ArgoCD, Cloudflare Access, room-presence)のOIDC client定義・実クライアントでのログイン確認 → task 3, 7.2
      (spec作成後にDirectusは撤去されPayload CMSへ移行済みのため、Directus prod/stgはCMS prodへ読み替え)
    - Dovecotの実フィルタ式(`LDAP_QUERY_FILTER_USER`等)を用いたLDAP認証確認 → task 2.2
    - 既存authentikユーザー・グループデータの移行および件数突き合わせ → task 5, 6.2
    - vaultwarden-rbac-syncのグループ同期(`LldapGroupClient`)の疎通確認 → task 4
  - _Requirements: 1.1, 1.2_

- [x] 1.1 (P) k3dクラスタ構築とLLDAPデプロイ・LDAP認証フロー再検証
  - **目的**: LLDAPがDovecot型のLDAP認証シーケンス(サービスアカウントbind→複合ANDフィルタ検索→ユーザー自身のパスワードでのauth bind)に対応できることを、本番相当のK3sバージョン上で再確認する。
  - Docker上にk3dクラスタを1台起動し本番prod-node-1と同等のK3sバージョン(`v1.36.3+k3s1`、`.kiro/steering/tech.md`参照)で稼働させる。イメージ例: `k3d cluster create <name> --image rancher/k3s:v1.36.3-k3s1`
  - LLDAPの最小構成(SQLite永続化なしの一時起動でよい、標準の`mail`属性のみでカスタムスキーマは投入しない)をデプロイする
  - 検証手順:
    1. サービスアカウント(またはPoCではadmin)でLDAP bind
    2. `(&(objectClass=inetOrgPerson)(mail=<test-mail>))`の複合ANDフィルタで検索しユーザーエントリが1件取得できることを確認
    3. 取得したユーザーDNに対し、ユーザー自身の正しいパスワードでauth bindが成功することを確認
    4. 同じDNに誤ったパスワードでauth bindを試み、`Invalid credentials`(LDAP result code 49 / `invalidCredentials`)で拒否されることを確認
  - 検証ツール: `ldapsearch`/`ldapwhoami`(openldap-clients)が環境になければ、同等の検証をPython `ldap3`ライブラリ(bind/search/bind)で代替してよい。システムパッケージを追加インストールする場合はユーザーに確認する
  - _Requirements: 3.1, 3.2, 3.3_
  - _Boundary: LLDAP Deployment_

- [x] 1.2 k3d上へのAuthelia最小構成デプロイとOIDC疎通確認
  - **目的**: AutheliaがLLDAPをLDAP authentication backendとして参照し、OIDC Authorization Code + PKCEフローでid_token/access_tokenを発行できることを最小構成で確認する。本番相当のOIDC clientはtask 3で定義する。
  - Autheliaの最小構成をk3dクラスタへデプロイする。最低限必要な設定項目(バージョンにより差異あり、`authelia crypto hash generate`等で生成):
    - `authentication_backend.ldap`: LLDAPをLDAP backendとして参照(`address`/`base_dn`/`users_filter`/`groups_filter`等)
    - `identity_providers.oidc`: `hmac_secret`、署名鍵(`jwks`または`issuer_private_keys`、バージョン依存)、テスト用クライアント1件(`client_secret`はハッシュ化必須)
    - `storage.encryption_key`、`session.secret`
    - `session.cookies[].authelia_url`/`default_redirection_url`はhttps必須(自己署名証明書で可)
  - `/.well-known/openid-configuration`がDiscovery documentを返すことを確認する(`issuer`/`authorization_endpoint`/`token_endpoint`等)
  - 汎用OIDCテストクライアント(既存の`oidc-debugger`相当ツール、または自作スクリプトでよい)でAuthorization Code + PKCEフローを実行し、LLDAP上のテストユーザーでid_token/access_tokenが発行されることを確認する。検証手順の目安:
    1. `POST /api/firstfactor`でLLDAPバックエンドのユーザー認証
    2. PKCE code_verifier/code_challenge(S256)を生成し`GET /api/oidc/authorization`で認可コード取得(`state`は8文字以上必須)
    3. `POST /api/oidc/token`で認可コードをid_token/access_tokenに交換
    4. 任意で`GET /api/oidc/userinfo`によりLLDAPのmail属性がクレームとして取得できることを確認
  - _Requirements: 1.2, 2.1_
  - _Boundary: Authelia Deployment_
  - _Depends: 1.1_

- [ ] 2. 本番向けLLDAP gitops manifestとDovecot接続先の実装
- [x] 2.1 LLDAP本番gitops manifest実装(Deployment/PVC/ExternalSecret)
  - `gitops/manifests/prod/lldap/`にDeployment・PVC(SQLite永続化)・ExternalSecret(admin password, JWT secret)・VolSync ReplicationSourceを実装する
  - ユーザーエントリのカスタム属性スキーマ(`ak-active`, `mail-list-address`, `mail-alias`, `mail-acl-groups`)をLLDAP命名制約(アンダースコア不可)に適合させて定義する
  - k3d環境へapplyし、Pod起動後にGraphQL API(`/api/graphql`)へのadmin認証が成功することを確認する
  - _Requirements: 1.1, 1.2_
  - _Boundary: LLDAP Deployment_

- [x] 2.2 mailserver statefulsetのLDAP接続先・フィルタ式変更
  - `LDAP_SERVER_HOST`をLLDAPのクラスタ内Service DNSへ変更する
  - `LDAP_SEARCH_BASE`・`LDAP_BIND_DN`・`LDAP_QUERY_FILTER_USER`等の各フィルタ式をLLDAPの属性命名に合わせて更新する(既存の`ak-active`/`mail-list-address`はハイフン区切りのためそのまま流用可能)
  - k3d環境でDovecot型の複合ANDフィルタクエリ(task 1.1で確立した手順)を新しいフィルタ式定義に対して再実行し、期待通りの検索結果が得られることを確認する
  - _Requirements: 3.1, 3.2, 3.4_
  - _Boundary: mailserver statefulset変更_
  - _Depends: 2.1_

- [x] 3. (P) 本番向けAuthelia gitops manifest実装
  - `gitops/manifests/prod/authelia/`にDeployment・PVC(SQLite永続化)・ExternalSecret(JWT secret, LDAP bind password, OIDC client secrets)・VolSync ReplicationSourceを実装する
  - `identity_providers.oidc.clients[]`に既存`terraform/authentik_apps.tf`の6 Provider(Roundcube, ArgoCD, Cloudflare Access, Vaultwarden, room_presence, cms_prod)分のclient定義を機械的に書き写す(client_id/redirect_uris/scopesを踏襲、client_secretは新規発行)。Directus prod/stgはspec作成後に撤去済みのため対象外
  - RoundcubeクライアントについてはLLDAPの`mail-acl-groups`属性をclaims_policiesで`userdb_acl_groups`クレームとしてexposeする設定を追加する
  - k3d環境へapplyし、6 Client定義がすべてAuthelia起動時のバリデーションエラーなく読み込まれることを確認する
  - _Requirements: 1.1, 1.2, 2.1, 2.2_
  - _Boundary: Authelia Deployment_
  - _Depends: 2.1_

- [x] 4. vaultwarden-rbac-sync LldapGroupClient実装
- [x] 4.1 (P) LldapGroupClientクラスの実装
  - グループ名を受け取りLLDAP GraphQL APIへメンバーシップクエリを送信し、既存`AuthentikGroupClient`と同一の戻り値型(`GroupMembersResult`)でメンバーemail一覧を返す処理を実装する
  - グループが存在しない場合は例外を投げず`GroupMembersResult.error`に記録し、認証エラー・タイムアウトは例外として呼び出し元へ伝播させる分岐を実装する
  - `build_clients_from_env`内のクライアント生成を`LldapGroupClient`に差し替える
  - k3d環境のLLDAPに対し実際にクエリを送信し、存在するグループ・存在しないグループ双方で期待通りの戻り値が得られることを確認する
  - _Requirements: 4.1, 4.2_
  - _Boundary: LldapGroupClient_
  - _Depends: 2.1_

- [x] 4.2 (P) LldapGroupClient向けユニットテストの追加
  - 既存`tests/test_task5_sync_orchestrator.py`等が`AuthentikGroupClient`をモックしている箇所を`LldapGroupClient`用モックに置き換える
  - グループ存在時・不在時・API障害時の3分岐を検証するユニットテストを追加する
  - 追加したテストがすべてパスすることを確認する
  - _Requirements: 4.1, 4.2_
  - _Boundary: LldapGroupClient_

- [x] 5. ユーザー・グループ移行スクリプトの実装とローカル検証
- [x] 5.1 migrate_users.py実装(authentik API→LLDAP GraphQL)
  - authentik REST APIから全ユーザー(email・属性含む)と全グループ(メンバーシップ含む)を取得する処理を実装する
  - 取得したユーザー・グループをLLDAP GraphQL APIのcreateUser/createGroup/addUserToGroup相当のmutationで投入する処理を実装する
  - 投入後にauthentik側のユーザー数・グループメンバーシップとLLDAP側の件数を突き合わせて差分を報告する検証処理を実装する
  - _Requirements: 6.1_
  - _Boundary: migrate_users.py_
  - _Depends: 2.1_

- [x] 5.2 k3d環境でのダミーデータ移行リハーサル
  - k3d環境のLLDAPに対しダミーのauthentikユーザー・グループデータ(または実authentikのstaging相当データ)で`migrate_users.py`を実行する
  - 移行後の検証処理が件数一致を報告し、不一致時にはどのユーザー・グループが欠落したか特定できることを確認する
  - _Requirements: 6.1, 6.2_
  - _Boundary: migrate_users.py_
  - _Depends: 5.1_

- [x] 5.3 (P) Authelia実OIDCログインフロー(Authorization Code + PKCE)のk3d実機確認
  - **目的**: task 1.2で疎通確認したのは汎用テストクライアント1件のみ。task 3で投入した本番相当6クライアント定義(特にRoundcubeの`userdb_acl_groups`クレーム)で実際にログインが通るかは未検証のため確認する
  - ローカル`/etc/hosts`に`127.0.0.1 idp.aramakisai.com`を追記する(Authelia session cookieのdomain検証を通すため。システムファイル変更のためユーザーに確認する)
  - k3d上のAuthelia(port-forward `127.0.0.1:9091`)に対し、6クライアントのうち最低1件(理想は全6件)でAuthorization Code + PKCEフルフローを実行しid_token/access_tokenが発行されることを確認する
  - Roundcube向けclient_idについて`GET /api/oidc/userinfo`を呼び、`userdb_acl_groups`クレームがLLDAPの`mail-acl-groups`属性値を反映していることを確認する
  - _Requirements: 2.1, 2.2_
  - _Boundary: Authelia Deployment_
  - _Depends: 3, 1.2_

- [x] 5.4 mailserver実体をk3dへデプロイしLLDAP経由IMAP/SMTP認証を実証
  - **目的**: task 2.2の検証はPython `ldap3`ライブラリによる直接LDAPクエリのみ。実docker-mailserverコンテナ(Dovecot)がLLDAPに対して実際に認証できるかは未検証のため確認する
  - task 2.2で変更済みの`gitops/manifests/prod/mailserver/statefulset.yaml`相当の設定でmailserverコンテナをk3dへ最小構成デプロイする(既存PVC/Secret構成を流用、本番のTLS証明書等は省略可)
  - task 5.2で投入済みのダミーユーザーに対し、IMAPログイン・SMTP認証(Dovecot SASL)が成功することを確認する
  - `LDAP_QUERY_FILTER_GROUP`/`LDAP_QUERY_FILTER_SENDERS`(グループメーリングリスト送信可否判定)についても、ダミーグループデータで期待通りの絞り込みになることを確認する
  - _Requirements: 3.1, 3.2, 3.3, 3.4_
  - _Boundary: mailserver statefulset変更_
  - _Depends: 2.2, 5.2_

- [x] 5.5 LLDAPサービスアカウント作成手順のrunbook化
  - **目的**: 本番投入時に必要な`authelia-service`・`mailserver-service`等サービスアカウントの作成手順が、本セッションのk3d検証では場当たり的(bootstrap Job用JSON設定を都度手書き)であり文書化されていないため整備する
  - `docs/idp-runbook.md`を新規作成し、サービスアカウント用bootstrap JSON設定のテンプレート、投入手順(ConfigMap→PostSync Job)、`lldap_strict_readonly`グループ付与手順を記載する
  - k3d環境上でrunbook記載の手順のみを見て(記憶を使わず)サービスアカウントを1件再現できることを確認する
  - _Boundary: mailAclGroups運用手順_
  - _Depends: 2.1_

- [x] 5.6 カットオーバー・ロールバック手順のk3dリハーサル
  - **目的**: design.mdのTesting Strategyがロールバック手順の実地確認を要求しているが、本セッションでは未実施のため確認する
  - task 7.2(RPアプリのOIDC接続先切替)およびtask 8.1(mailserverのLDAP接続先切替)に相当する「切替→切り戻し」操作をk3d環境上で模擬実行する
  - 切り戻し後、旧設定(authentik相当のダミー設定)でログイン・LDAP認証が問題なく成功することを確認する
  - _Requirements: 7.1, 7.2, 7.3_
  - _Depends: 3, 2.2_

- [x] 5.7 AIによるブラウザ自動操作E2Eログインテスト(テストユーザー使用)
  - **目的**: task 5.3はスクリプトによるAuthorization Code + PKCEの機械的な疎通確認のみ。実際のブラウザUI操作でのログイン画面・リダイレクト挙動・エラー表示は未確認のため確認する
  - 前提: task 5.3で`/etc/hosts`に`127.0.0.1 idp.aramakisai.com`を追記済みであること
  - **実施者**: AI(claude-in-chromeブラウザ自動操作ツールを使用)。実データ・実ユーザーには一切触れず、k3d上のLLDAPテストユーザーアカウント(`testuser`等)のみを使用する
  - 6クライアントのうち最低1件(理想は複数)について、ブラウザ上で以下を自動操作し確認する
    1. Authelia初回ログイン画面(ユーザー名/パスワード入力)が表示され、LLDAPのテストユーザーでログインできる
    2. ログイン後、RP側(または疑似コールバックページ)へ正しくリダイレクトされる
    3. ログイン済み状態で同一ブラウザから別クライアントへアクセスした際、Autheliaのセッション(SSO)が効いて再ログインを求められない、または想定通り求められる(挙動を記録する)
    4. ログアウト操作でセッションが破棄され、再度保護ページへアクセスするとログイン画面へ戻される
  - 気づいた違和感・UI不備・想定外挙動は本taskの実施メモとして記録し、必要ならtasks.mdへ追加taskとして起票する
  - _Requirements: 2.1, 2.2_
  - _Boundary: Authelia Deployment_
  - _Depends: 5.3_

- [x] 5.8 AIによるモンキーテスト(異常系・想定外操作、テストユーザー使用)
  - **実施結果(2026-08-31)**:
    - ブルートフォース連続失敗→regulation機構によりアカウント一時ロックアウト、認証失敗と同一メッセージで応答: 良好
    - ユーザー列挙耐性(存在しないユーザー名 vs 実在ユーザーの誤パスワード): 応答が完全に同一、耐性あり
    - 認可コード再利用: 2回目は`invalid_grant`で拒否
    - PKCE `code_verifier`不一致: `invalid_grant`で拒否
    - `redirect_uri`改ざん: `invalid_request`で正しく拒否、指定先へのリダイレクトは発生しない
    - LLDAP `lldap_strict_readonly`サービスアカウントでの`createUser`試行: `Unauthorized user creation`で拒否
    - ログイン/ログアウト中のブラウザ戻る・進むボタン連打: 異常終了なし、機密情報の復元なし
    - 複数タブでの別ユーザー同時ログイン: ブラウザのCookieストアが共有されるため後着ログインが優先される(セッションCookieベース認証の一般的な制約であり不備ではない)
    - **[低重大度]** issuerとリクエストHostヘッダのポート不一致時、Authelia内部でpanic(recovered、Podはクラッシュしない)が発生し500を返す。本番はissuer/Hostが常に一致するため通常は発現しないが、Authelia本体の実装上の頑健性の課題として記録(本番投入のブロッカーではない)
  - 重大な不備は検出されず、本番投入(task 6以降)の判断を妨げるものはない
  - **目的**: 正常系フロー(task 5.3, 5.7)は確認済みだが、想定外の操作に対する挙動(セキュリティ上の耐性含む)は未確認のため確認する
  - **実施者**: AI(claude-in-chromeブラウザ自動操作ツール、またはスクリプトでのHTTPリクエスト)。k3d上のテストユーザーアカウントのみ使用し、実データ・実ユーザーには一切触れない
  - 以下の操作を自動で試し、それぞれの挙動を記録する。異常終了せずエラーメッセージが適切に表示されること、認証がバイパスされないことを確認する
    - 誤ったパスワードでの複数回連続ログイン試行(ロックアウト/レート制限の有無)
    - ログイン途中でブラウザの戻る/進むボタンを連打する
    - 発行済み認可コード(authorization code)を2回目のtoken交換に再利用する
    - `state`パラメータまたはPKCE `code_verifier`を意図的に不一致にしてtoken交換を試みる
    - 複数タブ・複数ウィンドウで同時に別ユーザーとしてログインを試みる
    - ログアウト後、ブラウザの戻るボタンで保護ページへアクセスを試みる
    - `redirect_uri`をクエリパラメータで改ざんしてAuthorization Requestを送る
    - 存在しないユーザー名でログインを試み、エラーメッセージがユーザー存在有無を推測させない内容になっているか確認する(ユーザー列挙耐性)
    - LLDAPの`lldap_strict_readonly`所属サービスアカウントの認証情報で、意図的に書き込み系GraphQL mutation(`createUser`等)を呼び権限エラーになることを確認する
  - 見つかった不備は重大度を付けて記録し、本番投入(task 6以降)の可否判断に反映する
  - _Requirements: 2.1, 2.2, 2.3_
  - _Boundary: Authelia Deployment, LLDAP Deployment_
  - _Depends: 5.7_

- [ ] 6. 本番Authelia/LLDAPの並行稼働デプロイとユーザーデータ移行
- [ ] 6.1 ArgoCD Application登録と本番並行稼働確認
  - `gitops/apps/prod/authelia.yaml`・`lldap.yaml`を追加しArgoCD syncで本番にAuthelia/LLDAPをデプロイする(既存authentikは稼働継続)
  - 両Podが起動しHelathcheckが通ることを確認する
  - `make kubectl ARGS="top pods -n prod"`でAuthelia/LLDAP合計の実メモリ使用量がauthentik実測(約800Mi)より明確に少ないことを確認する
  - _Requirements: 1.1, 1.2, 1.3_
  - _Boundary: Authelia Deployment, LLDAP Deployment_
  - _Depends: 2.1, 3, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8_

- [ ] 6.2 本番ユーザー・グループデータ移行の実行とパスワードリセット案内
  - 本番authentikに対し`migrate_users.py`を実行し、全ユーザー・グループをLLDAPへ移行する(パスワードハッシュは移植不可のため移行対象に含まない)
  - 移行後の検証処理で件数不一致がゼロであることを確認する
  - 移行完了後、全ユーザーに対し新IdPでの初回パスワードリセット案内(手順・リセット導線)を送付する
  - _Requirements: 6.1, 6.2, 6.3_
  - _Boundary: migrate_users.py_
  - _Depends: 6.1, 5.2_

- [ ] 7. RPアプリのOIDC接続先段階的切替
- [ ] 7.1 6 RP分のOIDCクライアントシークレット発行とExternalSecret登録
  - CMS(prod), Roundcube, Vaultwarden, ArgoCD, Cloudflare Access, room-presenceそれぞれについてAuthelia向けclient_secretを新規発行しInfisicalへ登録する
  - 各アプリのExternalSecretがAuthelia向けclient_id/secretを参照するよう変更する
  - `kubectl get externalsecret`で全キーが復号・反映されていることを確認する
  - _Requirements: 2.2, 2.3_
  - _Boundary: OIDC Client移行_
  - _Depends: 6.1_

- [ ] 7.2 RPアプリを1つずつAuthelia接続先へ切替・ログイン確認
  - 各RPアプリのOIDC issuer/authorization/token endpoint設定をauthentikからAuthelia向けへ1アプリずつ変更しArgoCD syncする
  - 切替直後に当該アプリへ実ログインしAuthorization Code + PKCEフローが成功することを確認する
  - ログインに失敗した場合は当該アプリのみauthentik向け設定へ切り戻す
  - 6アプリすべてでAuthelia経由ログインが成功したことを確認する
  - _Requirements: 2.1, 2.2, 2.3, 7.1, 7.2, 7.3_
  - _Boundary: OIDC Client移行_
  - _Depends: 7.1_

- [ ] 8. Dovecot・vaultwarden-rbac-syncの本番切替
- [ ] 8.1 Dovecot LDAP接続先の本番切替とメール認証確認
  - `gitops/manifests/prod/mailserver/statefulset.yaml`の変更(task 2.2)をArgoCD syncで本番へ適用する
  - IMAP/SMTPログインおよびRoundcube経由メール認証(mail-acl-groupsクレーム込み)が成功することを確認する
  - 失敗した場合はauthentik-ldap-outpost向け設定へ切り戻す
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 7.1, 7.2_
  - _Boundary: mailserver statefulset変更_
  - _Depends: 6.2, 3_

- [ ] 8.2 vaultwarden-rbac-syncのLLDAP接続先切替と同期動作確認
  - `VAULTWARDEN_RBAC_SYNC_AUTHENTIK_API_TOKEN`相当をLLDAP向けAPIトークンに置き換えたExternalSecretを本番へ適用する
  - dry-run実行で差分計算結果が想定通りであることを確認したのち、本番同期を1回実行しVaultwarden Collection権限が正しく反映されることを確認する
  - 失敗した場合は`AuthentikGroupClient`向け設定へ切り戻す
  - _Requirements: 4.1, 4.2, 7.1, 7.2_
  - _Boundary: LldapGroupClient_
  - _Depends: 4.1, 6.2_

- [ ] 9. authentikリソース撤去と関連設定の同期
- [ ] 9.1 authentik関連Terraformリソースの撤去とDiscord連携の終了
  - `terraform/authentik_discord.tf`・`authentik_ldap.tf`・`authentik_apps.tf`等の`authentik_*.tf`一式を削除し、Discordロール自動同期・アバター自動取得・動的グループ判定の機構を完全に撤去する(本移行ではDiscordソーシャルログイン単純連携も対応しない判断とする)
  - `terraform plan`で削除対象リソースのみが差分として出ることを確認したのち`terraform apply`する
  - `gitops/helm-values/prod/falco.yaml`のauthentik関連誤検知除外ルールをAuthelia/LLDAPのプロセス名・イメージ名に置き換える
  - authentik-server/worker/ldap-outpost/authentik-db Podが削除されクラスタから消えていることを確認する
  - _Requirements: 5.1, 5.2, 1.1, 1.3_
  - _Boundary: authentik関連リソース撤去_
  - _Depends: 8.1, 8.2, 7.2_

- [ ] 9.2 mailAclGroups手動運用手順の整備
  - グループ変更時にLLDAPの`mail-acl-groups`属性を運営担当者が手動更新する手順を運用ドキュメントとして整備する
  - `.kiro/steering/vaultwarden-rbac.md`のAuthentikグループ参照箇所をLLDAPグループ参照に更新する
  - 実際にテストグループのメンバー変更→`mail-acl-groups`手動更新→Dovecot ACL反映確認、の一連を1回リハーサルし手順の実行可能性を確認する
  - _Requirements: 5.3_
  - _Boundary: mailAclGroups運用手順_
  - _Depends: 8.1_
