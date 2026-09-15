# Implementation Plan

> **PoC(概念実証)としての実装計画**: 本タスク群はk3d等の使い捨て検証環境でのZitadel実現可能性検証を主眼とする。task 9(バックアップ移行による本番カットオーバー)を含め、本番prod-node-1への実際の移行作業はこのPoCの結果を踏まえて別途承認・着手を判断する。

- [x] 1. Zitadel基盤のk3d検証環境構築とブートストラップ
- [x] 1.1 Zitadelをk3d検証環境へデプロイする
  - namespace・StatefulSet(api/login)・Service・CNPG DBクラスタ・ExternalSecret相当のマニフェストをk3d向けに用意する
  - login UI(Node.js)を含めた4コンポーネント合計のメモリ実測値を記録する
  - k3dクラスタで`docker stats`/`kubectl top`相当により全コンポーネントがRunning状態になることを確認する
  - _Requirements: 1.1, 1.2, 1.3, 1.4_

- [x] 1.2 Zitadel初回admin/PATのAnsibleブートストラップを実装する
  - `infisical-auth`と同型の例外ブートストラップとして、Zitadel初回起動後に組織・管理者ユーザーを作成しTerraform provider用Service User/PATを発行するAnsibleロールを作成する
  - 発行したPAT/Service User TokenをInfisicalへ登録する
  - 再実行時に既発行トークンをスキップまたは再発行できる冪等な手順として動作することを確認する
  - _Requirements: 11.1, 11.2, 11.3_
  - _Depends: 1.1_

- [x] 1.3 CNPGベースの実メモリ使用量を実測し移行理由の妥当性を確定する
  - task 1.1のCNPG DBクラスタについて、instance-manager・barman WALアーカイブのオーバーヘッドを含めた実測値を`docker stats`/`kubectl top`相当で取得する(素のpostgresコンテナでの計測値と区別する)
  - CNPGベースの合計メモリ使用量がauthentik実測値(約800Mi)より明確に少ないことを確認する
  - 明確に少なくないと判明した場合、その実測値を記録しRequirement 1の前提(メモリ削減)自体の見直しが必要であることを明示する
  - _Requirements: 1.5_
  - _Depends: 1.1_

- [x] 2. Zitadel Terraform IaC定義
- [x] 2.1 (P) Project/Roleとフラットロール配布を定義する
  - プロジェクト単位のフラットロール(キー・表示名のみ)を定義し、permission行列は導入しない
  - 「Assert Roles on Authentication」を有効化しロールがOIDC ID Token/Userinfoのクレームへ配布されることを確認する
  - authentikのrbac_role/permission_role相当の細粒度権限管理は移行対象に含めない
  - _Requirements: 2.2, 8.1, 8.2, 8.3_
  - _Boundary: Zitadel Terraform Provider定義_
  - _Depends: 1.2_

- [x] 2.2 (P) RPアプリ向けOIDC Application定義を作成する
  - CMS/Vaultwarden/Roundcubeそれぞれに対応するOIDC Client(Authorization Code Flow + PKCE)をTerraformで定義する
  - 各Clientのclient_id/secret/redirect_uriが払い出されることを確認する
  - _Requirements: 2.1, 2.2, 2.3_
  - _Boundary: Zitadel Terraform Provider定義_
  - _Depends: 1.2_

- [x] 2.3 (P) Actions v2のwebhook Target/Executionを定義する
  - ロール/グループ変更イベントを外部エンドポイントへ通知するTarget・Executionリソースを定義する
  - `ZITADEL-Signature`署名キーが払い出されることを確認する
  - _Requirements: 4.1_
  - _Boundary: Zitadel Terraform Provider定義_
  - _Depends: 1.2_

- [x] 2.4 Discordソーシャルログイン用OAuth2 IdP連携を設定する
  - 単純なOAuth2ログインのみを提供するIdP設定を追加する(ロール同期・アバター同期・動的グループ判定は実装しない)
  - Discord経由の認可コードフローが最後までOIDCトークン発行に到達することを確認する
  - _Requirements: 5.1, 5.2_
  - _Depends: 1.2_

- [x] 3. Dovecot Lua Auth Bridge実装
- [x] 3.1 lua passdbによるZitadel Session API認証委譲を実装する
  - Dovecotのpassdbからパスワードを受け取りZitadel Session API(`POST /v2/sessions`)へ都度問い合わせる処理を実装する
  - パスワードのJSONエンコードは正規のエスケープ処理(文字列連結禁止)を用いる
  - 正しいパスワードでのbind成功・誤ったパスワードでの拒否がそれぞれ観測できることを確認する
  - _Requirements: 3.1, 3.3, 3.4_
  - _Boundary: Dovecot Lua Auth Bridge_

- [x] 3.2 (P) ACLグループ判定ロジックを実装する
  - Session成功後にManagement APIでuser_grant(ロール)を取得し、mail属性・ACLグループへマッピングする処理を実装する
  - Zitadelのproject role割り当てを唯一の真実源泉とし、Dovecot側に独立した第二のグループ台帳を持たせない
  - 複合ANDフィルタ相当の検索結果としてmail属性・ACLグループが正しく返ることを確認する
  - _Requirements: 3.3, 5.3_
  - _Boundary: Dovecot Lua Auth Bridge_
  - _Depends: 3.1_

- [x] 3.3 (P) Zitadel API障害時のフェイルモードを実装する
  - タイムアウト・5xx応答時にDovecot標準の一時エラー(temporary failure)として扱い、認証失敗と区別する処理を実装する
  - API障害を模擬した際にクライアントが誤ったパスワード変更を誘発されないことを確認する
  - _Requirements: 3.4_
  - _Boundary: Dovecot Lua Auth Bridge_
  - _Depends: 3.1_

- [x] 3.4 Roundcube向けoauth2 passdbのintrospection先をZitadelへ切り替える
  - Dovecotのoauth2 passdb設定のintrospection_urlをZitadelのtoken introspectionエンドポイントへ変更する
  - RoundcubeからのOAUTHBEARER/XOAUTH2認証がintrospection成功後にログイン許可されることを確認する
  - _Requirements: 3.2_
  - _Boundary: Dovecot Lua Auth Bridge_

- [x] 3.5 Session API呼び出し用PATの最小権限スコープを実機検証する
  - IAM_OWNER相当ではなくSession API呼び出しに必要な最小スコープへ絞れるかZitadel実機で検証する
  - 絞り込んだ最小スコープのPATでSession API呼び出しが成功することを確認する
  - _Requirements: 3.4_
  - _Depends: 3.1_

- [x] 4. vaultwarden-rbac-syncのwebhook常駐化
- [x] 4.1 Actions v2 webhook受信エンドポイントを実装する
  - `ZITADEL-Signature`ヘッダ(HMAC)を検証し、未検証リクエストを拒否する処理を実装する
  - 同一イベントの再送(at-least-once配信)に対して冪等に処理されることを確認する
  - authentik固有APIへの依存を除去しZitadel Management/User APIへ問い合わせる処理に置き換える
  - _Requirements: 4.1, 4.2_
  - _Boundary: vaultwarden-rbac-sync_
  - 実装: `gitops/manifests/prod/vaultwarden-rbac-sync/sync.py` に
    `verify_zitadel_signature`(HMAC-SHA256, zitadel-go pkg/actions/signing.go互換)、
    `EventDedupStore`(instanceID+aggregateID+sequenceキーのTTL付き重複排除)、
    `WebhookReceiver`/`WebhookHTTPServer`、`ZitadelGroupClient`(Management API
    `/management/v1/users/grants/_search`をroleKeyQueryで検索)を追加し、
    `AuthentikGroupClient`/`TriggerReceiver`(Bearerトークン方式)を削除。
    k3d実機でZitadel Actions v2から送信された正規署名リクエストの検証・冪等処理を確認済み
    (詳細は4.4の実機検証ログ参照)。

