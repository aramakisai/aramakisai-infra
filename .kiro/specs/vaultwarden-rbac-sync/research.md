# Research & Design Decisions

## Summary
- **Feature**: `vaultwarden-rbac-sync`
- **Discovery Scope**: Complex Integration（既存 Authentik / Vaultwarden への外部API連携 + イベント駆動トリガー）
- **Key Findings**:
  - Vaultwarden の **Organization API Key**（client_credentials, `client_id=organization.<uuid>`）はユーザーコンテキストを持たず、`/public/organization/import`（Directory Connector互換のbulk import）専用。招待・Collection権限変更に必要な `AdminHeaders`/`ManagerHeaders` ガードは通過できない。
  - Collection権限変更（`PUT /organizations/{orgId}/users/{memberId}`）と招待（`POST /organizations/{orgId}/users/invite`）はいずれも `AdminHeaders` ガード必須＝実ユーザーとしてのログインが必要。**User Personal API Key**（client_credentials, `client_id=user.<uuid>`）はユーザーコンテキスト付きトークンを返すため、専用サービスアカウントユーザーをOrganizationのAdmin/Ownerにすれば利用可能。
  - User API Key ログインは `SSO_ONLY=true` を迂回する（コード上 `password` グラントのみがSSO_ONLYでブロックされる）。ただしAPI Key自体をUI上で発行するには初回ログイン（マスターパスワード設定）が必要で、SSO_ONLY有効時は通常ログインフォームが出ないため**鶏と卵問題**が生じる。
  - Authentik 側のイベント駆動トリガーは、(a) 既存の `authentik_policy_expression` をログインフローにバインドするパターン（`discord_group_sync` で実証済み、外部HTTP呼び出し実績あり）と、(b) 標準の `authentik_event_transport`(webhook) + `authentik_policy_event_matcher` + `authentik_event_rule` の組み合わせ、の2系統がTerraformで管理可能。

## Research Log

