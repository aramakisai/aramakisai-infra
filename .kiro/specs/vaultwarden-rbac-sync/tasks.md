# Implementation Plan

- [x] 1. 基盤: シークレット・マッピング設定・実行基盤のセットアップ

- [x] 1.1 (P) ExternalSecret定義とInfisicalキー登録によるシークレット注入基盤構築
  - Authentik APIトークン、Vaultwardenサービスアカウントclient_id/client_secret、Trigger共有ベアラートークンを新規Infisicalキーとして登録しExternalSecretに定義
  - 既存`DISCORD_OPS_WEBHOOK_URL`を新規Webhook作成せず再利用する設定にする
  - `kubectl get secret`で全キーが正しく復号・Secretとして存在することを確認できる
  - _Requirements: 12.1, 12.2_
  - _Boundary: RbacSyncSecrets_

- [x] 1.2 (P) マッピング設定ConfigMapのスキーマ実装と検証ロジック構築
  - `mapping.json`構造（`authentik_group`/`organization`/`collection_id`/`permission`必須＋`collection_label`任意の配列、1グループが複数エントリを持てる）を定義（**設計変更**: Collection名はOrg鍵でクライアント暗号化されたCipherStringのためAPI照合不可と実機検証で判明し、`collection`(名前)から`collection_id`(UUID直接指定)に変更。research.md「Vaultwarden Collection名はOrg鍵でクライアント暗号化される」参照）
  - 構文不正・必須フィールド欠落・未知の`permission`値を検出するロード時検証ロジックを実装
  - サンプルマッピング（広報→SNSアカウント→`collection_id`（プレースホルダー）→`can_view`等、`vaultwarden-rbac.md`の実例に基づく）をGitOps ConfigMapとして投入。実Collection IDの反映はtask 1.5（人手作業）
  - 不正なマッピングを与えた場合に検証エラーが返り、正常なマッピングは全件ロードされることを確認できる
  - _Requirements: 4.1, 4.2, 4.3, 4.4_
  - _Boundary: MappingConfigLoader, RbacMappingConfigMap_

- [x] 1.3 (P) Lease操作用ServiceAccount/RBAC定義
  - `vaultwarden-rbac-sync`専用ServiceAccountと、`leases.coordination.k8s.io`に対するget/create/update権限のみを持つRole/RoleBindingを`prod` namespaceに定義
  - 当該ServiceAccountがLease以外のリソースを操作できない（最小権限）ことを確認できる
  - _Requirements: 10.2, 13.3_
  - _Boundary: SyncLockManager RBAC_
  - **修正済み（2026-06-25、task 8実装時に発覚）**: `release()`が`kubectl delete lease`を呼ぶにもかかわらず`delete`権限が付与されておらず本番で403になる欠落があったため、`verbs`に`delete`を追加

- [x] 1.4 (P) 実行エントリポイント骨格（モード分岐・構造化ログ基盤）実装
  - `--mode=cron` / `--mode=serve` の起動引数分岐を実装
  - 招待/更新/削除件数を後続タスクで埋め込める構造化ログ出力フォーマットを定義
  - `--mode=cron`で起動すると指定したモード名がログに記録され正常終了することを確認できる
  - _Requirements: 10.1_

