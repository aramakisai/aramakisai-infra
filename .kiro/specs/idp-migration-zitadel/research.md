# Research & Design Decisions

## Summary
- **Feature**: `idp-migration-zitadel`
- **Discovery Scope**: Complex Integration(既存authentikからの全面移行、複数RPアプリ・メールサーバー・RBAC同期を含む)
- **Key Findings**:
  - Zitadelは自身がLDAPサーバとして動作する機能を持たない(公式discussionで要望として残るのみ、未実装)。DovecotのLDAP認証継続にはLLDAP翻訳層またはlua+Session API委譲のいずれかが必須。
  - Actions v2はZitadel→外部エンドポイントへのpush型webhookで、`ZITADEL-Signature`ヘッダ(HMAC)による署名検証が可能。vaultwarden-rbac-syncのイベント駆動化はこの機構で実現できる。
  - プロジェクトの「Assert Roles on Authentication」設定により、ロールをOIDC ID Token/Userinfoの`urn:zitadel:iam:org:project:roles`クレームへ配布できる。これがRequirement 8のDiscordロール的フラットRBACの実装手段になる。
  - Zitadel公式Terraform providerは100種類超のresourceを持ち、project/role/application/action(webhook)まで一通りIaC管理可能。
  - Zitadelは環境間データ移行用の公式Admin API(`POST /admin/v1/export`/`POST /admin/v1/import`)を持つ。pg_dump/pg_restoreによるバックエンドDB直接移行は、masterkeyが環境ごとに異なると復号不能になる問題があるため不採用とし、この公式APIへ切り替えた。
  - dig調査により、k3d実測(素のpostgresコンテナ)367MiBは本番CNPG構成(instance-manager・barman WALアーカイブ込み)と乖離があると判明。Requirement 1.5でCNPGベースの実測を追加要件化した。

## Research Log

