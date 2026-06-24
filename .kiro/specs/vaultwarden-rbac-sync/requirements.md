# Requirements Document

## Project Description (Input)
vaultwarden-rbac-sync Authentikグループメンバーシップをマスタ情報源として、Vaultwarden Organization/Collectionの権限を自動同期するKubernetes CronJob。Vaultwarden OSSはグループベースの権限割り当てAPIを持たないため、ユーザー単位のCollection権限を手動運用するのはアンチパターン。代わりに、Personal API Key (client_credentials grant、SSO_ONLY=trueでもブロックされない) でVaultwarden Organization APIを呼び出し、Authentikグループ名とVaultwarden Collection名のマッピング設定に基づいて、グループメンバーシップの差分をCollection権限 (PUT /organizations/{orgId}/users/{memberId} の collections配列) に反映する。Authentik API (既存のPRESENCE_AUTHENTIK_API_TOKENパターンを参考に新規トークン発行) でグループメンバー一覧を取得し、Vaultwarden Organization APIでメンバー一覧・Collection一覧を取得して差分を計算する。未参加ユーザーへの招待 (POST /organizations/{orgId}/users/invite) や、グループ脱退時の権限削除も含む。設定はGitOps ConfigMapでマッピング(group_name → collection_name → permission_level)を管理し、CronJobは1時間毎に実行してドリフトを自動修正する。dry-runモードと実行ログのDiscord通知も検討する。

## Introduction

Vaultwarden OSS はグループベースの権限割り当て API を持たず、Collection 権限はユーザー単位でのみ設定可能である。現状は `.kiro/steering/vaultwarden-rbac.md` に定義された手順に従い、Authentik グループメンバーシップの変更を管理者が Web UI で手動反映している。本機能は、この手動運用を Kubernetes CronJob による自動化に置き換える。Authentik グループメンバーシップを唯一の情報源（Source of Truth）とし、GitOps で管理する ConfigMap のマッピング設定（Authentik グループ名 → Vaultwarden Organization/Collection → 権限レベル）に基づいて、Vaultwarden Organization API 経由でメンバーシップと Collection 権限の差分を検出・修正する。これにより、グループメンバーシップ変更からインフラ反映までの遅延と、管理者による手動操作の不整合リスクを排除する。1時間毎の定期実行に加え、Authentik 側のログイン・属性変更イベントをトリガーとした即時実行も行うことで、反映遅延を最小化する。

## Boundary Context

- **In scope**: Authentik グループメンバーシップの取得、Vaultwarden Organization メンバー一覧・Collection 一覧の取得、設定マッピングに基づく差分計算、未参加ユーザーの Organization 招待、Collection 単位の権限付与・更新・剥奪、dry-run 実行、実行結果の Discord 通知、CronJob による定期実行（1時間毎）、Authentik 側のログイン・属性変更（グループメンバーシップ変更含む）イベントをトリガーとした即時実行。
- **Out of scope**: Authentik グループ自体の作成・編集（Authentik 側は既存の手動運用または別仕様に従う）、Vaultwarden Organization・Collection 自体の新規作成（既存のものを対象とする）、ユーザーが Organization 招待を受諾する操作、**管理者による Confirm 操作**（Organization 鍵の再暗号化にマスターパスワードが必要なため自動化不可。CronJob は Confirm 待ちユーザーを Discord 通知で管理者に知らせ、管理者が Web UI で手動クリックする設計。Bitwarden 公式 Directory Connector も Confirm を自動化していない）、グループに紐付かない理由でのユーザーの Organization 完全除名（Collection 権限の剥奪のみを行い、Organization からの除名は `.kiro/steering/vaultwarden-rbac.md` のオフボーディング手順に従い手動で行う）。
- **Adjacent expectations**: マッピング設定の追加・変更は GitOps ConfigMap の更新で行う（CronJob 自体の再デプロイは不要）。新規に発行する Authentik API トークン・Vaultwarden API 認証情報は `steering/tech.md` の ExternalSecret パターンと Infisical シークレット管理規約に従う。