- [ ] 1.5 (人手・Claude実行不可) Authentik/Vaultwarden側の実環境セットアップとmapping.json反映
  - **Authentikグループ権限不足の修正**（2026-06-25、管理用APIトークンで実機確認済み: `VAULTWARDEN_RBAC_SYNC_AUTHENTIK_API_TOKEN`は`GET /api/v3/core/groups/`（フィルタ有無問わず）で`403 You do not have permission to perform this action`を返す。`AuthentikGroupClient`（task 2、実装済み）はこのままでは本番で動作しない）
    - Authentik Admin UI → Directory → Roles で`authentik_core.view_group`権限を持つRoleを作成（未作成なら）
    - 当該トークンに紐づくサービスアカウントユーザーへ上記Roleを割り当てる
    - 割り当て後、同トークンで`GET /api/v3/core/groups/?name=広報&include_users=true`等が200を返すことを確認する
  - **未作成のAuthentikグループの作成**（このSpecのスコープ外、既存の手動運用に従う。design.md Non-Goal参照）
    - `リーダー`グループを新規作成（管理者と同様に全Collectionへcan_manageでアクセスする想定、mapping.json参照）
    - `Googleアカウント`グループを新規作成（Googleアカウント認証情報専用、所属者はcan_view_except_passwordsでアクセスする想定）
  - **Vaultwarden Organization/Collectionの作成**（このSpecのスコープ外、design.md Non-Goal「Organization・Collection自体の新規作成」参照。現状DBに既存Organization/Collectionは0件、2026-06-25実機確認済み）
    - Organization「荒牧祭実行委員会」をWeb Vaultで作成
    - 配下にCollectionを8件作成: `企画`/`会計`/`出店`/`出演`/`広報`/`総務`/`Googleアカウント`/`管理者`（Authentikグループ名と一致させる、`vaultwarden-rbac.md`命名規則参照。`管理者`Collectionは管理者・リーダー専用で部門グループはアクセス権を持たない）
    - サービスアカウント（`vaultwarden-rbac-sync`専用、未ブートストラップなら`vaultwarden-rbac.md`の初回ブートストラップ手順を先に実施）を当該OrganizationのAdminとして参加・Confirmする
  - **Collection IDの確認とmapping.json反映**
    - Organization Owner/Adminが実ブラウザでWeb Vaultにログインし、各Collectionを開いてCollection ID (UUID) を確認する（Collection名はOrg鍵でクライアント暗号化されたCipherStringのため、API/CLI/Claudeでは解決不可能。research.md「Vaultwarden Collection名はOrg鍵でクライアント暗号化される」参照）
    - 確認したUUIDを`mapping-configmap.yaml`の対応する23エントリ全件の`collection_id`へ反映する（現状は全件プレースホルダー値`00000000-0000-0000-0000-000000000000`、`collection_label`を目印に対応付ける）
  - `mapping.json`の全エントリの`collection_id`が対象Organizationに実在するCollectionを指し、かつAuthentikグループ一覧取得が成功することを確認できる
  - _Requirements: 1.1, 1.2, 4.1, 4.4_
  - _Boundary: RbacMappingConfigMap_
  - _Note: 実ブラウザでのマスターパスワード復号操作・Authentik Admin UI操作が必須なため、Claudeはこのタスクを代行できない_

- [x] 2. (P) AuthentikGroupClient実装によるグループメンバーシップ取得
  - 専用Authentik APIトークン（`PRESENCE_AUTHENTIK_API_TOKEN`パターン踏襲）での認証処理を実装
  - グループ名からメンバーのメールアドレス一覧を取得する処理を実装
  - 認証エラー・タイムアウト発生時は例外を上位に伝播させ、グループ不在時はそのグループのみエラー記録して処理を継続する分岐を実装
  - 存在するグループと存在しないグループを混在させたテストで、両方の分岐が期待通り動作することを確認できる
  - _Requirements: 1.1, 1.2, 1.3, 1.4_
  - _Boundary: AuthentikGroupClient_

- [x] 3. VaultwardenOrgClient実装によるAPI連携

- [x] 3.1 (P) サービスアカウント認証によるアクセストークン取得実装
  - `client_id=user.<uuid>`のPersonal API Keyを用いたclient_credentials grantでの`/identity/connect/token`呼び出しを実装
  - 認証失敗時に例外を投げる分岐を実装
  - 有効なサービスアカウント認証情報でアクセストークンが取得できることを確認できる（検証環境での統合テスト）
  - _Requirements: 2.1, 2.2, 2.3, 2.4_
  - _Boundary: VaultwardenOrgClient_

- [x] 3.2 Organization/Collection/メンバー現状取得実装
  - `GET /api/organizations`、`GET /api/organizations/{orgId}/users`、`GET /api/organizations/{orgId}/collections`を呼び出す処理を実装
  - マッピング設定が参照するCollectionが対象Organizationに存在しない場合、当該マッピングのみエラー記録して継続する分岐を実装
  - 対象Organizationの現メンバー一覧（メール・メンバーID・現在のCollection権限）とCollection一覧が取得できることを確認できる
  - _Requirements: 3.1, 3.2, 3.3_
  - _Boundary: VaultwardenOrgClient_