### Vaultwarden Organization API Key の実際の権限範囲
- **Context**: 要件定義の前提（Personal API Key for client_credentials grant）が、招待・Collection権限変更まで実際にカバーできるか検証する必要があった。
- **Sources Consulted**:
  - [Organisation API for n8n #2317](https://github.com/dani-garcia/vaultwarden/discussions/2317)
  - vaultwarden source: `src/api/identity.rs`（`api_key_login`/`organization_api_key_login`/`user_api_key_login`）
  - vaultwarden source: `src/api/core/organizations.rs`（ルート定義・ガード種別）
  - vaultwarden source: `src/api/core/public.rs`（`/public/organization/import` ハンドラ `ldap_import`）
- **Findings**:
  - `organization_api_key_login` は `client_id=organization.<org_uuid>` を受理し、**ユーザーコンテキストを持たないorganizationスコープのトークン**を返す。
  - この種のトークンの唯一の用途は `POST /public/organization/import`（ガード`PublicToken`）。受理ペイロードは `groups`（name, external_id, member_external_ids）、`members`（email, external_id, deleted）、`overwrite_existing` のみで、**Collection権限割り当てロジックは存在しない**。`overwrite_existing=true` 時は最後のConfirmed Ownerを除き、ペイロードに無いメンバーを削除する安全策がある。
  - Collection権限変更 `PUT /organizations/<org_id>/users/<member_id>`（`put_member`/`edit_member`）と招待 `POST /organizations/<org_id>/users/invite`（`send_invite`）はいずれも **`AdminHeaders`** ガード＝そのOrganizationでAdmin/Owner権限を持つ実ユーザーとしての認証が必須。
  - `user_api_key_login`（`client_id=user.<user_uuid>`）は対象ユーザーの `api_key` フィールドを検証し、**ユーザーコンテキスト付きの通常のaccess_token**（refresh_tokenなし、Bearer、`AdminHeaders`等を満たせる）を返す。2FAはバイパスされる。
- **Implications**: 本機能が必要とするCollection権限の変更・招待は、**専用サービスアカウントユーザー（User Personal API Key）を対象Organization全てでAdmin/Owner登録する方式**でのみ実現可能。Organization API Keyは本要件には使えない（読み取りや一括import用途では将来検討の余地あり、現時点では不採用）。

### サービスアカウントユーザーのブートストラップと SSO_ONLY
- **Context**: Personal API Key はユーザー自身が「設定 → セキュリティ → キー」でマスターパスワード確認の上で発行する。`SSO_ONLY=true` 環境でこの操作をどう行うか。
- **Sources Consulted**: vaultwarden source `src/api/identity.rs`（`password_login` 内の `CONFIG.sso_enabled() && CONFIG.sso_only()` チェック）
- **Findings**: `SSO_ONLY=true` は `/identity/connect/token` の **`password` グラント自体をサーバー側で拒否**する（UI非表示だけでなくプロトコルレベルでブロック）。`client_credentials` グラントには同等のチェックが存在しない。すなわちAPI Key発行の前提となる初回マスターパスワード設定・ログインは、SSO_ONLY有効時はサービスアカウントでも実行不可。
- **Implications**: サービスアカウントユーザーの初回セットアップ（マスターパスワード設定 + Personal API Key発行）は、**一時的に `SSO_ONLY=false` に戻す手動ブートストラップ手順**が必要（既存の `ORG_CREATION_USERS` 等の手動初期設定と同種の一回限りの運用作業として許容）。CronJob/トリガー本体の自動化対象外。

### Vaultwarden Organization Users / Collections API のデータ契約
- **Context**: 差分適用に使う具体的なリクエスト構造を確認する。
- **Sources Consulted**: vaultwarden source `src/api/core/organizations.rs`（`EditUserData`, `InviteData`, `CollectionData`構造体）
- **Findings**:
  - `CollectionData { id, read_only: bool, hide_passwords: bool, manage: bool }` — `.kiro/steering/vaultwarden-rbac.md` の4権限レベルは次のように対応する：
    | 権限レベル | read_only | hide_passwords | manage |
    |---|---|---|---|
    | Can View | true | false | false |
    | Can View Except Passwords | true | true | false |
    | Can Edit | false | false | false |
    | Can Manage | false | false | true |
  - `EditUserData { type, collections: Option<Vec<CollectionData>>, groups: Option<Vec<GroupId>>, permissions }` — `PUT /organizations/<org_id>/users/<member_id>` で使用。
  - `InviteData { emails: Vec<String>, groups: Vec<GroupId>, type, collections: Option<Vec<CollectionData>>, permissions }` — `POST /organizations/<org_id>/users/invite` で使用。招待と同時にCollection権限を指定可能。
  - `groups` フィールドはAPI上存在するが、OSS版はEnterprise Groups機能を実装していないため実質未使用（空配列を送る）。
- **Implications**: 招待時にCollection権限を同時指定できるため、「招待」と「権限付与」を2リクエストに分けず1回で完結できる（Requirement 6.2 を満たす）。既存メンバーの権限更新は `PUT .../users/<member_id>` の `collections` 配列を都度フルリプレースで送信する（部分更新APIではない点に注意）。

### Authentik イベント駆動トリガーの実現方式
- **Context**: ログイン・属性変更・グループメンバーシップ変更を即時検知してWebhook相当の通知を送る方法、かつTerraformで管理可能か。
- **Sources Consulted**:
  - 既存コード `terraform/authentik_discord.tf`（`discord_group_sync` Expression Policy、ログインフローバインド実績）
  - [Notification Transports](https://docs.goauthentik.io/sys-mgmt/events/transports/) / [Notification Rules](https://docs.goauthentik.io/sys-mgmt/events/notifications/)
  - terraform-provider-authentik docs: `event_transport.md`, `policy_event_matcher.md`, `event_rule.md`
- **Findings**:
  - 既存実装で実証済み：`authentik_policy_expression` はPython式の中から `client.do_request()` で任意の外部HTTPエンドポイント（Discord API）を呼び出しており、内部ネットワーク向けの呼び出しも技術的に同様に可能。`discord_group_sync` は `authentik_policy_binding` でデフォルトフロー（`default-source-authentication` / `default-source-enrollment` のUserLoginStageバインド、UUID固定値）にバインドされている。
  - 公式の汎用Webhook機構は3つのTerraformリソースの組み合わせ：
    - `authentik_event_transport`（`mode=webhook`, `webhook_url`, `webhook_mapping_headers` でカスタムヘッダー＝Bearer等の認証情報を付与可能）
    - `authentik_policy_event_matcher`（`action`, `app`, `model` 等でイベント種別をフィルタ。例: `action=model_updated` かつ `model=<group相当>` でグループ変更を検知。Group更新イベントの `event.context["diff"]["users"]["add"/"remove"]` でメンバー増減を判定可能）
    - `authentik_event_rule`（`transports` を指定し、`authentik_policy_binding` で上記Matcherポリシーを紐付ける、UI上の「Notification Rule」に相当）
  - 正確な `app`/`model` 文字列（Django app label形式、例: `authentik_core.group` 等）はバージョン依存のため設計確定前に実環境での確認が必要（Open Questionとして残す）。
- **Implications**: ログイン即時同期は既存パターン（Expression Policy + ログインフローバインド）を再利用し、管理者による手動グループ変更の即時検知は標準のEvent Matcher + Webhook機構を新規追加する、という**2系統のトリガー**を採用する。両方ともTerraformで宣言的に管理できる。

### Webhook受信側の認証とKubernetes内部経路
- **Context**: Requirement 13.2（未認証呼び出し拒否）をどう満たすか、Authentikから到達可能な経路をどう設計するか。
- **Sources Consulted**: 既存 `gitops/manifests/prod/` の namespace 配置（Authentik・Vaultwarden双方 `prod` namespace）、`steering/tech.md`（Cilium CNIだがNetworkPolicy未使用の現状）
- **Findings**: AuthentikもVaultwardenも同一 `prod` namespace 上で稼働しており、`prod` 内でNetworkPolicy/CiliumNetworkPolicyによる制限は現状適用されていない。Authentik workerポッドから他Podの ClusterIP Service への直接到達は既存の外部HTTP呼び出し実績（Discord API）と同様に問題なく成立する。
- **Implications**: トリガー受信エンドポイントは**ClusterIP Serviceのみで公開し、Cloudflare Tunnel等の外部公開は不要**。共有Bearerトークン（新規Infisicalシークレット）による認証をアプリケーションレベルで実施する（Requirement 13.2）。将来的にCiliumNetworkPolicyでingressを `authentik` Podのラベルに限定する追加の防御も可能（本設計ではOpen Questionとして記録）。

### Confirm前のPUT（Collection権限更新）の実際の挙動（ローカルDocker検証）
- **Context**: 当初「未ConfirmメンバーへのPUTはAdminHeadersガードのみで成功するはず」という想定（research.md冒頭の`organizations.rs`ソース調査）から、設計時に誤って「未ConfirmへのPUTは失敗する」という逆の前提を一度設計に書いてしまった。実機検証で実際の挙動を確定させる必要があった。
- **Sources Consulted**: ローカルDocker環境（`vaultwarden/server:latest` 2026.5.0 + Postgres 16、`/tmp/vw-test/docker-compose.yml`）でOrganization作成→メンバー招待→未ConfirmメンバーへのPUTを実行し、PostgreSQLへ直接`SELECT`して実データを確認。Vaultwardenソース`src/db/models/collection.rs`の`Collection::find_by_user_uuid`を直接読解。
- **Findings**:
  - `PUT /organizations/{orgId}/users/{memberId}` はstatus（Invited/Confirmed）に関わらず常に200で成功し、`users_collections`テーブルへの保存も実際に行われる（DBで確認済み）。`edit_member`実装（`src/api/core/organizations.rs`）にstatusチェックは存在しない。
  - しかし`Collection::find_by_user_uuid`が`.filter(users_organizations::status.eq(MembershipStatus::Confirmed as i32))`という条件を持つため、**未Confirmの間はそのメンバーのCollection権限が一切「存在しない」ものとして扱われる**。`GET /organizations/{orgId}/users`の`collections`配列も常に空。クライアント側でもアクセス不可。
  - Confirm完了の瞬間に、事前にPUT済みの権限が自動的に有効化される（再度PUTし直す必要はない）。
  - Vaultwarden OSSは`organizations`テーブル作成時のレスポンスで`"useGroups":false`を明示しており、Enterprise Groups機能が無効であることを実機でも再確認した。
- **Implications**: 同期ロジックは未ConfirmメンバーへのPUT送信をスキップする必要は無い（スキップすると後でConfirmされた際に反映が1サイクル遅れるだけで実害は無いが、スキップしない方がシンプル）。PUTはフルリプレースで冪等なため、Confirm待ちの間も毎回マッピング通りに送信して問題ない。Confirm待ち検出はDiscord通知の判定にのみ使う（`get_member_status`、design.md参照）。

### Vaultwarden Collection名はOrg鍵でクライアント暗号化される（ローカルDocker検証、2026-06-25）
- **Context**: design.mdの`RbacMappingConfigMap`は「Organization/Collectionは人間が読めるnameで記述し、IDは同期エンジンが実行時にVaultwarden APIから解決する」という前提で設計されていた。task 3（VaultwardenOrgClient）の実装着手前に、この前提が実際のVaultwardenデータモデルと整合するか検証する必要があった。
- **Sources Consulted**: ローカルDocker環境（`vaultwarden/server:latest` + Postgres 16、使い捨て環境）に対し、Playwright + 実際の公式Web Vaultクライアント（`vaultwarden-web-vault`、暗号化ロジックを含む本物のクライアント）で実際にユーザー登録・ログイン・Organization作成・Collection確認を行い、その結果をPostgreSQLへ直接`SELECT`して実データを確認した。
- **Findings**:
  - `organizations.name`は平文で保存される（例: `VERIFY_ORG_PLAINTEXT_TEST`のまま）。Organization名はバルト解錠（マスターパスワード入力）前にアカウント切替UIへ表示する必要があるため、サーバー側で平文保持される。
  - `collections.name`はOrganization作成時にクライアントが自動生成する「Default collection」であっても、本物のBitwarden CipherString形式（`2.<iv_base64>|<ciphertext_base64>|<mac_base64>`、AES-256-CBC + HMAC-SHA256）で保存される。これはOrganization鍵（Org Owner/Adminの復号済み鍵でのみ復号可能）でクライアント側暗号化されたものであり、**サーバー（および本機能のような非ブラウザAPIクライアント）には復号する手段が無い**。
  - したがって、`GET /api/organizations/{orgId}/collections`のレスポンスの`name`フィールドは常にこの暗号化文字列であり、`mapping.json`の人間可読なCollection名（例: `"広報"`）と文字列一致することは**原理的にあり得ない**。
- **Implications**: design.mdが前提としていた「Collection名でAPI照合してIDを解決する」方式は実装不可能。Organization名（平文）でのマッチングは引き続き有効。Collectionは**`collection_id`（UUID）をmapping.jsonに直接記述する方式**に変更する。人間可読性を失わないよう、レビュー専用の`collection_label`（コメント相当、照合には使わない）を任意フィールドとして残す。`collection_id`の実際の値は、Organization Owner/Adminが実ブラウザでWeb Vaultにログインし、対象Collectionを開いて表示されるIDをコピーする一回限りの人手作業が必要（既存の「サービスアカウント初回ブートストラップ」と同種の運用作業）。

### Bitwarden公式Directory Connector（bwdc）併用案の検討と不採用
- **Context**: 自前でinvite/PUTを実装する代わりに、Bitwarden公式のDirectory Connector（`JonTheNiceGuy/vaultwarden-sync`がVaultwarden向けにパッケージ化）をLDAP(Authentik LDAP Outpost)と組み合わせて使えば、ユーザー/グループ同期の再実装を避けられないか検討した。
- **Sources Consulted**: vaultwardenソース`src/api/core/public.rs`の`ldap_import`ハンドラ（`/public/organization/import`）全文。Organization API Key発行エンドポイント`src/api/core/organizations.rs`の`api_key`関数（`PasswordOrOtpData::validate`）。
- **Findings**:
  - `ldap_import`がメンバーに設定する`MembershipStatus`は`Invited`または（`mail_enabled()`が false かつ既存ユーザーの場合のみ）`Accepted`が最大値。`Confirmed`に進める分岐はコード全体（249行）に一切存在しない。つまり**bwdc経由のimportでもConfirm問題は回避できない**。importは「招待を出す」フェーズの代替に過ぎず、Collection権限割り当てロジックも持たない（既存研究で確認済み）。
  - Organization API Key（`client_id=organization.<uuid>`）の発行自体は、Confirmとは異なる検証で完結する。`api_key`エンドポイントは`AdminHeaders`ガード＋`PasswordOrOtpData::validate`で、これは`user.check_valid_password(pw_hash)`という**サーバー側の文字列比較のみ**（クライアント側の鍵復号は不要）。よってサービスアカウント自身のマスターパスワードハッシュをInfisicalに保管しておけば、Organization API Key自体の取得は完全自動化できる（Confirmのような暗号的制約はここには無い）。
- **Implications**: 「Organization API Key + import」へ切り替えても、(a) Confirm問題は解決しない、(b) Collection権限はimportでは扱えないため別途PUTの実装が必須、(c) 認証経路がUser Personal API Key（招待・PUT用）とOrganization API Key（import用）の2種類に増え複雑性が上がる。再実装の回避という利点に対しメリットが小さいため**不採用**。現行設計（User Personal API Keyでinvite + PUTを直接呼ぶ）を継続する。

## Architecture Pattern Evaluation

| Option | Description | Strengths | Risks / Limitations | Notes |
|--------|-------------|-----------|---------------------|-------|
| CronJobのみ（ポーリング限定） | 1時間毎の定期実行のみ | 実装最小 | Requirement 13（即時反映）を満たせない | 不採用 |
| CronJob + 都度Job生成（Webhook受信→K8s Job作成） | 受信時にk8s API経由でJobをcreate | CronJobと完全に同一実行単位 | 受信側に Job作成RBAC が必要、同時実行排除の実装がやや複雑 | 不採用（複雑さに対するメリット小） |
| **CronJob + 常駐Trigger Receiver（Lease排他制御）（採用）** | 同一コンテナイメージを2モード（`mode=cron` / `mode=serve`）で実行し、`coordination.k8s.io/v1 Lease` で相互排他 | k8s標準プリミティブのみで同時実行制御、ロジックは単一実装を共有 | 常駐Pod1つ分のリソースが増える（軽量） | 採用 |

## Design Decisions

### Decision: Vaultwarden認証方式は User Personal API Key + 専用サービスアカウント
- **Context**: Collection権限変更・招待エンドポイントは `AdminHeaders` ガード必須で、Organization API Keyでは到達不可能と判明。
- **Alternatives Considered**:
  1. Organization API Key（`/public/organization/import`）でメンバー存在のみpush → Collection権限は別経路が必要になり一貫性が崩れる
  2. ADMIN_TOKEN（管理パネルAPI）を流用 → 管理パネルは「Organizationへの新規追加」も「Collection権限」も非対応（役割変更のみ）と判明
  3. **User Personal API Key（採用）**: 専用サービスアカウントユーザーを作成し、対象Organization全てでAdmin/Owner権限を付与、Personal API Keyでclient_credentialsログイン
- **Selected Approach**: 専用ユーザー（例: `rbac-sync-bot@aramakisai.invalid`）を一度だけ手動ブートストラップし、各Organizationへ Admin として参加させる。Personal API Key（client_id/client_secret）をInfisicalに保管し、CronJob/Trigger Receiverが都度 `/identity/connect/token`（grant_type=client_credentials, scope=api）でaccess_tokenを取得して利用する。
- **Rationale**: 唯一、要件が求めるCollection権限変更・招待APIに到達できる現実的な経路。
- **Trade-offs**: 初回セットアップ（マスターパスワード設定・API Key発行）はSSO_ONLY一時解除を伴う手動作業が必要。サービスアカウントの資格情報はOrganization全体のAdmin権限を持つため漏洩時の影響範囲が大きく、Infisical管理を徹底する。
- **Follow-up**: 実装時にSSO_ONLY一時解除の具体的な作業手順をrunbook化し、`.kiro/steering/vaultwarden-rbac.md` に追記する。

### Decision: イベント駆動トリガーは「ログインバインド式」と「Event Matcher Webhook」の併用
- **Context**: Requirement 13 はログイン・属性変更・グループメンバーシップ変更の3トリガーを要求するが、ログインに紐付かない管理者操作（Authentik admin UIでのグループ脱退等）は別経路が必要。
- **Alternatives Considered**:
  1. ログインバインドのみ → 管理者が即時剥奪したくてもユーザーが次にログインするまで（最大1時間後のCronJobまで）反映が遅れる、オフボーディングの即時性に欠ける
  2. Event Matcher Webhookのみ → ユーザーが新しいグループに入った直後にログインしても「ログインの瞬間に確実に最新化されている」保証がない（Webhook配送タイミングとログインタイミングが非同期）
  3. **併用（採用）**: ログイン時は既存 `discord_group_sync` と同様の Expression Policy で都度トリガー、管理者操作によるグループ変更は `authentik_event_transport` + `authentik_policy_event_matcher` + `authentik_event_rule` のWebhookでトリガー
- **Selected Approach**: 2系統とも同一のTrigger Receiver `/trigger` エンドポイントを共有Bearerトークンで呼び出す。
- **Rationale**: オンボーディング（ログイン時に最新化）とオフボーディング（管理者操作で即時剥奪）の両方を最小コストでカバーできる。
- **Trade-offs**: Authentik側のTerraformリソースが増える（Expression Policy 1つ + Event系3リソース）が、いずれもIaC化されるため運用ドリフトのリスクは低い。
- **Follow-up**: 実環境の `app`/`model` 文字列値、および「ログイン」を確実に捕捉できるフロー/UserLoginStageバインドのUUIDは実装時に `make kubectl` 経由のAuthentik Admin UI/APIで確認する。

### Decision: CronJobとイベント駆動トリガーは単一コンテナイメージ + Lease排他制御
- **Context**: Requirement 10.2 / 13.3 が定期実行とイベント駆動実行の同時実行を防止することを要求する。
- **Alternatives Considered**:
  1. Webhook受信時にk8s Job APIで都度Job生成 → RBAC・Job命名・後始末の実装コストが増える
  2. **常駐Deployment + Lease（採用）**: 同一イメージを `serve`モードで常駐させ、CronJobの`cron`モードと同じ `coordination.k8s.io/v1 Lease` を取得できた側のみ実処理を行う
- **Selected Approach**: Lease名固定（例: `vaultwarden-rbac-sync-lock`、namespace `prod`）。取得失敗時はCronJob側はログに記録して正常終了、Trigger Receiver側は202 Acceptedで「次回CronJodまたは次のイベントで反映される」ことをDiscord/ログに記録（Requirement 13.4）。
- **Rationale**: 追加のk8s権限（Job create等）を増やさず、標準の分散ロック機構のみで要件を満たせる。
- **Trade-offs**: 常駐Pod1つ分のリソースを消費する（CPU/メモリは軽量見込み）。
- **Follow-up**: Lease の `leaseDurationSeconds` と同期処理の最大実行時間の関係を実装時に調整する。

### Decision: mapping.jsonのCollection指定はID直接記述方式に変更（name解決方式から変更）
- **Context**: 当初設計（design.md初版）は「Organization/Collectionは人間が読めるnameで記述し、IDは同期エンジンが実行時にVaultwarden APIから解決する」前提だった。上記「Vaultwarden Collection名はOrg鍵でクライアント暗号化される」の検証により、この前提はCollectionに関して成立しないことが判明した。
- **Alternatives Considered**:
  1. **name解決方式（旧設計、不採用）**: `GET /organizations/{orgId}/collections`の`name`と`mapping.json`の`collection`を文字列一致 → Collection名は常に暗号化CipherStringのため原理的に一致しない
  2. 同期エンジン自身がCollectionを直接作成し平文nameで管理する → design.mdのNon-Goal（Vaultwarden Organization・Collection自体の新規作成は対象外）に抵触し、かつ人間が実Web Vault UIでそのCollectionを開いた際にクライアントが復号に失敗し表示が壊れる実害がある
  3. **`collection_id`（UUID）を`mapping.json`に直接記述（採用）**: Organization名（平文確認済み）は引き続きnameで解決し、Collectionのみ事前に人間が実ブラウザで確認したUUIDを使う。レビュー用に`collection_label`（任意、照合には使わない）を残す
- **Selected Approach**: `MappingEntry`を`(authentik_group, organization, collection_id, permission)`必須・`collection_label`任意に変更。`VaultwardenOrgClient`はCollection IDが対象Organizationの`GET .../collections`が返すID一覧に存在するかのみを検証し（名前比較はしない）、存在しない場合は当該マッピングのみエラー記録して継続する（Requirement 3.3は維持）。
- **Rationale**: Organization名は実機検証で平文確認済みのため変更不要。Collectionだけ実害のある箇所を最小限修正する。
- **Trade-offs**: `collection_id`の特定には人間が実ブラウザでログインしCollectionを開いてUUIDをコピーする一回限りの作業が必要（Organization Owner/Adminのブラウザ操作、既存の[[project_vaultwarden_rbac_sync_bootstrap_blockers]]と同種の制約）。`mapping.json`のGit diffはUUIDの羅列になり`collection_label`を読む必要があるが、`collection_label`を併記すれば実質的なレビュー性は保たれる。
- **Follow-up**: `.kiro/steering/vaultwarden-rbac.md`のオンボーディング手順に「Collection IDの確認方法（Web Vault → 対象Collection → URL or 詳細表示でUUIDを確認）」を追記する。

## Risks & Mitigations
- **サービスアカウントの資格情報漏洩** — Admin/Owner権限を持つ強い資格情報のため、ExternalSecret経由のみで配布し、ログに平文出力しない（Requirement 12）。定期的なローテーションを運用手順に含める。
- **Authentikの `app`/`model` フィルタ文字列がバージョン依存で不正確だと、グループ変更Webhookが発火しない** — 実装時に実環境で動作確認（テストグループでの追加削除→Webhook発火確認）を行う。
- **ログインバインドのHTTP呼び出しがログインフローをブロック/遅延させる** — `discord_group_sync` 同様、例外処理で同期処理自体の失敗がログインを阻害しないようにし、タイムアウトを短く設定する。
- **PUT /organizations/.../users/{id} がフルリプレースAPIのため、意図しない権限欠落を起こしうる** — 差分計算時に対象ユーザーの全Collection権限（マッピング対象外のCollectionも含む）を取得し、リクエスト構築前に現状とマージするロジックを実装で保証する。
- **`mapping.json`の`collection_id`が指す先のCollectionが削除・別名変更された場合、Gitの差分だけでは検知できない** — `collection_label`を併記し、定期的にOrganization内のCollection一覧と`collection_label`の対応を目視確認する運用を推奨する（自動検証は本機能のスコープ外）。

## References
- [Organisation API for n8n #2317](https://github.com/dani-garcia/vaultwarden/discussions/2317) — Organization API Keyの実装範囲についての保守者コメント
- [Notification Transports | authentik](https://docs.goauthentik.io/sys-mgmt/events/transports/) — Webhook通知の仕組みとカスタムヘッダー
- [Notification Rules | authentik](https://docs.goauthentik.io/sys-mgmt/events/notifications/) — Event Matcher Policyとの連携
- [Bitwarden Public API](https://bitwarden.com/help/public-api/) — client_credentials grant・scope=api.organizationの一般仕様（vaultwardenは未実装の部分を含む）
- vaultwarden source: `src/api/identity.rs`, `src/api/core/organizations.rs`, `src/api/core/public.rs`（GitHub `dani-garcia/vaultwarden` main branch, 2026-06時点）
- terraform-provider-authentik docs: `event_transport.md`, `policy_event_matcher.md`, `event_rule.md`（GitHub `goauthentik/terraform-provider-authentik` main branch）