## Requirements

### Requirement 1: Authentik グループメンバーシップ取得

**Objective:** As an インフラ管理者, I want RBAC同期Job が Authentik から最新のグループメンバーシップを取得できること, so that Vaultwarden 側の権限の正本情報として利用できる

#### Acceptance Criteria

1. When 同期処理が開始された, the RBAC同期Job shall マッピング設定で参照されている全 Authentik グループのメンバー一覧（メールアドレス）を Authentik API から取得する
2. The RBAC同期Job shall 既存の `PRESENCE_AUTHENTIK_API_TOKEN` パターンに従い発行された専用 API トークンで Authentik API を認証する
3. If Authentik API 呼び出しが認証エラーまたはタイムアウトで失敗した, then the RBAC同期Job shall 当該実行を中断し、Vaultwarden 側へのいかなる変更も適用しない
4. If マッピング設定で参照されているグループが Authentik 上に存在しない, then the RBAC同期Job shall 当該グループをエラーとして記録し、他の正常なグループの同期処理は継続する

### Requirement 2: Vaultwarden API 認証

**Objective:** As an インフラ管理者, I want RBAC同期Job が `SSO_ONLY=true` 環境下でも Vaultwarden Organization API を呼び出せること, so that パスワードログインを無効化したまま自動同期を実現できる

#### Acceptance Criteria

1. The RBAC同期Job shall Vaultwarden Organization の Personal API Key（client_credentials grant）を用いて Vaultwarden API のアクセストークンを取得する
2. While `SSO_ONLY=true` が有効である, the RBAC同期Job shall client_credentials grant によるアクセストークン取得がブロックされないことを前提として動作する
3. If アクセストークンの取得に失敗した, then the RBAC同期Job shall 当該実行を中断し、Discord に認証失敗を通知する
4. The RBAC同期Job shall Vaultwarden API 認証情報を ExternalSecret 経由で Infisical から取得し、マニフェストに平文で含めない

### Requirement 3: Vaultwarden 現状データ取得

**Objective:** As an インフラ管理者, I want RBAC同期Job が同期対象 Organization の現在のメンバー・Collection 状態を取得できること, so that Authentik側の情報との差分を計算できる

#### Acceptance Criteria

1. The RBAC同期Job shall マッピング設定で参照されている各 Vaultwarden Organization のメンバー一覧（メールアドレス、メンバーID、現在の Collection 権限）を取得する
2. The RBAC同期Job shall マッピング設定で参照されている各 Organization の Collection 一覧（Collection名、Collection ID）を取得する
3. If マッピング設定で指定された Collection が対象 Organization に存在しない, then the RBAC同期Job shall 当該マッピングをエラーとして記録し、他の正常なマッピングの同期処理は継続する

### Requirement 4: グループ-Collection マッピング設定管理

**Objective:** As an インフラ管理者, I want Authentik グループと Vaultwarden Organization/Collection/権限レベルの対応関係を GitOps で宣言的に管理できること, so that マッピング変更を Git 経由でレビュー・追跡できる

#### Acceptance Criteria

1. The RBAC同期Job shall Authentik グループ名・対象 Vaultwarden Organization・Collection名・権限レベルの対応関係を GitOps管理の ConfigMap から読み込む
2. The RBAC同期Job shall 権限レベルとして `.kiro/steering/vaultwarden-rbac.md` で定義された `Can View` / `Can View Except Passwords` / `Can Edit` / `Can Manage` の4種別をサポートする
3. If ConfigMap のマッピング定義の構文または必須フィールドが不正である, then the RBAC同期Job shall 同期処理全体を中断し、Discord にエラーを通知する
4. While 1つの Authentik グループが複数の Organization/Collection にマッピングされている, the RBAC同期Job shall すべてのマッピング先に対して権限同期を適用する

### Requirement 5: 権限差分計算

**Objective:** As an インフラ管理者, I want RBAC同期Job が Authentik の実態と Vaultwarden の現状の差分を正確に計算できること, so that 必要最小限の変更のみが適用される