- [x] 3.3 招待・Collection権限更新・削除の適用実装（Confirm待ち検出含む）
  - `POST /api/organizations/{orgId}/users/invite`による招待処理を実装
  - メンバー一覧取得時に`status`フィールドを確認し、`invited`（未Confirm）メンバーを`confirm_pending`リストへ追加する処理を実装（**検証済み**: `PUT`は`status`に関わらず常に成功しDBへ保存されるが、Vaultwarden側で`status=Confirmed`になるまでCollection権限は有効化されない。そのためPUT自体はスキップせず、Confirm待ち検出はDiscord通知判定のみに用いる）
  - `PUT /api/organizations/{orgId}/users/{memberId}`によるCollection権限更新処理を実装し、フルリプレースAPIであることを踏まえマッピング対象外のCollection権限は現状値を保持してマージする（未Confirmメンバーにも送信してよい。Confirm後に自動有効化される）
  - PUTリクエストの`type`フィールドには対象メンバーの**現在のtypeをそのまま再送**する処理を実装（`edit_member`の権限昇格ガードにより、サービスアカウントAdminがtype変更を伴うPUTを送ると403になるため。Collection権限のみを変更し、type自体は変更しない）
  - 同一ユーザーへの複数Collection権限変更を1回のPUTリクエストに集約する処理を実装
  - マージ処理の前後でマッピング対象外のCollection権限が変化しないことを確認できる（ユニットテスト）
  - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 7.1, 7.2, 7.3, 8.1, 8.3_
  - _Boundary: VaultwardenOrgClient, PermissionDiffEngine_

- [x] 4. (P) PermissionDiffEngine実装による権限差分計算
  - マッピングエントリ単位でAuthentikグループメンバーとVaultwarden現状（メンバー・Collection権限）を比較し、招待対象・更新対象・削除対象を算出する処理を実装
  - 同一ユーザー×Organization単位で複数Collectionへの変更を集約する処理を実装
  - 複数グループに所属するユーザーが一部グループのみ脱退した場合、脱退したグループに対応するCollection権限のみを削除対象とし、継続所属グループに対応する権限は変更対象から除外する判定を実装
  - 現在の権限がマッピング設定と一致するユーザーが変更対象から除外されることを確認できる（ユニットテスト）
  - _Requirements: 5.1, 5.2, 5.3, 8.2_
  - _Boundary: PermissionDiffEngine_

- [x] 5. SyncOrchestrator実装による実行順序制御とdry-run分岐
  - マッピング読込→Authentikグループメンバー取得→Vaultwarden現状取得→差分計算→（dry-runでなければ）適用（Confirm済み・未Confirm双方のメンバーへCollection権限PUTを送信する。未Confirm分はConfirm完了まで無効化されたまま保持され、Confirm検知後の再送処理は不要）→ Confirm待ちメンバー検出とDiscord通知キュー追加→ログ出力→Discord通知、の順序を制御する処理を実装
  - dry-runモード有効時はVaultwardenへの変更系API呼び出しを行わず、適用予定の変更内容（Confirm待ち含む）のみをログ出力する分岐を実装
  - 個別エラー（グループ不在・Collection不在・招待失敗）を記録し、他の正常な対象の処理を継続させる
  - dry-runモード有効時にVaultwarden側へ実際の変更が発生しないことを確認できる（統合テスト）
  - _Requirements: 5.3, 6.1, 6.3, 6.4, 6.5, 7.1, 8.1, 9.1, 9.2_
  - _Boundary: SyncOrchestrator_