### Zitadel LDAP Server機能の有無
- **Context**: Requirement 3(Dovecot LDAP認証継続)の実現可能性を確認するため
- **Sources Consulted**: [ZITADEL as LDAP Server (Discussion #1929)](https://github.com/zitadel/zitadel/discussions/1929)、[Configure LDAP as an Identity Provider in ZITADEL](https://zitadel.com/docs/guides/integrate/identity-providers/ldap)
- **Findings**:
  - ZitadelにあるのはLDAPを外部認証ソースとして「消費する」側の機能(`zitadel_idp_ldap`/`zitadel_org_idp_ldap` resource)のみ
  - Zitadel自身がLDAP bindを受け付けるサーバとして動作する機能は存在しない(要望issueのみで未実装)
- **Implications**: Dovecot(mailserver)の認証方式は、(a) LLDAPを認証情報の翻訳層として残すハイブリッド構成、(b) Dovecot lua passdb経由でZitadel Session APIへ認証委譲、のいずれかを設計フェーズで選定する必要がある。GLAuthも標準ではOIDC/REST backendを持たないため(a)(b)いずれの代替にもならない([GLAuth Backends](https://glauth.github.io/docs/backends.html))。

### Zitadel Session API (`POST /v2/sessions`) 経由のパスワード検証委譲
- **Context**: Dovecot lua passdb経由でのオンザフライ認証委譲(案b)の実現可能性検証
- **Sources Consulted**: [Session API | ZITADEL Docs](https://zitadel.com/docs/apis/resources/session_service_v2)、[Access ZITADEL APIs](https://zitadel.com/docs/guides/integrate/zitadel-apis/access-zitadel-apis)
- **Findings**:
  - リクエストは`checks`(パスワードチェック含む)・`metadata`・`challenges`・`userAgent`・`lifetime`を持つ。レスポンスは`sessionId`・`sessionToken`・`details`
  - API呼び出しには`session.write`権限が必要。ドキュメント上はPATとして「IAM_OWNER相当」が例示されているが、実際に必要な最小スコープは実装検証が必要(Design時の未解決点)
  - Actions v2のパスワード変更イベントには平文パスワードが含まれない(ZITADELのイベントソーシング・セキュリティ設計上の制約) — LLDAPへのパスワード同期型アプローチは成立しない
- **Implications**: 案(b)を採用する場合、PATの権限最小化・エスケープ処理の正しさ(JSON injection回避)・ZITADEL API障害時のフェイルモードの3点を設計で明記する必要がある(前spec検討時にGemini提案のコード例で発見した実装バグを踏まえる)

### Actions v2 (webhook) の設定方式
- **Context**: Requirement 4(vaultwarden-rbac-syncのイベント駆動化)の実現方式確認
- **Sources Consulted**: [Using Actions | ZITADEL Docs](https://zitadel.com/docs/guides/integrate/actions/usage)、[ZITADEL Actions v2](https://zitadel.com/docs/concepts/features/actions_v2)、[Actions V2 Regression Issue #12225](https://github.com/zitadel/zitadel/issues/12225)
- **Findings**:
  - `Target`(webhook送信先URL)と`Execution`(発火条件: Requests/Responses/Functions/Events)の2リソースで構成
  - 各リクエストに`ZITADEL-Signature`ヘッダ(HMAC、リクエスト内容+タイムスタンプから計算)が付与され、受信側で署名検証可能
  - Terraform providerに`zitadel_action_target`・`zitadel_action_execution_event`等が存在しIaC管理可能
  - 既知のリグレッション: Event条件のExecutionがAPIエラー・ログイン機能破壊を誘発した事例が2026年に報告されている(#12225)
- **Implications**: vaultwarden-rbac-syncはwebhook受信エンドポイントとして実装し、ZITADEL-Signature検証を必須にする。本番投入前にステージングでEvent条件Executionの安定性を検証する(Requirement 4.3)。

### OIDCロールクレーム配布(Assert Roles on Authentication)
- **Context**: Requirement 8(Discordロール的フラットRBAC)の実装手段確認
- **Sources Consulted**: [Retrieve User Roles in ZITADEL](https://zitadel.com/docs/guides/integrate/retrieve-user-roles)、[Claims in ZITADEL](https://zitadel.com/docs/apis/openidoauth/claims)
- **Findings**:
  - プロジェクトの「Assert Roles on Authentication」を有効にすると、ロール情報がUserinfoエンドポイント・トークンへ配布される
  - ロールクレームは`urn:zitadel:iam:org:project:roles`(単一プロジェクト)または`urn:zitadel:iam:org:project:{projectId}:roles`(複数プロジェクト)
  - 内部実装は`prepareRoles`(スコープからロールプレフィックスを走査しroleAudienceを構築)と`assertRoles`(ユーザーのgrantを走査しUserInfo.Claimsへ反映)
- **Implications**: RPアプリはこのクレームを見るだけでロール判定でき、Zitadel側はproject role(キー・表示名のみ)を定義するだけで済む。細粒度permission行列は導入しない設計方針(Requirement 8)を技術的に裏付ける。

### Zitadel Terraform Provider リソース一覧
- **Context**: Requirement 2.2(OIDC Client/Project/RoleのIaC管理)の具体的な実現手段確認
- **Sources Consulted**: `gh api repos/zitadel/terraform-provider-zitadel/contents/docs/resources`(GitHub API)
- **Findings**: 主要リソースを確認
  - `zitadel_project` / `zitadel_project_role` / `zitadel_project_grant` — プロジェクト・ロール定義
  - `zitadel_application_oidc` / `zitadel_application_api` / `zitadel_application_saml` — RPアプリ向けクライアント定義
  - `zitadel_human_user` / `zitadel_machine_user` / `zitadel_user_grant` — ユーザー・サービスアカウント・ロール紐付け
  - `zitadel_action_target` / `zitadel_action_execution_event` / `zitadel_action_execution_function` — Actions v2(webhook)定義
  - `zitadel_org_idp_ldap` — 外部LDAP連携(Zitadelがconsumeする側)
  - `zitadel_instance_trusted_domain` / `zitadel_domain` — インスタンス/ドメイン設定
- **Implications**: OIDC Client・Project Role・Actions v2 webhookは全てTerraformで宣言的管理できる。招待コード発行(`CreateInviteCode`)自体は一時的なAPI呼び出しでありTerraform管理対象外、運用手順(Requirement 6.2)側で扱う。

### メモリ実測(k3d docker-compose検証)
- **Context**: Requirement 1(メモリ削減)の定量的裏付け
- **Sources Consulted**: 実機検証(`docker stats --no-stream`、公式docker-compose構成で起動)
- **Findings**: アイドル状態で以下を実測
  - zitadel-api: 127.5MiB
  - zitadel-login(Node.js別プロセス): 98.98MiB
  - proxy(Traefik): 67.06MiB
  - postgres: 73.06MiB
  - 合計: 約367MiB
- **Implications**: authentik実測(約800Mi)より明確に軽量(Requirement 1.1充足の裏付け)。ただしlogin UIがNode.js別プロセスで追加メモリを要する点はRequirement 1.4で明記済み。K3sシングルノード(prod-node-1、RAM約7.7Gi)への影響は設計フェーズでlimits設定として確定する。

## Architecture Pattern Evaluation

| Option | Description | Strengths | Risks / Limitations | Notes |
|--------|-------------|-----------|---------------------|-------|
| Zitadel単独 + LLDAP翻訳層 | ZitadelをOIDც Provider、LLDAPをDovecot向けLDAP翻訳層として併存 | Dovecot設定変更が最小限、前spec資産(LLDAP bootstrap)を一部再利用可 | パスワードの二重管理は不可のため、LLDAP側パスワードは初期招待時のみ同期し以降は不整合が生じうる | 一般IMAPクライアントの継続的なパスワード変更に追従できない恒常的な設計上の穴が残る |
| Zitadel単独 + Dovecot lua委譲 | LLDAPを廃止し、Dovecot lua passdbからZitadel Session APIへ直接問い合わせ | パスワードの二重管理が発生しない、構成要素が単純化する | 実装コストが高い(lua script保守)、ZITADEL API障害がメール認証障害に直結する単一障害点化 | PAT権限最小化・エスケープ処理・フェイルモードの3点を設計で詰める前提 |
| Zitadel + GLAuthプロキシ | GLAuthをLDAP-OIDC変換層として追加 | (期待していたが)標準機能では不成立 | GLAuthの標準backendはconfig/ldap-proxy/owncloudのみでOIDC/REST backendがなく、カスタムbackend自作が必要でLLDAP同等のコストがかかる | 不採用。詳細はDovecot認証方式の研究ログ参照 |

## Design Decisions

### Decision: Dovecot認証方式はlua + Session API委譲を正式採用する
- **Context**: Requirement 3が翻訳層/委譲方式いずれかの選定を求めていた。validate-design(1回目)およびdig調査で、LLDAP翻訳層案の前提(招待完了時にLLDAPへパスワードを同期する)が技術的に成立しないことが判明した
- **Alternatives Considered**:
  1. LLDAP翻訳層として存続 — 招待完了時の一度きり同期を想定していたが、招待コード(`CreateInviteCode`/`VerifyInviteCode`)とパスワードリセットコードは別API体系であり、Zitadel標準の招待UIでユーザーがパスワードを設定してもその平文を捕捉するコンポーネントが存在しないため、同期の起点自体がない。加えて通常ログイン画面には`hidePasswordReset`設定があっても実際には消えない既知の不具合報告があり([Password Reset hidden](https://questions.zitadel.com/m/1347316217127374991))、ユーザーの自由なパスワード変更を防ぐ手立てがない
  2. カスタム招待完了ページを自作しパスワードを両系へpush — Zitadel標準UIを迂回する必要がありスコープ増、事後のパスワードリセット(標準機能)は依然捕捉不可能なため根本解決にならない
  3. Dovecot lua + Session API委譲 — パスワードをZitadelのみで一元管理し、Dovecotは都度問い合わせる。パスワードの二重管理・不整合リスクを構造的に排除する
- **Selected Approach**: 3(lua + Session API委譲)を正式採用。LLDAP翻訳層は新設・継続利用しない
- **Rationale**: 1・2はいずれもZitadel標準のパスワードリセット導線を完全に塞げない限り恒久的な機能不全(パスワード変更後にメール認証不能)を抱える。3は実装・運用コストと可用性リスク(単一障害点化)を伴うが、Requirement 3の目的(メール認証の継続)を構造的に満たせる唯一の選択肢
- **Trade-offs**: 実装コスト増(lua script保守)、Zitadel API障害がメール認証障害に直結する可用性リスクを受け入れる。フェイルモード設計(一時エラーとして扱う)で影響を限定する
- **Follow-up**: PAT最小権限スコープの実機確認(IAM_OWNER相当が本当に必要か)をタスクの初期段階で検証する

### Decision: vaultwarden-rbac-syncのActions v2安定性検証はk3dのみで行う
- **Context**: Requirement 4.3(本番投入前の安定性検証)の実施環境について、staging namespaceがprod-node-1と同一ノード上にあり、Zitadel一時デプロイがメモリオーバーコミット(本移行の目的)を悪化させる矛盾が指摘された(dig調査)
- **Alternatives Considered**:
  1. staging Zitadelを一時デプロイし検証後撤去 — 本番同等の信号が得られるが、検証期間中に同一ノードのオーバーコミットが悪化する
  2. k3d等の使い捨て検証環境のみで検証 — 信号はやや弱いが本番ノードへの影響がない
- **Selected Approach**: 2(k3dのみ)を採用
- **Rationale**: 本移行の目的自体がメモリオーバーコミット解消であり、検証のために一時的とはいえ同じ問題を悪化させるのは本末転倒
- **Trade-offs**: k3d環境はprodと完全同一ではないため、Actions v2の安定性検証としての信号強度はstaging実施よりやや弱い

### Decision: Terraformプロバイダー認証はAnsible例外ブートストラップで対応する
- **Context**: TerraformでZitadelのproject/role/applicationを宣言的管理するには、Zitadel自体が起動し管理者PAT/Service User Tokenが発行済みである必要がある(鶏と卵問題、dig調査で指摘)
- **Alternatives Considered**:
  1. Ansible手順で例外的にブートストラップ(`infisical-auth`と同一パターン) — 既存の運用パターンを踏襲でき理解しやすい
  2. ArgoCD PostSync Bootstrap Jobで自動化 — GitOps内で完結するが、kubectl exec相当の複雑さを伴う
- **Selected Approach**: 1(Ansible例外ブートストラップ)を採用
- **Rationale**: `infisical-auth` Secret作成という既存の前例があり、運用者にとって一貫した理解しやすいパターンになる

### Decision: k3d→本番のデータ移行はZitadel公式Admin API(export/import)を採用しpg_dump/pg_restoreを撤回する
- **Context**: 前バージョンのdesign.mdはk3d Zitadelのpostgresをpg_dumpし本番CNPGクラスタへpg_restoreする方式を採用していたが、dig調査でmasterkey不一致問題が発覚した。Zitadelのmasterkeyは環境ごとに独立して生成されるべきものであり(公式ドキュメント・コミュニティ回答で明言)、pg_dumpされた暗号化データを別のmasterkeyを持つ環境へpg_restoreすると復号できず、静かに壊れるか壊滅的に失敗する
- **Alternatives Considered**:
  1. pg_dump/pg_restoreを維持しmasterkeyをk3d/本番で固定ピン留めする — 復元は保証されるが、masterkeyを`infisical-auth`同様のライフサイクル管理対象にする追加負担が生じる
  2. pg_dump/pg_restore自体をやめ、Terraform管理外のUI操作設定を本番で手動再現する — masterkeyの罠を回避できるが、UI専用設定の洗い出しと手動再現作業が別途必要
  3. Zitadel公式Admin API(`POST /admin/v1/export`/`POST /admin/v1/import`)を使う — 組織・ユーザー・プロジェクト・アプリケーション・ロール・IDP設定をアプリケーションレイヤーのデータとして移行するため、DB暗号化キー(masterkey)に依存しない
- **Selected Approach**: 3(Zitadel公式Export/Import API)を採用
- **Rationale**: masterkeyの罠を構造的に回避でき、かつUI専用設定も含めて公式にサポートされた手段で一括移行できる。手動再現(案2)より作業量が少なく、masterkey固定(案1)より運用上のシークレット管理負担が少ない
- **Trade-offs**: export/importの対象範囲(withPasswords/withOtp等)を事前に精査する必要があり、パスワードハッシュを含めるかどうかは招待ベース移行(Requirement 6)の方針と重複しないよう調整が必要
- **Follow-up**: 実装時にexport対象からk3d検証用テストユーザーを除外する具体的なフィルタ(`excludedOrgIds`等)を確定する

## Risks & Mitigations
- Actions v2のEvent条件Executionが2026年時点でリグレッション報告あり(#12225) — k3d検証環境での安定性検証を必須プロセスとして組み込む(Requirement 4.3)。不安定と判明した場合はvaultwarden-rbac-syncのイベント駆動化自体をスコープ除外してよい(Requirement 4.4)
- Zitadel Session APIのPAT権限が「IAM_OWNER相当」前提でドキュメント化されている — 実際に絞り込める最小スコープを実装前に検証しないと、メール認証コンポーネントが過大な権限を持つリスクがある
- login UI(Node.js)を含めた実メモリがK3sシングルノードのオーバーコミットに影響する可能性 — 素のpostgresコンテナでなくCNPGベースで実測し直し、Requirement 1.5の基準値と照合する
- Dovecot Lua Auth Bridgeの実装がZitadel API障害時にメール認証の単一障害点になる — フェイルモード(一時エラー扱い)を明確に実装し監視を強化する
- Zitadel Export/Import APIのwithPasswords/withOtpオプションを誤って有効化すると、招待ベース移行(Requirement 6)の設計と矛盾する形で認証情報が持ち込まれるリスクがある — 実装時に明示的にオプションを無効化し、ユーザーは招待コードで移行する方針を維持する

## References
- [ZITADEL as LDAP Server (未実装)](https://github.com/zitadel/zitadel/discussions/1929) — LDAPサーバ機能非対応の根拠
- [Configure LDAP as an Identity Provider in ZITADEL](https://zitadel.com/docs/guides/integrate/identity-providers/ldap) — Zitadelが持つLDAP関連機能の実態
- [Session API | ZITADEL Docs](https://zitadel.com/docs/apis/resources/session_service_v2) — Session作成APIの仕様
- [Using Actions | ZITADEL Docs](https://zitadel.com/docs/guides/integrate/actions/usage) — Actions v2のTarget/Execution設定
- [Actions V2 Regression Issue #12225](https://github.com/zitadel/zitadel/issues/12225) — 既知のリグレッション事例
- [Retrieve User Roles in ZITADEL](https://zitadel.com/docs/guides/integrate/retrieve-user-roles) — ロールクレーム配布設定
- [terraform-provider-zitadel docs/resources](https://github.com/zitadel/terraform-provider-zitadel/tree/main/docs/resources) — 利用可能なTerraform resource一覧
- [GLAuth Backends](https://glauth.github.io/docs/backends.html) — GLAuth不採用の根拠
- [Resend an invite code for a user](https://zitadel.com/docs/apis/resources/user_service_v2/user-service-resend-invite-code) — 招待コードが初回セットアップ専用でありパスワードリセットとは別体系である根拠
- [Password Reset hidden (questions.zitadel.com)](https://questions.zitadel.com/m/1347316217127374991) — `hidePasswordReset`設定の既知の不具合報告、LLDAP翻訳層案不採用の決め手
- [ExportData | ZITADEL Docs](https://zitadel.com/docs/apis/resources/admin/admin-service-export-data) — 環境間データ移行の公式Export API仕様
- [ImportData | ZITADEL Docs](https://zitadel.com/docs/reference/api/admin/zitadel.admin.v1.AdminService.ImportData) — 環境間データ移行の公式Import API仕様
- [Restoring backup DB doesn't work (questions.zitadel.com)](https://questions.zitadel.com/m/1328367071301734492) — pg_dump/pg_restoreとmasterkey不一致問題の実例