#### Acceptance Criteria

1. The RBAC同期Job shall マッピング定義ごとに、Authentik グループメンバーと Vaultwarden Organization の現メンバー・Collection権限を比較し、追加・更新・削除が必要な対象を算出する
2. When 算出されたユーザーの現在の Collection 権限が マッピング設定の権限レベルと一致している, the RBAC同期Job shall 当該ユーザーに対する変更操作を実施しない
3. The RBAC同期Job shall 差分計算結果（追加対象・権限更新対象・削除対象のユーザー一覧）を実行ログに記録する

### Requirement 6: Organization 未参加ユーザーの招待

**Objective:** As an インフラ管理者, I want Authentik グループに新規追加されたユーザーが自動的に対応する Organization へ招待されること, so that オンボーディングの手動操作を排除できる

#### Acceptance Criteria

1. When Authentik グループに新規メンバーが追加されており、かつ当該ユーザーが対応する Vaultwarden Organization のメンバーでない, the RBAC同期Job shall 当該ユーザーを Organization 招待 API で招待する
2. The RBAC同期Job shall 招待時に、招待先ユーザーに対しマッピング設定で指定された権限レベルで対象 Collection へのアクセスを指定する（Collection アクセスは Confirm 後に有効化される）
3. If 招待対象ユーザーのメールアドレスが Vaultwarden 上に存在しない、または招待 API がエラーを返した, then the RBAC同期Job shall 当該ユーザーの招待を失敗として記録し、他のユーザーの同期処理は継続する
4. When 招待済みユーザーが未 Confirm 状態である, the RBAC同期Job shall （Confirm 完了を待たず）マッピング設定に基づく Collection 権限更新 API を送信する（Vaultwarden 側の仕様により、当該権限は管理者 Confirm 完了の瞬間に自動的に有効化されるため、RBAC同期Job は Confirm 完了を検知して再送する処理を行う必要はない）
5. When 招待済みユーザーが未 Confirm 状態（Invite 後に Accept したが管理者がまだ Confirm していない）である場合, the RBAC同期Job shall 当該ユーザーを「Confirm 待ち」として Discord 通知に含める

### Requirement 7: Collection 権限の同期適用

**Objective:** As an インフラ管理者, I want 既存 Organization メンバーの Collection 権限が Authentik グループメンバーシップと一致するよう自動更新されること, so that 権限のドリフトが解消される

#### Acceptance Criteria

1. When Organization メンバーの Collection 権限が マッピング設定の権限レベルと異なる, the RBAC同期Job shall 当該メンバーの Collection 権限を更新APIで修正する（更新APIは未 Confirm の招待済みメンバーにも送信可能。ただし Vaultwarden 側の仕様により Confirm 完了まで権限は有効化されない）
2. The RBAC同期Job shall 同一ユーザーに対する複数 Collection への権限変更を1回のメンバー更新APIリクエストにまとめる
3. The RBAC同期Job shall Collection 権限の更新のみを行い、対象メンバーの Organization 内 type（User/Manager/Admin/Owner の役割区分）を変更しない（サービスアカウントは Admin 権限で稼働するため、type 変更を伴う更新は権限昇格ガードにより失敗する）

### Requirement 8: グループ脱退時の権限剥奪

**Objective:** As an インフラ管理者, I want Authentik グループから脱退したユーザーの Collection 権限が自動的に剥奪されること, so that 不要な権限が残留するリスクを排除できる

#### Acceptance Criteria

1. When Vaultwarden Organization メンバーが、マッピング設定で対応する Authentik グループのメンバーから除外されている, the RBAC同期Job shall 当該ユーザーの対象 Collection への権限を削除する
2. While あるユーザーが複数の Authentik グループに所属しており、そのうち一部のグループのみを脱退した, the RBAC同期Job shall 脱退したグループに対応する Collection 権限のみを削除し、継続して所属しているグループに対応する Collection 権限は維持する
3. The RBAC同期Job shall Collection 権限の削除のみを行い、Organization からのユーザー除名は行わない