- [x] 6. (P) DiscordNotifier実装による実行結果通知
  - 同期完了時に招待・更新・削除件数のサマリーを既存`DISCORD_OPS_WEBHOOK_URL`へ通知する処理を実装
  - Confirm待ちユーザーが1件以上存在する場合、サマリーにConfirm待ちユーザー数と対象メールアドレス一覧を含め、管理者がVaultwarden Web UIでConfirm操作を行うことを促すメッセージを追加する処理を実装
  - エラー発生時にエラー内容を通知する処理を実装
  - 通知送信の失敗が同期処理自体の成否に影響しないようにする
  - 同期処理完了後にDiscordへサマリーメッセージが送信されることを確認できる（Webhookモックを用いた統合テスト）
  - _Requirements: 11.1, 11.2, 11.3, 11.4_
  - _Boundary: DiscordNotifier_

- [x] 7. (P) SyncLockManager実装によるLease排他制御
  - `kubectl`経由で固定名Lease（`vaultwarden-rbac-sync-lock`, namespace `prod`）の取得・解放処理を実装
  - Lease取得に失敗した場合、呼び出し元に取得失敗を返し同期処理を実行させない分岐を実装
  - 2プロセスが同時にLease取得を試みた際、片方のみ取得に成功することを確認できる（統合テスト）
  - _Requirements: 10.2, 13.3, 13.4_
  - _Boundary: SyncLockManager_
  - **修正済み（2026-06-25、task 8実装時に発覚）**: 当初実装は`kubectl create --dry-run=client -o yaml | kubectl apply`だったが、`kubectl apply`は既存Lease相手でもresourceVersion不問のupsertで常に成功するため排他制御として機能していなかった（旧ユニットテストはこの実挙動と異なる失敗パターンをモックしており検出できなかった）。実APIへの`kubectl create`（既存時はAlreadyExistsで失敗）+ stale判定（`leaseDurationSeconds`超過時のみ`resourceVersion`を伴う`kubectl replace`で奪取）に変更し、release()もholderIdentity不一致時は削除をスキップするよう修正。テストもkubectl create/get/replace/deleteの実APIサーバー意味論を模倣するFakeLeaseStoreに置き換えた

- [x] 8. 実行エントリポイントへの統合

- [x] 8.1 CronJobエントリポイント統合とマニフェスト定義
  - `--mode=cron`起動時にLease取得→SyncOrchestrator実行（dry_run=False）→Lease解放の一連の流れを接続する
  - `schedule: "0 * * * *"`、`concurrencyPolicy: Forbid`、実行履歴上限を設定したKubernetes CronJobマニフェストを定義
  - CronJobを手動トリガーで起動した際、Lease取得から解放までの一連のログが出力されることを確認できる
  - _Requirements: 10.1, 10.2, 10.3_
  - _Depends: 5, 7_

- [x] 8.2 Trigger Receiver実装とDeployment/Service統合
  - `POST /trigger`エンドポイントと`Authorization: Bearer`検証処理を実装
  - 検証成功時はLease取得を試行し、成功時はバックグラウンドでSyncOrchestratorを起動して即座に202を返し、Lease取得失敗時もログ記録の上202を返す分岐を実装
  - 常駐DeploymentとClusterIP Serviceのマニフェストを定義
  - 正しいBearerトークンでのリクエストが202を返し非同期に同期処理が起動することを確認できる
  - 不正なBearerトークンでのリクエストが401を返すことを確認できる
  - _Requirements: 13.1, 13.2, 13.3, 13.4_
  - _Depends: 5, 7_

- [x] 9. Authentikイベント駆動トリガーのIaC実装

