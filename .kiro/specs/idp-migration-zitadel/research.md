# Research & Design Decisions

## Summary
- **Feature**: `idp-migration-zitadel`
- **Discovery Scope**: Complex Integration(既存authentikからの全面移行、複数RPアプリ・メールサーバー・RBAC同期を含む)
- **Key Findings**:
  - Zitadelは自身がLDAPサーバとして動作する機能を持たない(公式discussionで要望として残るのみ、未実装)。DovecotのLDAP認証継続にはLLDAP翻訳層またはlua+Session API委譲のいずれかが必須。
  - Actions v2はZitadel→外部エンドポイントへのpush型webhookで、`ZITADEL-Signature`ヘッダ(HMAC)による署名検証が可能。vaultwarden-rbac-syncのイベント駆動化はこの機構で実現できる。
  - プロジェクトの「Assert Roles on Authentication」設定により、ロールをOIDC ID Token/Userinfoの`urn:zitadel:iam:org:project:roles`クレームへ配布できる。これがRequirement 8のDiscordロール的フラットRBACの実装手段になる。
  - Zitadel公式Terraform providerは100種類超のresourceを持ち、project/role/application/action(webhook)まで一通りIaC管理可能。

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

### Decision: Dovecot認証方式は設計フェーズで最終選定する(本research時点では両論併記)
- **Context**: Requirement 3が翻訳層/委譲方式いずれかの選定を求めている
- **Alternatives Considered**:
  1. LLDAP翻訳層として存続 — 実装コスト低いが恒常的なパスワード不整合リスクを抱える
  2. Dovecot lua + Session API委譲 — パスワード一元管理を実現するが実装・運用コストと可用性リスクが増す
- **Selected Approach**: 未確定。design.md執筆時に、Roundcube(OAuth2 passdb、既存踏襲)と一般IMAP/POP3クライアント(上記2択)を分けて明記する
- **Rationale**: 前spec調査時点でも技術的トレードオフが拮抗しており、実機検証(PAT最小権限スコープの確認、lua実装の可用性テスト)なしに断定すべきでない
- **Trade-offs**: 翻訳層維持は安全側(既存踏襲)、委譲方式はゴールに近いが実装リスクを負う
- **Follow-up**: 設計フェーズでPAT最小スコープの実機確認、lua auth実装のPoCをタスク化する

## Risks & Mitigations
- Actions v2のEvent条件Executionが2026年時点でリグレッション報告あり(#12225) — ステージングでの安定性検証を必須プロセスとして組み込む(Requirement 4.3)
- Zitadel Session APIのPAT権限が「IAM_OWNER相当」前提でドキュメント化されている — 実際に絞り込める最小スコープを実装前に検証しないと、メール認証コンポーネントが過大な権限を持つリスクがある
- login UI(Node.js)を含めた実メモリがK3sシングルノードのオーバーコミットに影響する可能性 — 設計フェーズでlimits/requestsを確定し、Requirement 1.3の基準値と照合する

## References
- [ZITADEL as LDAP Server (未実装)](https://github.com/zitadel/zitadel/discussions/1929) — LDAPサーバ機能非対応の根拠
- [Configure LDAP as an Identity Provider in ZITADEL](https://zitadel.com/docs/guides/integrate/identity-providers/ldap) — Zitadelが持つLDAP関連機能の実態
- [Session API | ZITADEL Docs](https://zitadel.com/docs/apis/resources/session_service_v2) — Session作成APIの仕様
- [Using Actions | ZITADEL Docs](https://zitadel.com/docs/guides/integrate/actions/usage) — Actions v2のTarget/Execution設定
- [Actions V2 Regression Issue #12225](https://github.com/zitadel/zitadel/issues/12225) — 既知のリグレッション事例
- [Retrieve User Roles in ZITADEL](https://zitadel.com/docs/guides/integrate/retrieve-user-roles) — ロールクレーム配布設定
- [terraform-provider-zitadel docs/resources](https://github.com/zitadel/terraform-provider-zitadel/tree/main/docs/resources) — 利用可能なTerraform resource一覧
- [GLAuth Backends](https://glauth.github.io/docs/backends.html) — GLAuth不採用の根拠