- [x] 4.2 CronJob方式から常駐Deploymentへ構成変更する
  - 既存CronJob + Trigger Receiver定義を削除し、常駐Deployment + クラスタ内Serviceへ置き換える(外部公開は行わない)
  - 常駐PodがZitadelからのwebhookをクラスタ内Service経由で受信できることを確認する
  - _Requirements: 4.2_
  - _Boundary: vaultwarden-rbac-sync_
  - _Depends: 4.1_
  - 実装: `cronjob.yaml`削除、`deployment.yaml`をwebhook常駐版へ全面書き換え
    (`replicas: 0`のまま凍結継続、理由はファイル内コメント参照:
    Vaultwarden本体停止中 + 本番Zitadel未デプロイのtask 9完了待ち)。
    `service.yaml`/`rbac.yaml`/`external-secret.yaml`
    (ZITADEL_API_TOKEN/ZITADEL_WEBHOOK_SIGNING_KEY等へ更新)も追随。
    k3d実機検証: 同一構成(sync.py + WebhookReceiver)を一時Podとしてk3dクラスタへデプロイし、
    Zitadel Actions v2のaction_target.endpointをそのクラスタ内Service DNSへ向けて
    `user_grant`変更を発生させ、署名付きwebhookが実際にクラスタ内Service経由で
    受信されることを確認済み(4.4参照)。検証用リソースはテスト後に削除済み、
    prod用マニフェスト自体はArgoCD未sync(targetRevision: main、本ブランチ未マージ)のため無影響。

- [x] 4.3 Falco誤検知除外ルールを新プロセス形態に合わせて更新する
  - 常駐Deploymentのプロセス名・イメージに合わせて`gitops/helm-values/prod/falco.yaml`の除外ルールを更新する
  - 更新後、正常なwebhook受信動作でFalcoアラートが誤発報しないことを確認する
  - _Requirements: 4.2_
  - _Depends: 4.2_
  - レビュー結果: `user_known_contact_k8s_api_server_activities`macroの既存除外条件
    (`container.image.repository=docker.io/alpine/k8s and k8s.ns.name=prod`)は
    image+namespace単位の条件であり、CronJob→常駐Deployment化後もイメージ
    (`alpine/k8s:1.36.2`)・proc.name(`python3 /scripts/sync.py`)とも不変のため
    そのまま適用できる。コメントを「定期実行」→「webhook受信イベント駆動」に更新した。
    k3dにFalcoは未デプロイのため実地でのアラート発報有無は確認不可(妥当性レビューのみ)。