### Requirement 9: Dry-run モード

**Objective:** As an インフラ管理者, I want 実際の変更を適用せずに同期内容を事前確認できること, so that 意図しない権限変更を防止できる

#### Acceptance Criteria

1. Where dry-run モードが有効化されている, the RBAC同期Job shall Vaultwarden への招待・権限更新・権限削除APIを呼び出さず、適用予定の変更内容のみを実行ログに出力する
2. While dry-run モードが有効である, the RBAC同期Job shall Authentik および Vaultwarden からのデータ取得と差分計算を通常モードと同様に実行する

### Requirement 10: 実行スケジュールと冪等性

**Objective:** As an インフラ管理者, I want RBAC同期Job が定期的に自動実行され、何度実行しても安全であること, so that 権限ドリフトが継続的に自動修正される

#### Acceptance Criteria

1. The RBAC同期Job shall Kubernetes CronJob として1時間毎に実行される
2. While 直前の実行（定期実行・イベント駆動実行のいずれか）が完了していない, the RBAC同期Job shall 新たな実行を開始しない
3. When 差分が存在しない状態で実行された, the RBAC同期Job shall Vaultwarden に対していかなる変更も発生させずに正常終了する

### Requirement 13: イベント駆動トリガーによる即時同期

**Objective:** As an インフラ管理者, I want ユーザーのログインや属性変更（グループメンバーシップ変更含む）が発生した際に同期処理が即時実行されること, so that 最大1時間の定期実行待ちによる権限反映の遅延をなくせる

#### Acceptance Criteria

1. When Authentik 上でユーザーのログイン、属性変更、またはグループメンバーシップ変更のイベントが発生した, the RBAC同期Job shall 次回の定期実行を待たずに同期処理を即時実行する
2. The RBAC同期Job shall Authentik からのイベント通知を認証済みの呼び出し元からのみ受理し、未認証または不正な呼び出し元からのトリガーは拒否する
3. While イベント駆動による即時実行が進行中である, the RBAC同期Job shall 定期実行や他のイベント駆動実行との同時実行を防止する（Requirement 10.2 の排他制御に従う）
4. If イベント駆動トリガーの呼び出しが同期処理の異常終了やタイムアウトにより失敗した, then the RBAC同期Job shall 当該イベントの取り逃しが次回の定期実行（最大1時間後）で補完されることを前提とし、エラーをログおよびDiscordに記録する

### Requirement 11: 実行ログと Discord 通知

**Objective:** As an インフラ管理者, I want 同期結果を実行ログと Discord 通知で確認できること, so that 権限変更を事後に追跡・検証できる

#### Acceptance Criteria

1. When 同期処理が完了した, the RBAC同期Job shall 招待・権限更新・権限削除・Confirm 待ちが発生したユーザー数とその内容を実行ログに出力する
2. If 同期処理中に1件以上のエラーが発生した, then the RBAC同期Job shall 既存の Discord通知パターン（`steering/tech.md` 記載の Webhook 構成）に従い、エラー内容を Discord に通知する
3. Where Discord通知が設定されている, the RBAC同期Job shall 正常終了時にも変更内容のサマリーを Discord に通知する
4. When 1件以上の招待済みユーザーが未 Confirm 状態である, the RBAC同期Job shall Discord 通知サマリーに Confirm 待ちユーザー数と対象のメールアドレス一覧を含め、管理者が Vaultwarden Web UI で Confirm 操作を行うことを促す

### Requirement 12: 認証情報とシークレット管理

**Objective:** As an インフラ管理者, I want RBAC同期Job が利用する全認証情報が既存のシークレット管理規約に従うこと, so that シークレット漏洩リスクを増やさずに機能を追加できる

#### Acceptance Criteria

1. The RBAC同期Job shall Authentik API トークン、Vaultwarden API クライアント認証情報、Discord Webhook URL を ExternalSecret 経由で Infisical から取得する
2. The RBAC同期Job shall 取得した認証情報をマニフェストやログに平文で出力しない