- [x] 9.1 (P) ログイン時即時トリガーのExpression Policy実装
  - 既存`discord_group_sync`パターンを参考に、ログイン成功時にTrigger Receiverへリクエストを送信する`authentik_policy_expression`を実装
  - 対象ログインフローのUserLoginStageへ`authentik_policy_binding`で紐付ける
  - 呼び出し失敗時に例外を握り潰しログイン処理自体を継続させる
  - テストユーザーのログイン後、Trigger Receiverへのリクエストが送信されることを確認できる
  - _Requirements: 13.1_
  - _Depends: 8.2_
  - _Boundary: LoginSyncTriggerPolicy_
  - **実装メモ（2026-06-25）**: `discord_group_sync`が使う`client.do_request()`はOAuth Source専用コンテキストのみに存在するため使えず、代わりに全Expression Policyで標準提供される`requests`グローバル（authentik `BaseEvaluator`の`get_http_session()`）を使用。実環境API (`/api/v3/flows/bindings/?target=<flow_pk>`) で確認した3つのUserLoginStage binding（`default-authentication-flow`直接ログイン: `12ed7fd6-...`、`default-source-authentication`: `550f392f-...`、`default-source-enrollment`: `2ab74f9c-...`、後二者は既存discord_group_syncと共用）全てにバインドし、ログイン経路を問わず即時トリガーされることを保証。`terraform apply`済み・本番反映済み・full plan diff 0件確認済み。
  - **既知のギャップ**: バインド先のTrigger Receiver (`vaultwarden-rbac-sync.prod.svc.cluster.local`) は本コミット時点でtask 4-8のDeployment/Service/CronJobがgit未コミットのため未デプロイ。ログイン時のPOSTは現状失敗するが例外握り潰しのためログイン自体への影響はない（fail-open）。

- [x] 9.2 (P) 管理者操作イベントWebhookの実装
  - Bearerトークンをヘッダに付与する`authentik_event_transport`を定義
  - グループメンバーシップ変更イベントのみを対象とする`authentik_policy_event_matcher`を定義
  - `authentik_event_rule`と`authentik_policy_binding`でMatcherとTransportを紐付ける
  - Authentik管理画面でグループメンバーを変更した際、Trigger Receiverへのリクエストが送信されることを確認できる
  - _Requirements: 13.1_
  - _Depends: 8.2_
  - _Boundary: GroupChangeWebhookRule_
  - **実装メモ（2026-06-25）**: `app`/`model`の正確な値は`terraform providers schema -json`のenum一覧と実Events API実例（2026-06-25時点 `app="authentik.core"`, `model="authentik_core.group"`、action="model_updated"）で確認済み。**providerバグ回避**: `destination_group`未設定(null)のままだとレスポンスの`destination_group_obj`がnullになり、terraform-provider-authentik 2026.2.0のGoクライアントがネストオブジェクトデコードに失敗し`HTTP Error 'no value given for required property pk'`でcreate/import双方が失敗する実機確認済み不具合。`destination_group`に既存`管理者`グループを指定することで回避（管理者への可視化という副次的利点もある）。`destination_event_user=true`も設定し、NotificationRule.destination_users()が空になりWebhookが発火しない事態を防止。`terraform apply`済み・本番反映済み・full plan diff 0件確認済み。

- [ ] 10. E2E検証とドリフト修復確認

- [ ] 10.1 オンボーディングE2Eテスト（2段階フロー）
  - 一段階目: テスト用Authentikグループへの新規メンバー追加→トリガー（ログインまたはWebhook）→Vaultwarden招待送信→Discord「Confirm待ちN件」通知確認を一連で確認できる
  - 二段階目: 上記の後、管理者がWeb UIでConfirm→次回CronJob実行時にCollection権限が自動適用されることを確認できる
  - _Requirements: 1.1, 6.1, 6.2, 6.4, 6.5, 7.1, 11.4_
  - _Depends: 9.1, 9.2

- [ ] 10.2 オフボーディングE2Eテスト
  - テスト用Authentikグループからのメンバー脱退→トリガー→対象Collection権限剥奪までの反映を確認できる
  - Organizationからの除名が発生せず、Collection権限の剥奪のみが行われることを確認できる
  - _Requirements: 8.1, 8.2, 8.3
  - _Depends: 9.1, 9.2

- [ ] 10.3 冪等性・同時実行排他のリグレッション確認
  - 差分が存在しない状態でCronJobを実行した場合、Vaultwarden側に変更が発生せず正常終了することを確認できる
  - CronJobとイベント駆動トリガーがほぼ同時に発火した場合、一方のみが実行され他方はLease競合により実行を見送ることを確認できる
  - _Requirements: 10.2, 10.3, 13.3, 13.4
  - _Depends: 8.1, 8.2