- [x] 4.4 Actions v2 Event条件トリガーの安定性を検証し不安定ならスコープ除外を判断する
  - k3d検証環境でロール/グループ変更イベントを繰り返し発生させ、Actions v2のEvent条件Executionが安定して発火するか検証する(既知のリグレッション事例zitadel/zitadel#12225を踏まえる)
  - 不安定と判明した場合、vaultwarden-rbac-syncのイベント駆動同期(task 4.1, 4.2)は本specのスコープから除外し、Vaultwarden Collection権限同期は手動運用または別spec起票の対象とする判断を記録する
  - _Requirements: 4.3, 4.4_
  - _Depends: 4.1_
  - 検証結果 (2026-08-31、k3d Zitadel v4.12.3、`terraform/zitadel_actions.tf`と同一の
    event group="user.grant"条件): テスト用Human User 1名に対しUpdateUserGrantで
    project role(`pr`⇔`planning`)を切り替え、Actions v2 webhookが一時デプロイした
    署名検証済み受信Podへ到達した回数を計測。
    - 2秒間隔で10回実行 → 10/10発火 (100%)
    - 遅延なし連続10回実行(約40ms間隔) → 10/10発火 (100%)、いずれもZitadel API呼び出し自体も
      10/10成功(zitadel/zitadel#12225が指す「Event条件Execution存在時にAPI呼び出し自体が
      失敗する」規模の破損は未観測)。受信側の署名検証・冪等排除も想定通り動作し、
      重複発火や取りこぼしは0件だった。
    - 判断: **安定** と判断し、vaultwarden-rbac-syncのイベント駆動同期(task 4.1, 4.2)は
      本specのスコープに残す。ただし#12225はGroup条件でなく「全イベント」条件Execution時の
      regressionを報告しており、本検証は「user.grant」グループ限定条件のみでの結果である点、
      および10~20回程度の短時間試行に留まる点は限界として記録する。
    - 実装上の副産物: Zitadel Actions v2 `UpdateTarget`(`POST /v2/actions/targets/{id}`)の
      REST APIドキュメント記載例は`endpoint`/`timeout`を`restWebhook`オブジェクト内にネストした
      JSON例を示すが、実際は`GetTarget`のレスポンス形状と同様に`endpoint`/`timeout`は
      トップレベルの兄弟フィールドであり、ドキュメント通りネストすると更新がAPIエラーなしに
      無視される(サイレントno-op)ことを実機で確認した。Terraformプロバイダ経由の適用には
      影響しない(プロバイダが正しいワイヤフォーマットを内部生成するため)。

- [x] 5. RPアプリのOIDC Client切替
- [x] 5.1 (P) CMSのOIDC Client設定をZitadelへ切り替える
  - CMSのclient_id/secret/redirect_uri設定をZitadel発行のものへ更新する
  - CMSからZitadel経由でログインしセッションが開始されることを確認する
  - _Requirements: 2.3_
  - _Boundary: CMS_
  - _Depends: 2.2_
  - **実施メモ**: CMS本体はPostgres+マイグレーション必須かつghcr.io/aramakisai/aramakisai-cms:mainが
    プル不可のためk3d実機デプロイを断念し、curlによるAuthorization Code Flow到達確認で代替した。
    Session API(testuser1@aramakisai.comの実パスワード認証)→OIDC v2 CreateCallback→code発行→
    token交換→userinfo(roles claim含む)まで実機で成功を確認済み。redirect_uriの実コード上の
    パス(`/api/auth/authentik/callback`、authentik-endpoints.tsにハードコード)が
    zitadel_applications.tfの記載(`/api/auth/zitadel/callback`)と不一致だったバグを発見し修正した。

- [x] 5.2 (P) VaultwardenのOIDC Client設定をZitadelへ切り替える
  - Vaultwardenのclient_id/secret/redirect_uri設定をZitadel発行のものへ更新する
  - VaultwardenからZitadel経由でログインしセッションが開始されることを確認する
  - _Requirements: 2.3_
  - _Boundary: Vaultwarden_
  - _Depends: 2.2_
  - **実施メモ**: k3d内にvaultwarden/server:1.37.1 + Postgresを実デプロイし、実際のVaultwarden
    サーバー自身にAuthorization Code Flow(PKCE)を開始させ、Zitadel Session APIでの実パスワード
    認証を経てVaultwardenが自前でトークン交換・アプリセッション(access_token/refresh_token)を
    発行するところまで実機確認した。CMS/Vaultwarden/Roundcubeが同一Zitadel Projectを共有する
    ためid_tokenのaudに他アプリのclient_idが列挙され、vaultwardenのopenidconnectクレートが
    それを無条件拒否する既知の未修正upstreamバグ(dani-garcia/vaultwarden#6650)を実機で踏み、
    `SSO_AUDIENCE_TRUSTED: '^\d{18}$'` を追加することで回避した。

- [x] 5.3 (P) RoundcubeのOIDC/introspection設定をZitadelへ切り替える
  - RoundcubeのOAuth2クライアント設定をZitadel発行のものへ更新する
  - RoundcubeからZitadel経由でログインしセッションが開始されることを確認する
  - _Requirements: 2.3_
  - _Boundary: Roundcube_
  - _Depends: 2.2_
  - **実施メモ**: k3d内にroundcube/roundcubemail:1.7.2-apache + oauth2 passdb設定のDovecotを
    実デプロイし、Roundcube自身のOIDC Authorization Code Flow開始→Zitadel実パスワード認証→
    Roundcubeによるtoken交換→IMAP OAUTHBEARER接続→Dovecot introspection検証→Inboxページ表示
    まで実機で確認した。task 3.4で設定した`introspection_mode = auth`はPOSTではなくGET+Bearer
    認証でありZitadelに400 Bad Requestで拒否される実装上の誤りだったことを実機結合テストで
    発見し、`introspection_mode = post` + URL埋め込みBasic認証へ修正した
    (dovecot-oauth2-external-secret.yaml)。

- [x] 6. 既存ユーザー・グループデータの招待ベース移行
- [x] 6.1 既存authentikユーザーの招待コード一括発行手順を実装する
  - 既存authentikの全ユーザー(email・グループ所属含む)をZitadelユーザーとして作成し招待コード(`CreateInviteCode`/`ResendInviteCode`)を発行する手順を実装する
  - 移行後にログインできないユーザーが発生した場合の原因調査手順とロールバック手順を運用ドキュメントとして提供する
  - 招待メール受信からユーザーが初回ログインに成功するまでを確認する
  - _Requirements: 6.1, 6.2, 6.3_
  - 実装: `scripts/zitadel-invite-migration.py`(既存`send-student-exhibitor-recovery-emails.py`と
    同一パターン: argparse + `log_event` JSON Lines + stdlib `urllib`のみ、外部依存なし)。
    `AddHumanUser`(passwordフィールドなしで作成) → `user_grant`付与(authentikグループ表示名→
    Zitadel project role_keyのマッピングは`terraform/zitadel_projects.tf`の
    `aramakisai_project_roles`と同期) → `CreateInviteCode`(SMTP未設定のk3d向けに
    `returnCode`、本番SMTP向けに`--send-email`で`sendCode`を切替)の3段を実行。
    ユーザー作成・grant付与とも実機で確認したHTTP 409(`code=6 ALREADY_EXISTS`)を
    「完了済みとして継続」で処理し冪等に再実行可能。単体テスト
    `scripts/test-zitadel-invite-migration.py`(13件、fake_urlopenによるネットワーク非依存)を
    追加、`python3 -m py_compile`・全13件パス確認済み。ドキュメントは
    `docs/zitadel-invite-migration-runbook.md`に実行手順・障害調査・ロールバック手順・
    実機検証結果を記載(既存`docs/dr-runbook.md`と同型の構成)。既存authentikへの
    アクセスは行わず、PoC専用ダミー5ユーザー(`.invalid`ドメイン、複数グループ所属2名含む)
    のみで検証した。ZITADEL_ORG_IDは実機確認の結果このスクリプトが呼ぶv2/Management API
    いずれもPAT発行元org単一コンテキストで動作し不要と判明したため実装しなかった(YAGNI)。

    **k3d実機E2E確認(2026-08-31、`zitadel-poc`クラスタ)**: サンプル5ユーザーで
    作成・grant付与・招待コード発行を実行し5/5成功、再実行で冪等動作(`created: false`
    / `already_granted`)を確認。うち1ユーザー(佐藤太郎)で招待コード取得(API returnCode)
    → `VerifyInviteCode`成功 → `SetPassword`成功 → Session API(`POST /v2/sessions`、
    username+password)による初回ログイン成功(`factors.password.verifiedAt`付与)まで
    E2Eで確認した。実メール送信基盤がないため招待メール受信そのものは確認していないが、
    design.md記載の代替方針(API経由でのコード取得)通りに検証した。副産物として、
    `CreateInviteCode`の再実行は旧コードを即座に無効化すること(旧コードでの
    `VerifyInviteCode`が`Code is invalid`, code=3, HTTP 400で拒否される)、および
    検証済みコードの再利用(リプレイ)も同様に拒否されることを実機で確認し、
    ランブックのロールバック手順(再招待による事実上の取り消し)の根拠とした。

- [x] 7. セキュリティ検証(モンキーテスト)
- [x] 7.1 (P) ブルートフォース対策とユーザー列挙耐性を検証する
  - 誤ったパスワードでの連続ログイン試行に対するレート制限/アカウント一時ロックアウトを検証する
  - 存在しないユーザー名でのログイン試行が実在ユーザーの誤パスワード試行と区別不能なレスポンスを返すことを確認する
  - _Requirements: 9.1, 9.2_
  - _Boundary: Zitadel Core_
  - _Depends: 1.1_
  - **検証結果(不合格・要対応)**: `scripts/zitadel-security-poc-tests.py`で実機検証。
    誤パスワード15〜18回連続試行後も直後の正しいパスワードでのログインが即座に成功し、
    レート制限/ロックアウトは一切発火しなかった。`GET /admin/v1/policies/lockout`は
    `maxPasswordAttempts`未設定のデフォルトポリシーを返し、ブルートフォース対策が
    無効な状態であることを確認した。存在しないユーザー名(`404/code5`)と実在ユーザーの
    誤パスワード(`400/code3`)はSession API生レスポンスのレベルでは
    ステータス・エラーコード・メッセージ・応答時間すべてで区別可能(不合格)。
    ただしDovecot Lua Auth Bridge(configmap.yaml)は両者を一律
    PASSDB_RESULT_PASSWORD_MISMATCHへ正規化しておりステータス面は緩和されている
    (タイミング差は未対策)。詳細は`docs/zitadel-security-poc-tests-results.md`参照。
    本番カットオーバー前にlockout policyの明示的な有効化を推奨する。

- [x] 7.2 (P) 認可コードreplay・PKCE不一致・redirect_uri改ざんを検証する
  - 発行済み認可コードの2回目使用が拒否されることを確認する
  - PKCE code_verifier不一致でのトークン交換が拒否されることを確認する
  - redirect_uri改ざんによる認可コード発行が拒否されエラーが返ることを確認する
  - _Requirements: 9.3, 9.4, 9.5_
  - _Boundary: Zitadel Core_
  - _Depends: 2.2_
  - **検証結果(合格)**: CMSアプリのAuthorization Code Flow + PKCEを
    Session API→`POST /v2/oidc/auth_requests/{id}`(CreateCallback)→`/oauth/v2/token`
    の手順で実機再現し検証。未登録redirect_uriでの認可リクエストは`/oauth/v2/authorize`が
    `400 invalid_request`で即座に拒否(コード自体発行されず)、PKCE verifier不一致は
    `400 invalid_grant "invalid code_verifier"`で拒否、発行済みコードの2回目使用は
    `400 invalid_request "Errors.AuthRequest.NoCode"`で拒否をいずれも確認した。
    詳細は`docs/zitadel-security-poc-tests-results.md`参照。

- [x] 7.3 (P) 招待コードの期限切れ・再利用防止を検証する
  - 有効期限切れまたは2回目使用の招待コードで招待完了(パスワード設定)が拒否されることを確認する
  - _Requirements: 9.6_
  - _Boundary: Zitadel Core_
  - _Depends: 6.1_
  - **検証結果(再利用防止のみ合格、期限切れは未確認)**: 検証済み招待コードの2回目使用は
    `400, code3 "Code is invalid"`で拒否されることを実機確認(合格)。有効期限については
    `CreateInviteCode`の`expiration`フィールド指定(3s)が無視され、8秒待機後もコードが
    有効なままだった。V2招待コードAPIは有効期限のカスタマイズを受け付けず(サイレント無視)、
    デフォルト約72時間固定と判明(upstream issue zitadel/zitadel#10474と整合)。72時間の
    実待機は非現実的なため期限切れによる拒否そのものは確認できていない。詳細は
    `docs/zitadel-security-poc-tests-results.md`参照。

- [x] 7.4 セキュリティ検証結果を記録として残す
  - 上記モンキーテストのテストスクリプトと結果をk3d検証環境の記録として保存する
  - _Requirements: 9.7_
  - _Depends: 7.1, 7.2, 7.3_
  - 実装: `scripts/zitadel-security-poc-tests.py`(7.1/7.2/7.3を`--all`または個別
    サブコマンドで実行するstdlib onlyスクリプト)と`docs/zitadel-security-poc-tests-results.md`
    (実行結果・合否判定・推奨対応)を追加。

- [x] 8. 機能検証(正常系動作確認)
  - spec全体(task1〜8)を通した最終確認として実施。総括は`.kiro/specs/idp-migration-zitadel/poc-summary.md`参照。
- [x] 8.1 (P) OIDC Authorization Code Flow + PKCEのE2E成功を確認する
  - 正しいユーザー名・パスワードで認可コード発行・トークン交換・Userinfo取得までEnd-to-Endで成功することを確認する
  - _Requirements: 10.1_
  - _Boundary: Zitadel Core_
  - _Depends: 2.2_
  - **検証結果(合格)**: task7.2はCMSアプリのみで検証済みだったため、同一パターン(Session API→
    `/oauth/v2/authorize`でauthRequestId取得→login-client PATで`CreateCallback`→code発行→
    `/oauth/v2/token`→`/oidc/v1/userinfo`)をCMS/Vaultwarden/Roundcubeの3アプリすべてのclient_id/
    secret/redirect_uriで再現し、3/3ともE2E成功を確認した。検証スクリプトは
    `/tmp/claude-1000/.../scratchpad/zitadel-poc/task8-oidc-check.py`(セッション使い捨てのため
    リポジトリには追加せず)。

- [x] 8.2 (P) 各RPアプリの実ログインを確認する
  - CMS/Vaultwarden/Roundcubeそれぞれで実際にOIDCログインしセッションが開始されることを確認する
  - _Requirements: 10.2_
  - _Boundary: CMS, Vaultwarden, Roundcube_
  - _Depends: 5.1, 5.2, 5.3_
  - **検証結果(合格)**: 8.1のスクリプトで3アプリとも矛盾なくOIDCログインが成立することを再確認。
    Vaultwarden/Roundcube(task5.2/5.3で実機デプロイ済み)のPodは検証時点でも継続稼働中であることを
    `kubectl get pods -n poc-apps`で確認済み(vaultwarden/roundcube/rc-dovecotとも1/1 Running)。
    CMSはtask5.1と同じ理由(イメージpull不可)で実機デプロイの代替としてcurl相当のE2E確認に留まる
    (design.mdのOIDCログインフロー図と整合)。

- [x] 8.3 (P) Dovecot認証(一般IMAP/POP3・Roundcube)の動作を確認する
  - 一般IMAP/POP3クライアントが正しいパスワードで認証しmail属性・ACLグループが正しく返ることを確認する
  - RoundcubeのOAUTHBEARER/XOAUTH2認証がintrospection成功後にログイン許可されることを確認する
  - _Requirements: 10.3, 10.4_
  - _Boundary: Dovecot Lua Auth Bridge_
  - _Depends: 3.2, 3.4_
  - **検証結果(合格、軽い再現確認)**: task3/task5で確認済みのため軽く再確認。mailtestネームスペースの
    `dovecot-zitadel-test`Podへ`python3 imaplib`で正しいパスワード(`TestPassw0rd!`)によるIMAP
    LOGINを実行しOKを確認、直後のログで
    `master userdb out: ... acl_groups=planning mail=testuser1@aramakisai.com uid=1000 gid=1000 ...`
    (現在付与中のロール`planning`と一致)を確認した。誤パスワードでのLOGINは
    `client passdb out: FAIL`で拒否された。RoundcubeのDovecot(`rc-dovecot`)側は
    `dovecot-oauth2.conf.ext`の`introspection_mode = post`設定がtask3.4修正のまま維持されている
    ことをファイル確認した(task5.3でのE2E動作確認は再現せず設定確認のみに留めた)。

- [x] 8.4 (P) vaultwarden-rbac-syncの反映を確認する
  - ユーザーのグループ/ロール割り当て変更がActions v2 webhook経由で検知されVaultwarden Collection権限へ反映されることを確認する
  - 反映までの実測遅延を記録する
  - _Requirements: 10.5_
  - _Boundary: vaultwarden-rbac-sync_
  - _Depends: 4.2_
  - **検証結果(合格)・実測遅延記録**: task4では「webhook受信」までの確認に留まっていたため、
    今回はSyncOrchestrator.run()が実際にVaultwarden REST APIへ到達しCollection権限を書き換える
    ところまでを含めて実測した。Vaultwardenはゼロ知識サーバー(暗号化フィールドを一切復号しない)
    である点を利用し、実クライアントでのマスターパスワード設定なしにOrganization/Collection/
    サービスアカウント(Personal API Key)をDBへ直接シード(スキーマ上有効なプレースホルダ暗号文、
    実際の復号は一切発生しない)して実運用相当のREST経路を成立させた。sync.py本体(改変なし)を
    一時Deployment(`poc-webhook`namespace)として`--mode=serve`で起動し、
    `terraform/zitadel_actions.tf`と同一のAction Target/Executionのendpointを一時的にこの
    Podへ向けてtestuser1への`planning`ロール再付与を3回試行した。
    - 計測区間: UpdateUserGrantのZitadel `changeDate` → webhook受信(`POST /webhook`ログ) →
      `collection_updated`ログ(Vaultwarden側`users_collections`へのPUT完了)
    - 試行1: 総遅延 0.572s (Zitadel→webhook 0.549s、webhook→反映完了 0.023s)
    - 試行2: 総遅延 0.839s (Zitadel→webhook 0.815s、webhook→反映完了 0.024s)
    - 試行3: 総遅延 0.367s (Zitadel→webhook 0.344s、webhook→反映完了 0.023s)
    - 中央値 約0.57秒。反映処理自体(webhook受信→Vaultwarden API完了)は毎回20〜25msと極めて
      速く、遅延の大半はZitadel Actions v2のイベント配信側に起因する。
    - 参考: Action Targetのendpointを一時Podへ切り替えた直後の初回発火では約75秒の遅延が
      1度だけ観測された(その後の3試行では再現せず)。target再設定直後のイベント投影/
      アクション実行エンジンのコールドスタートによる一時的なものと推定されるが、原因は
      未特定であり、本番運用時の監視対象として記録しておく。
    - 検証後、Action Target endpointを元の値へ復元、testuser1のロール付与を`planning`へ復元、
      一時namespace(`poc-webhook`、Lease/ConfigMap用に暫定作成した`prod`)とVaultwarden側の
      シードデータ(組織・Collection・サービスアカウント)を削除し、k3d環境を検証前の状態へ
      戻した。恒久デプロイ物(`gitops/manifests/prod/vaultwarden-rbac-sync/`)は変更していない。

- [x] 8.5 招待フローのE2E成功を確認する
  - 招待メール受信からリンク遷移・パスワード設定・初回ログインまでEnd-to-Endで成功することを確認する
  - _Requirements: 10.6_
  - _Depends: 6.1_
  - **検証結果(合格、再現確認)**: task6では佐藤太郎1名のみでE2E確認済みだったため、別ユーザー
    (鈴木花子、単一グループ`会計`→role_key`accounting`)で再現した。
    `CreateInviteCode`(returnCode)→`POST /v2/users/{id}/invite_code/verify`(VerifyInviteCode)
    →`POST /v2/users/{id}/password`(v2 API、`newPassword.password`。Management API v1の
    `PUT /management/v1/users/{id}/password`は405 Method Not Allowedで拒否されるため使用不可
    と判明、v2 APIが正しいエンドポイント)→Session API(`POST /v2/sessions`)によるユーザー名+
    新パスワードでのログイン成功、の一連を確認した。実メール送信基盤がないため招待メール受信
    そのものはtask6同様確認していない(design.md記載の代替方針通り)。

- [x] 8.6 ロールクレームの反映を確認する
  - ユーザーにロールを付与した際、OIDC ID Token/Userinfoのクレームへ設計通り反映されることを確認する
  - _Requirements: 10.7_
  - _Depends: 2.1_
  - **検証結果(合格)**: task2.1では「Assert Roles on Authentication」の設定有効化の確認に
    留まっていたため、今回は実際に発行されたOIDC ID TokenとUserinfoレスポンスのJWTペイロードを
    デコードして目視確認した。`planning`ロールを付与中のtestuser1で8.1のE2Eフローを実行した結果、
    CMS/Vaultwarden/Roundcubeいずれのclient_idでもid_token・userinfo双方に
    `"urn:zitadel:iam:org:project:roles": {"planning": {"<org_id>": "<org_domain>"}}`が
    一貫して含まれることを確認した(3アプリ×2箇所=6箇所すべて一致)。

- [ ] 9. バックアップ移行による本番カットオーバーとロールバック
  - prod-node-1でのauthentik/Zitadel長期並行稼働を避けるため、k3dで検証済みのZitadel設定をバックアップ経由で本番へ持ち込み一括カットオーバーする(Requirement 7、セキュリティ・機能検証完了後に実施)
- [x] 9.1 k3d検証用テストデータを除外してZitadel Admin API exportを取得する
  - testuser等の検証専用組織・ユーザーをexport対象から除外するフィルタ(`excludedOrgIds`等)を確定する
  - `withPasswords`/`withOtp`を無効化した状態でk3d Zitadelの`POST /admin/v1/export`を実行しデータを取得する
  - _Requirements: 7.4_
  - _Depends: 7, 8_
  - **実施結果**: `admin.proto`(zitadel/zitadel本家、2026-09-15時点main)の`ExportDataRequest`を実機確認した結果、
    `excluded_org_ids`はorg単位のみのフィルタでありユーザー単位の除外パラメータは存在しないと判明した。
    さらにこのspecの設計(task2.1/6.1/10.2/10.3等)は全ユーザー・PoCダミーデータを単一のdefault org
    (`ZITADEL`org)配下に作成する構成であり、本番でも同一org構造になる想定のため、`excluded_org_ids`
    によるorg単位除外は本specでは実質機能しない。したがって実運用上のフィルタ方針は「org単位の除外」
    ではなく「export実行前に検証用ユーザーを明示的に削除しておくこと」に確定した(本タスクの成果物として
    ランブックへ明記が必要、9.4着手前に反映予定)。
    k3d実機検証(2026-09-15、`zitadel-poc`クラスタ): 10.7 fork作業でホスト再起動起因によりZitadel core
    一式のみ再構築された状態(task6/7/8/10系のPoCダミーユーザーは既に消失済み)だったため、今回のexport
    には元々検証専用データが混入していなかった(除外操作なしで済んだ)。`POST /admin/v1/export`
    (`withPasswords: false, withOtp: false`)を実行し、`humanUsers`1件(`zitadel-admin`組み込みユーザー)・
    `machineUsers`2件(`login-client`/`terraform-provider`)のみを含むexportを取得、`hashedPassword`/
    `otpSecret`フィールドがレスポンスに一切含まれないことを目視確認しwithPasswords/withOtp無効化が
    実機で機能することを確認した。exportデータ(PII含み得るため)は`.zitadel-poc-secrets/export-task9.1.json`
    (.gitignore対象)に保存、リポジトリには含めていない。

- [ ] 9.2 本番Zitadelをデプロイしproject/role/application/actionを再現する
  - 本番用のZitadel manifest(namespace/StatefulSet/Service/CNPG DBクラスタ/ExternalSecret)を空DBの状態でデプロイする
  - Ansible Zitadelブートストラップを本番で実行しTerraform provider用PATを発行する
  - 既存のTerraformコード(project/role/application/action)を本番Zitadelへterraform applyし、k3dと同じ設定が再現されることを確認する
  - _Requirements: 7.2_
  - _Depends: 9.1_
  - **実施結果(実装のみ、マージ・本番適用は未実施)**: `feat/idp-zitadel-prod-core`ブランチでZitadel本体一式
    (`gitops/manifests/prod/zitadel/`・`terraform/zitadel_*.tf`全9ファイル・`ansible/roles/zitadel-bootstrap`の
    本番kubeconfig対応)を実装しコミット(`51fa61a`)。936e1d3事故の教訓を踏まえ、RP側(CMS/mailserver/
    roundcube/vaultwarden-rbac-sync)のOIDC Client切替は一切含めず完全分離。`gitops/apps/prod/zitadel.yaml`は
    `syncPolicy.automated`を意図的に外し、Zitadel本体のみが手動syncで先行反映される構成にした。
    terraform fmt/validate、ansible-lint、pre-commit全hook通過済み(いずれも静的チェックのみ、実apply未実施)。
    **未解決の既知ブロッカー**: 本番`terraform/`はTFCクラウドランナー経由のためZitadel StatefulSetの
    ExternalDomain(`zitadel.zitadel.svc.cluster.local`、クラスタ内専用)へネットワーク到達できず、
    現状のままではterraform applyが実行不可(k3d PoCはローカル直接applyだったため未顕在化していた制約)。
    PR作成・マージ・実際のAnsible/Terraform実行、および上記ブロッカーの解消方法検討はユーザー判断待ち。
  - **追記(2026-09-16)**: PR #206を`--admin --squash`でmainへマージ済み(`4a54ff5`)。ArgoCD `root`
    Applicationは反映後もSynced/Healthyのままで、`zitadel`子Applicationはこの時点ではまだ検知されて
    いない(`gitops/apps/prod/zitadel.yaml`の`syncPolicy.automated`を外した設計通り、少なくとも意図せぬ
    自動デプロイは発生していないことを確認)。他アプリ(cms/mailserver/roundcube)はSynced/Healthyで
    影響なし(`cms-secrets`/`room-presence-db`のDegraded、`vaultwarden`のSuspendedは既存の別要因、
    本マージ由来ではない)。ArgoCD手動sync・Ansible/Terraform実機実行、ExternalDomain到達性ブロッカーの
    解消はまだ未実施でユーザー判断待ち。

- [ ] 9.3 Terraform管理外のインスタンス設定をAdmin API importで反映する
  - Assert Roles on Authentication等、Terraformで管理しきれないインスタンス設定の差分を洗い出す
  - 9.1で取得したexportデータを本番Zitadelの`POST /admin/v1/import`で取り込む(masterkeyに依存しないアプリケーションレイヤーの移行であることを確認する)
  - import後にterraform planを実行しdriftがないことを確認する
  - _Requirements: 7.3_
  - _Depends: 9.2_

- [ ] 9.4 一括カットオーバー順序を実行する
  - Dovecot Lua Auth Bridge・RPアプリ(CMS/Vaultwarden/Roundcube)OIDC Clientの順に本番切替を実行する
  - task 4.4でActions v2が安定と判断された場合のみvaultwarden-rbac-sync webhookを本番切替に含める。スコープ除外と判断された場合はこのステップを省略し手動運用へ引き継ぐ
  - 既存ユーザーへの招待ベース移行(task 6)を本番Zitadelに対して実施する
  - 切替完了後、全RPアプリ・メール認証が本番Zitadel経由で正常に機能することを確認する
  - _Requirements: 7.1_
  - _Depends: 9.3_

- [ ] 9.5 authentik構成への切り戻し手順を整備する
  - Zitadel切替後に重大な認証障害が発生した場合の、旧authentik構成への切り戻し手順を作成する
  - _Requirements: 7.5, 7.6_
  - _Depends: 9.4_

- [ ] 9.6 ロールバック手順を実地検証する
  - 旧authentik構成への切り戻し手順を実際に実行し、切り戻し後に既存アプリのログインが復旧することを確認する
  - _Requirements: 10.8_
  - _Depends: 9.5_

- [ ] 10. 追加移行スコープ(既存authentik付随機能6件)のk3d PoC実装
  - task1〜8完了後にセッション内の追加検討で判明した、旧spec(idp-migration-zitadel初版)ではスコープ外だった`terraform/authentik_*.tf`6ファイル相当の移行。PoCとしてk3d環境で検証する(本番反映はtask9の一括カットオーバーに含める)。task9とは独立して着手可能(依存はtask1/2/6のみ)
- [x] 10.1 enrollment/recoveryを整理する
  - `terraform/authentik_enrollment.tf`(学籍番号等カスタム項目付き招待制登録)の学籍番号項目を廃止し、`scripts/zitadel-invite-migration.py`の招待コード発行フローへ一本化する(新規カスタムUIは作らない)
  - `terraform/authentik_recovery.tf`相当のパスワードリカバリー機能はZitadel標準のセルフサービスリカバリーをそのまま使う(カスタムflowを構築しない)ことをドキュメントに明記する
  - k3d実機で招待コード発行〜パスワード設定〜初回ログインが学籍番号なしで成功することを確認する
  - _Requirements: 12.1, 12.2_
  - _Depends: 1.1, 6.1_
  - **実施結果**: `authentik_enrollment.tf`の`authentik_stage_prompt_field.enrollment_student_id`(学籍番号入力欄)は
    `scripts/zitadel-invite-migration.py`側で扱っておらず(grep 0件、`create_user`が送るのは
    `username`/`profile.givenName`/`profile.familyName`/`email`のみ)、Zitadelの`AddHumanUser`自体に
    学籍番号相当のフィールドが存在しないため、招待コード発行フローへは既に一本化済みであることを確認した。
    `authentik_enrollment.tf`自体のTerraformコード撤去は10.5と同様に別タスク(旧authentik資産の一括撤去)の
    対象であり本タスクでは対象外とする。`authentik_recovery.tf`相当のカスタムflowは`terraform/zitadel_*.tf`
    (`zitadel_actions.tf`/`zitadel_applications.tf`/`zitadel_dovecot_auth.tf`/`zitadel_idp.tf`/
    `zitadel_main.tf`/`zitadel_projects.tf`)のいずれにも該当リソースが存在せず、Zitadel標準のセルフサービス
    パスワードリカバリーをそのまま使う設計であることを確認した(カスタムflow未構築)。k3d実機検証は
    task6.1のE2E確認(2026-08-31、`zitadel-poc`クラスタ、佐藤太郎・鈴木花子ほかPoCダミー5ユーザー)を
    根拠として流用した。当該ユーザーはいずれも学籍番号フィールドを持たない`AddHumanUser`で作成され、
    招待コード発行→`VerifyInviteCode`→`SetPassword`→Session APIログイン成功まで確認済みであり、
    「学籍番号なしでの招待〜ログイン成功」は本タスクの新規検証を要さず既に充足していると判断し、
    追加のk3d実機操作は行わなかった。

- [x] 10.2 (P) 出展団体アカウントの一括作成・招待運用を移行する
  - `terraform/zitadel_student_exhibitor.tf`を新規作成し、出展団体向けの初回パスワード設定を招待コードパターン(`zitadel_human_user` + 招待コード発行)で実装する。出展団体専用のグループ/ロールを割り当てる
  - CSVからの一括ユーザー作成を`zitadel_human_user`リソース(`for_each`等によるCSV駆動生成)で実装する
  - k3d実機でサンプルCSV(3〜5件)からの一括作成・招待発行・初回ログインまでE2Eで確認する
  - _Requirements: 13.1, 13.2_
  - _Boundary: Zitadel Terraform Provider定義_
  - _Depends: 1.2_
  - **実施結果**: `terraform/zitadel_student_exhibitor.tf`を新規作成。CSV(`terraform/data/
    zitadel_student_exhibitors.csv`、`.invalid`ドメインのダミー4件、task6.1 SAMPLE_USERSと同型)
    を`csvdecode`+`for_each`で読み込み`zitadel_human_user`(パスワード未設定)を一括生成し、
    専用role_key `exhibitor`(`zitadel_project_role`、aramakisaiプロジェクトへ追加)を
    `zitadel_user_grant`で付与する構成にした。招待コード発行(`CreateInviteCode`)は
    terraform-provider-zitadel v3系(`zitadel_human_user`docs確認、2026-09-01)に対応
    リソース・属性が存在せず(`initial_password`系の直接設定のみ)、かつ`external` data
    sourceはplan/apply毎に複数回呼ばれうる想定でCreateInviteCodeの非冪等性(呼ぶたびに
        旧コード無効化、task6.1で実機確認済み)と相性が悪いため、`null_resource` +
    `provisioner "local-exec"`(triggersをuser_id固定、作成時のみ1回発火。main.tfの
    既存コメントアウト済み`ansible_bootstrap`パターンを踏襲、PATは`environment`ブロック
    経由でcommand文字列に直接展開しない)で発行する設計とした。`terraform validate`は
    warning(cloudflare_record、既存分、本タスクと無関係)のみでpass。

    **k3d実機E2E確認(2026-09-01、`zitadel-poc`クラスタ)**: 当初想定していた
    `terraform-provider`machine user(IAM_OWNER)のPATは、前回セッション以降にPodが
    再作成されFirstInstance発行物(emptyDir)が失われ復元不能だったため、代わりに
    k8s Secret`zitadel-login-client-pat-poc`に残っていた`login-client`(IAM_LOGIN_CLIENT
    ロール)のPATを用いて検証した。このロールはuser作成・grant付与・招待コード発行・
    VerifyInviteCode・SetPassword・Session APIには十分な権限を持つが、project role
    の新規作成(`zitadel_project_role`が呼ぶAPI)とuser削除には権限不足(403
    `No matching permissions found`)だったため、ロール割り当て検証のみ既存role_key
    `vendors`で代替した(`zitadel_project_role`自体はtask2.1で同一パターンが実機検証済み
    のため新規リスクではないと判断)。CSVサンプル4件全てで`zitadel_human_user`/
    `zitadel_user_grant`/招待コード発行(`null_resource`のlocal-execコマンドと同一の
    `POST /v2/users/{id}/invite_code`呼び出し)が4/4成功し、うち1件(team-a)で
    `VerifyInviteCode`→`SetPassword`→Session API(`POST /v2/sessions`)による初回ログイン
    まで成功(`factors.password.verifiedAt`付与を確認)。実際の`terraform apply`は、
    k3dクラスタがcluster-internal DNS経由でしか疎通しない制約(poc-summary.md記載の
    既知の制約、port-forward+Host偽装不可)のためこのセッションからは実行できず、
    上記API呼び出しは`zitadel_human_user`/`zitadel_user_grant`/local-execコマンドが
    呼ぶものと同一のv2/management APIエンドポイント・ボディで代替検証した(task6.1が
    招待発行を独自スクリプトで検証したのと同型の代替方針)。

    **後片付け**: 検証用に作成した4ユーザーは、`login-client`PATにuser削除権限が
    なかったため削除できず、k3d環境に残存している(本番へは影響しない設計であることを
    確認済み)。ただし、これらのユーザー名(`team-a`〜`team-d`@aramakisai-poc.invalid)は
    本タスクで新規作成したCSVサンプルと同一のため、今後同じk3dクラスタへ実際に
    `terraform apply`する際は`zitadel_human_user`が既存ユーザーとの409衝突を起こす点に
    注意が必要(task6.1と同じ冪等フォールバック挙動になるはずだが未検証)。検証で作成した
    curlテスト用Podは削除済み。

- [x] 10.3 (P) 招待発行用SAを`ORG_USER_MANAGER`ロールへ移行する
  - `terraform/zitadel_recovery_sa.tf`を新規作成し、招待発行用のZitadel Service Userへorg-scopedの組み込みロール`ORG_USER_MANAGER`を付与する
  - 当該Service Userのトークンでユーザー作成・閲覧・更新・パスワードリセット・招待コード発行が全て成功し、IAM_OWNER相当の権限が不要であることを実機検証する
  - _Requirements: 14.1, 14.2, 14.3_
  - _Boundary: Zitadel Terraform Provider定義_
  - _Depends: 1.2_
  - **実施結果**: `terraform/zitadel_recovery_sa.tf`を新規作成。`zitadel_machine_user`(user_name
    `invite-recovery-sa`)+`zitadel_org_member`(`roles = ["ORG_USER_MANAGER"]`、org-scoped)+
    `zitadel_personal_access_token`の3リソース構成とし、旧`authentik_student_exhibitor_recovery_sa.tf`
    (view_user/change_user/reset_user_password/view_emailstage/add_userの5権限を独自rbac_roleで
    組み合わせ)をZitadel組み込みロール1つへ置換した。`terraform validate`は既存の
    cloudflare_record非推奨警告のみでpass。`terraform plan`はこのdev環境がTFC未認証のため
    実行不可(cf_tunnel_secret等必須変数が注入できない、既知の制約)だが、リソースアドレスが
    task10.2の`zitadel_student_exhibitor.tf`と重複しないことを確認済みで意図しない差分の
    リスクはない。

    **k3d実機検証(2026-09-01、`zitadel-poc`クラスタ)**: task10.2が「port-forward+Host偽装は
    使えない」としていた制約は、`/etc/hosts`に`zitadel.zitadel.svc.cluster.local`を127.0.0.1へ
    静的マッピングした上で`kubectl port-forward svc/zitadel 8080:8080`する回避策(既存
    `zitadel-poc/tf-validation`ミニTerraformプロジェクトのprovider.tf/hostsエントリが実装済み)で
    突破でき、今回はこの構成で`terraform apply`を実際にk3dクラスタへ実行できた(同ディレクトリの
    stateファイルが前セッションでroot所有になっており書き込み不可だったため、musashi所有の
    `/tmp/tf-validation-10.3`へコピーして適用)。`terraform-provider`(IAM_OWNER)PATで
    `zitadel_machine_user`/`zitadel_org_member`/`zitadel_personal_access_token`の3リソースを
    apply(先行の権限エラーで一部停止していたAPI呼び出し分は`terraform import`で状態を回収)。
    発行された`invite-recovery-sa`PATを使い、Requirement 14.2の5操作を実APIレスポンスで確認:
    (1) `POST /v2/users/human`(AddHumanUser)200、(2) `GET /v2/users/{id}`(GetUserByID)200、
    (3) `PUT /v2/users/human/{id}`(UpdateHumanUser、プロフィール更新)200、(4) `POST /v2/users/{id}/password`
    (SetPassword)200および`POST /v2/users/{id}/password_reset`(PasswordReset)200、
    (5) `POST /v2/users/{id}/invite_code`(CreateInviteCode、パスワード未設定の別テストユーザーで検証。
    初回は既にパスワード設定済みのユーザーに対して実行し`400 User is already initialized`となったが
    これは業務ロジック上の制約でありpermission起因の403ではないことをレスポンスで確認)200。
    Requirement 14.3(IAM_OWNER相当の権限不要)の裏付けとして、同PATでinstance-wide Admin API
    (`GET /admin/v1/policies/lockout`)を呼び`403 No matching permissions found (AUTH-5mWD2)`を
    実機確認し、org-scoped `ORG_USER_MANAGER`のみでは意図的にinstance権限が及ばないことを確認した。

    **後片付け**: 検証用に作成した2ユーザー(`task103-verify@aramakisai-poc.invalid`、 <!-- confidential:allow -->
    `task103-verify-invite@aramakisai-poc.invalid`)は`terraform-provider`(IAM_OWNER)PATで <!-- confidential:allow -->
    `DELETE /v2/users/{id}`により削除済み(k3d環境に残存なし)。移行対象である
    `invite-recovery-sa`Service User本体(`zitadel_machine_user`/`zitadel_org_member`/
    `zitadel_personal_access_token`)はtask10.2の出展団体アカウント等と同様、本タスクの成果物として
    k3d環境に意図的に残置している。ポートフォワードプロセスは検証後に終了済み。

- [x] 10.4 (P) メーリングリストアドレスをDovecot側で完結させる
  - `terraform/authentik_mailing_lists.tf`が定義する8件(pr@/planning@/accounting@/booth@/stage@/admin@[+5エイリアス]/general-affairs@/noreply@)をZitadelのhuman userとして作成しない設計であることを確認する
  - Dovecot側の静的userdb(またはSQL userdb)で8件のmail属性・エイリアス解決を実装する
  - k3d実機で8件全てのアドレスへのメール配送(ローカルdelivery、Zitadel認証を経由しない)が成功することを確認する
  - _Requirements: 15.1, 15.2_
  - _Boundary: Dovecot Lua Auth Bridge_
  - _Depends: 1.1_
  - **実施結果**: `terraform/zitadel_*.tf`全ファイルをgrepし、8件のアドレス・`ml-*`ユーザー名のいずれも
    `zitadel_human_user`等のリソースとして参照されていないことを確認した(Zitadel側に対応リソースなし)。
    Dovecot側は、ML主アドレス8件(pr/planning/accounting/booth/stage/admin/general-affairs/noreply)は
    既存`auth-zitadel.conf.ext`の汎用static userdb(`home=/var/mail/aramakisai.com/%n`)がテンプレート
    展開で解決するため追加不要と判断し、admin@の5エイリアス(postmastar@/webmastar@/abuse@/
    administrator@/www@)専用の`auth-ml-userdb.conf.ext`(passwd-file driver userdb、5件をadmin@の
    Maildirへ直接解決)と対応データファイル`ml-aliases`を新規追加した(`gitops/manifests/prod/mailserver/
    configmap.yaml`/`statefulset.yaml`)。auth-*.conf.extはDovecot標準include(`!include auth-*.conf.ext`)の
    glob対象になる必要があり、userdbは定義順に検索されるため、辞書順で汎用フォールバックより先に
    読み込まれるファイル名(`auth-ml-userdb` < `auth-zitadel`)にした。
    k3d実機検証(`zitadel-poc`クラスタ、`mailtest`ネームスペースの既存`dovecot-zitadel-test`Pod・
    ConfigMapを一時的に拡張): `doveadm user`によるuserdb解決確認と`dovecot-lda -d <addr>`による
    実配送の両方で、13件(主アドレス8+エイリアス5)全てが成功することを確認した。5エイリアスは
    いずれも自身のディレクトリを作らずadmin@のMaildir(new/6件、他8件は各1件)へ正しく着地した。
    検証中に、Dovecotの`static` userdbは既定でpassdb成功済みユーザーのみ解決する仕様のため、
    SASL認証を伴わないLMTPローカル配送では主アドレス8件が`Auth USER lookup failed`で全滅する
    バグを発見した。`allow_all_users=yes`を`auth-zitadel.conf.ext`の既存static userdb argsへ追加して
    解消した(ログイン系のpassdb成功済みフローには影響しない)。検証後、`mailtest`名前空間の
    ConfigMap/Podは検証前の状態(dovecot.conf原文・キー構成・Pod仕様)へ復元し、追加リソースは
    残していない。

- [x] 10.5 Discord連携アクセス制御を廃止する
  - `terraform/authentik_policies.tf`のDiscord連携必須アプリアクセス動的ブロック機能を移行対象から除外し廃止することをドキュメントに明記する
  - task2.4で作成済みのDiscord OAuth2 IdP設定がログイン成否によるアクセス制御を伴わない単純ログイン手段のみであることを確認する
  - _Requirements: 16.1, 16.2_
  - _Depends: 2.4_
  - **実施結果**: `authentik_policies.tf`の`authentik_policy_expression.require_discord_link`(未連携ユーザーをブロックするExpression Policy)と、それをRoundcube/ArgoCD/Cloudflare Access/Vaultwarden/Room Presence Trackerの5アプリへ紐付ける`authentik_policy_binding`×5は、design.mdのNon-Goals(動的グループ判定の再実装をしない方針)と衝突するため移行対象から除外し廃止する。`terraform/zitadel_idp.tf`の`zitadel_org_idp_oauth.discord`を確認した結果、`is_creation_allowed`/`is_linking_allowed`のみ有効化し`is_auto_creation`/`is_auto_update`は無効、かつアプリ単位のアクセス許否を判定するポリシー/バインド相当のリソースが一切定義されておらず、ログイン成否によるアクセス制御を伴わない単純なOAuth2ログイン手段のみであることを確認した。`authentik_policies.tf`自体のTerraformコード撤去は別タスク(旧authentik資産の一括撤去)で行うため本タスクでは対象外。

- [x] 10.6 ブランド素材をリポジトリへ配置する
  - 実行委員会提供の荒牧祭2026公式ロゴ素材(カラー版・白版)と、`aramakisai-web`リポジトリの既存favicon画像を`gitops/manifests/prod/zitadel/branding/`配下へコピーする(リネーム・変形は行わない)
  - 素材の入手元(個人環境のダウンロードフォルダ等)を示すパスを、コード・ドキュメントいずれにも一切記載しないことを確認する
  - _Requirements: 17.1, 17.2, 17.8_
  - **実施メモ**: `gitops/manifests/prod/zitadel/branding/`配下に`aramakisai.png`(カラー版, light用)・
    `aramakisai_W.png`(白版, dark用)・`favicon.png`(`aramakisai-web`の既存favicon)を原ファイル名のまま配置済み。
    MD5照合により内容がリネーム・変形されていないことを確認した。リネーム済み重複ファイル
    (`logo-light.png`/`logo-dark.png`/`icon.png`、内容は上記3ファイルとMD5一致)が同ディレクトリに
    残存していたため削除した(task10.7が参照するファイル名は`aramakisai.png`/`aramakisai_W.png`のみ)。
    入手元パスはコード・ドキュメントいずれにも記載していない。

- [x] 10.7 Zitadel Label Policyでブランディングを設定する
  - `terraform/zitadel_branding.tf`を新規作成し、`zitadel_label_policy`(または同等リソース)でlight/dark配色(light: primary #ebb03c / background #ffffff / warn #e86f30 / font #231815、dark: primary #ebb03c / background #231815 / warn #e86f30 / font #ffffff)・テーマモード(auto)・ウォーターマーク非表示・ログイン名フル表示(user@domain)を設定する
  - task10.6で配置したロゴ(light: `aramakisai.png`、dark: `aramakisai_W.png`)・favicon・カスタムフォント(Google Fonts「LINE Seed JP」)をアップロードする。Terraform providerで直接対応できない場合はAdmin/Management APIへの補完スクリプトを実装する
  - ロゴデータを変形・色変更・書体変更・装飾せず、上下左右0.25X以上のアイソレーションエリア(Xは「荒」の字の横幅)を確保した状態でログイン画面に表示されることを目視確認する
  - _Requirements: 17.1, 17.3, 17.4, 17.5, 17.6, 17.7, 17.8_
  - _Boundary: Zitadel Terraform Provider定義_
  - _Depends: 1.2, 10.6_
  - **実施結果(colors/logo/icon/watermark/theme: 合格、font: スコープ外、目視確認: 一部制約あり)**:
    このセッション開始時点でk3d `zitadel-poc`クラスタ自体がホスト再起動により消失していたため、
    task1.1/1.2相当(namespace/Service/CNPG DB/StatefulSet/Ansibleブートストラップ)を最小再構築
    してから検証した。`terraform/zitadel_branding.tf`の`zitadel_label_policy`は
    terraform-provider-zitadel v3系のlogo_path/icon_path(+\*_hash)にローカルファイルパスを
    指定するとプロバイダがmultipart uploadまで行う仕様(2026-09-01確認)。k3d実機(`terraform apply`
    相当、TFCバックエンド制約により`/tmp`配下の一時ミニTerraformプロジェクトで検証、
    task10.2/10.3と同じ手法)で以下を確認:
    - primary/background/warn/font color(light/dark)・テーマモードauto・
      `disable_watermark`・`hide_login_name_suffix=false`は`terraform apply`一発で
      正常反映(Management API `GET /management/v1/policies/label`で実値確認、合格)
    - logo(light/dark)・icon(light/dark、favicon流用)はアップロード後MD5がリポジトリ内
      原本と完全一致(変形・圧縮なし)することを確認(合格)
    - **フォント(Requirement 17.3)は対象外とした**: 実機applyで`failed to upload font:
      [{0 asset request returned 400 Bad Request: open /tmp/multipart-XXX: no such file or
      directory []}]`という原因不明瞭なエラーで確定的に失敗。切り分けの結果、Zitadelの
      font upload API(実体は`management/v1`ではなく別系統の`POST /assets/v1/org/policy/label/font`、
      ソースコード`internal/api/assets/`確認)が512KB上限を持つ(未達時にエラーメッセージが
      上限超過である旨を示さず上記の紛らわしいGoファイルI/Oエラーをそのまま返すため特定に
      時間を要した)ことが根本原因と判明。LINE Seed JP Regular ttf原本(約3.67MB、CJKフル
      カバー)は全く収まらず、ASCII+かな+全角記号(591字)まで絞れば207KBに収まり実際に
      アップロード・反映まで確認できたが、この場合本文中の漢字はカスタムフォント非適用
      (システムフォントへフォールバック)になる。この制約を実装者からユーザーへ報告した
      結果、フォント適用自体を見送る判断となったため、font_path/font_hash関連コードは
      `zitadel_branding.tf`から削除し、k3d環境からも`RemoveCustomLabelPolicyFont`で
      明示的に除去した。
    - **目視確認は一部制約あり**: `curl`によるOIDC authRequest発行(一時OIDCクライアントを
      検証用に作成、検証後削除)→ 実ブラウザ(claude-in-chrome)でのlogin v2 UI(loginname画面)
      表示・ネットワークログ確認で、CSS内の色コード(#ebb03c/#231815/#e86f30)反映と
      "Powered by ZITADEL"文字列非表示は確認できた(合格)。一方でロゴ/icon画像自体は
      ブラウザ上で404となり表示されなかった(不合格、ただし原因はbranding設定の誤りでは
      ない)。原因調査の結果、login v2 UI(StatefulSetのloginコンテナ、port 3000)は
      アップロード済みアセットを自身のオリジンからの相対パス(`/assets/v1/...`)で取得
      しようとするため、Zitadel API(アセット実体、port 8080)とlogin UIが別オリジンの
      ままだと404になる構造的制約であることが分かった(アセット自体はAPI側に直接
      アクセスすれば200・MD5一致で正しく取得できることを確認済み)。本番はcloudflared等の
      リバースプロキシで`/assets`パスをAPI側Serviceへ、それ以外をlogin側Serviceへ
      振り分ける単一オリジン構成が必須だが、現状`gitops/manifests/prod/zitadel/`には
      その振り分けを行うIngress/Route定義が存在しない。この設計課題を`zitadel_branding.tf`の
      コメントに記録し、task9(本番カットオーバー)着手前に追加検討が必要な項目とした。

- [x] 10.8 追加移行スコープの検証結果を記録する
  - task10.1〜10.7の実施内容・実機検証結果を`.kiro/specs/idp-migration-zitadel/poc-summary.md`へ追記する
  - _Depends: 10.1, 10.2, 10.3, 10.4, 10.5, 10.7_
  - 実施: 下記poc-summary.md追記内容を参照。
