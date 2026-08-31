## Summary
- **Feature**: `idp-migration`
- **Discovery Scope**: Complex Integration(既存IdP基盤の置き換え、複数RP・LDAPクライアント・自作同期スクリプトが関与)
- **Key Findings**:
  - Authelia + LLDAPの2コンポーネント構成が唯一、OIDC/LDAP両要件とメモリ削減目的を同時に満たす(他候補はいずれかで脱落済み、詳細はrequirements.md参照)。
  - vaultwarden-rbac-syncの`AuthentikGroupClient`はグループ名→メンバーemail一覧を返す薄いインターフェースのみで`SyncOrchestrator`から呼ばれており、LLDAP GraphQL APIへの差し替えはこのクラス1つの置換で完結する。
  - Roundcube向けDovecot OAuth2連携(`mail_acl_groups`スコープ、`userdb_acl_groups`クレーム)はauthentik Expression Policyによるログイン時動的計算に依存しており、Authelia claims_policiesでは静的なLDAP属性のexposeしかできない。動的計算ロジックは要件5(Discord連携縮小)の決定に準じ、手動更新の属性値に置き換える。
  - Authelia OIDC Providerはstorage backendにPostgreSQLも使えるが、シングルノード・複数レプリカ不要の本構成ではSQLite+PVCで十分でCNPG依存を増やす理由がない。

## Research Log

### Authelia OIDC Client設定スキーマ
- **Context**: 既存Directus/CMS/Roundcube/Vaultwarden/ArgoCD/Cloudflare Access/room-presenceのOIDC Provider設定をAuthelia側にどう表現するか確認するため。
- **Sources Consulted**: [Authelia OpenID Connect 1.0 Clients](https://www.authelia.com/configuration/identity-providers/openid-connect/clients/)
- **Findings**:
  - `identity_providers.oidc.clients[]`にclient単位でYAML定義。`client_id`, `client_secret`, `public`, `redirect_uris`, `scopes`, `grant_types`, `response_types`, `token_endpoint_auth_method`が主要項目。
  - Dynamic Client Registration(RFC7591/7592)は未実装のため、新規RP追加は必ずこの静的YAML編集+Authelia再起動を伴う。
- **Implications**: 既存`terraform/authentik_apps.tf`の`authentik_provider_oauth2.*`各リソースを、Authelia向けの静的YAML(ConfigMap/Secretの組み合わせ)へ1:1で変換する移行表が必要。TerraformでAuthentikを管理していたIaC性はここで失われる(Authelia用Terraform providerが存在しないため、gitops manifestとしてYAMLを直接管理する運用に切り替える)。

### Authelia OIDC Claims Policies とLDAPカスタム属性
- **Context**: authentikの`mail_acl_groups`スコープ(`userdb_acl_groups`クレームをDovecot oauth2 passdbへ渡す)がPythonのExpression Policyでログイン時に動的計算されている。Authelia側で同等機能があるか確認。
- **Sources Consulted**: [OpenID Connect 1.0 Claims | Authelia](https://www.authelia.com/integration/openid-connect/openid-connect-1.0-claims/), [Design: Attribute Mapping #2868](https://github.com/authelia/authelia/issues/2868), [Discussion #8621](https://github.com/authelia/authelia/discussions/8621), [Discussion #9076](https://github.com/authelia/authelia/discussions/9076)
- **Findings**:
  - Authelia 4.39+で`claims_policies`機能があり、LDAP認証バックエンドの`extra_attributes`(LLDAPのカスタム属性)を静的にOIDCクレームとしてexposeできる。
  - ただしこれは「格納済みの属性値をそのまま返す」だけで、authentikのようにログイン時にグループ集合演算(`{g.attributes.get("mailAclSlug") for g in user.groups}`)を実行する機能はない。
- **Implications**: `mailAclGroups`相当の値は、LLDAPユーザーのカスタム属性として**事前に手動更新**しておく必要がある。requirements.md Requirement 5.3で決定済みの「メーリングリストACLグループの手動管理」の実装詳細としてこれに合致する。ログイン時の動的差分計算ロジックは実装しない。

### Authelia Storage Backend
- **Context**: Authelia OIDC Provider・セッション・認可コード等の永続化先をどう構成するか。
- **Sources Consulted**: [Authelia PostgreSQL Storage](https://www.authelia.com/configuration/storage/postgres/), [Authelia SQLite Storage](https://www.authelia.com/configuration/storage/sqlite/)
- **Findings**: PostgreSQLはマルチレプリカ・フェイルオーバー向け。SQLiteはファイルベースで単一インスタンス運用に十分。
- **Implications**: prod-node-1はシングルノード・Authelia replicas=1が前提のため、SQLite+PVC(既存mailserver PVCパターンと同様)を採用しCNPG Clusterを新設しない。これはメモリ・運用コスト双方の観点でRequirement 1(メモリ削減)に資する。

### LLDAP GraphQL API(グループメンバーシップ取得)
- **Context**: vaultwarden-rbac-syncの`AuthentikGroupClient.get_group_members`(`/api/v3/core/groups/?name=<name>&include_users=true`)をLLDAP向けにどう置き換えるか。
- **Sources Consulted**: ローカルDocker PoCでのGraphQL introspection実施済み(前回セッション)。`query{groups{id displayName}}`、`addUserToGroup`等のmutation/queryを実機確認済み。
- **Findings**: LLDAPのGraphQL schemaは`Group`型が`users`フィールドを持ち、グループ名から所属ユーザー一覧(email含む)を1クエリで取得可能。
- **Implications**: `AuthentikGroupClient`を`LldapGroupClient`に置き換え、Bearer Token認証のREST呼び出しをGraphQL POSTリクエストに変える。`GroupMembersResult`という戻り値の型は変更不要なため、`SyncOrchestrator`・`PermissionDiffEngine`への影響はゼロ。

### 既存OIDC RP一覧とプロパティマッピングの棚卸し
- **Context**: `terraform/authentik_apps.tf`を読み、移行が必要なOIDC Providerの全量とカスタムscope依存を洗い出す。
- **Sources Consulted**: `terraform/authentik_apps.tf`(Roundcube, ArgoCD, Cloudflare Access, Vaultwarden, room_presence, directus_prod, directus_stg, cms_prod の8 Provider)
- **Findings**:
  - 標準的な`openid`/`profile`/`email`/`groups`スコープのみで足りるRP: ArgoCD, Cloudflare Access, Vaultwarden, room_presence, directus_prod, directus_stg, cms_prod。
  - Roundcubeのみカスタム`email`スコープ(部署メール優先ルーティング)と`mail_acl_groups`スコープに依存。
- **Implications**: 8 Providerのうち7つは移行表(client_id/redirect_uri/scopeの機械的な書き写し)で完結する。Roundcubeのみ「LDAP属性の手動整備」という追加作業が必要(上記Claims Policies調査を参照)。

## Architecture Pattern Evaluation

| Option | Description | Strengths | Risks / Limitations | Notes |
|--------|-------------|-----------|---------------------|-------|
| 単一コンポーネント代替(Zitadel/Keycloak/Kanidm/Casdoor) | 1バイナリでOIDC+LDAPを賄う | 運用コンポーネント数が少ない | いずれもLDAP Server機能欠如・メモリ超過・LDAP権限モデルの制約のいずれかで要件未達(requirements.md参照) | 却下 |
| Authelia + LLDAP(採用) | OIDC ProviderとLDAPユーザーストアを分離 | 双方PoC/文献で要件充足を確認済み、合計メモリが桁違いに軽量 | 2コンポーネント運用、Dynamic Client Registration非対応 | 採用 |

## Design Decisions

### Decision: IdP基盤をAuthelia(OIDC)+LLDAP(LDAP)の2コンポーネントに分離する
- **Context**: authentikは単一プロセスでOIDC/LDAP/ソーシャルログイン/ポリシーエンジンを兼ねるが、これがメモリ肥大化の一因かつ移行先候補の中でこの一体型を維持できる軽量な選択肢がなかった。
- **Alternatives Considered**:
  1. Zitadel/Keycloak/Casdoor/Kanidm単体 — いずれかの必須要件(LDAP Server機能、メモリ、属性権限モデル)を満たせず却下(requirements.md参照)。
  2. authentikを維持しリソース設定のみ再チューニング — 既に一度チューニング済みで実測値からこれ以上の削減余地がない(spec起票時点の実測: server 499Mi + worker 302Mi)。
- **Selected Approach**: Authelia(OIDC Provider、SQLite storage)+ LLDAP(LDAPユーザーストア、Authelia LDAP authentication backend経由で接続)を新規デプロイし、既存OIDC RP・Dovecotの接続先を順次切り替える。
- **Rationale**: PoCで実機確認済みの複合ANDフィルタ検索・auth bind方式によりDovecot要件を無改造で満たせる。メモリはauthentik比で一桁軽量。
- **Trade-offs**: コンポーネント数が1→2に増える。Dynamic Client Registration非対応のためRP追加のたびに静的YAML編集が要る(現状のRP数は8で運用可能な規模)。
- **Follow-up**: LLDAPのバックアップ方式(SQLiteファイルのVolSync対象化)を実装フェーズで具体化する。

### Decision: mailAclGroups相当の値はLLDAP属性への手動更新に置き換える
- **Context**: Roundcube向けDovecot ACL連携がauthentikのログイン時Python動的計算に依存しており、Authelia claims_policiesでは同等の動的計算ができない。
- **Alternatives Considered**:
  1. 動的計算を担う外部同期サービスを新規実装 — requirements.md Requirement 5で明示的にスコープ除外済み(Discordロール同期と同様の理由で過剰投資と判断)。
  2. LLDAP属性への手動更新(採用) — グループ変更頻度が低い学生団体運用に対して現実的なコスト。
- **Selected Approach**: `vaultwarden-rbac.md`と同様の運用ドキュメントを整備し、グループ変更時に運営担当者がLLDAP属性(`mail-acl-groups`相当)を手動更新する。
- **Rationale**: 要件5の決定と整合し、追加のカスタム同期サービスを増やさない。
- **Trade-offs**: リアルタイム性を失う(グループ変更が即座にACLへ反映されない)。運用手順のドキュメント化と周知が必須。
- **Follow-up**: 手動更新手順を実装フェーズでrunbook化する。

### ローカルK3s検証環境ツールの選定
- **Context**: 本番投入前に実装手順を確立するため、ローカルで使い捨てK3sクラスタを立てる方式を検討。
- **Sources Consulted**: k3d(Docker上でK3sノードを起動)、kind(Docker上でkubeadmベースの標準k8sを起動)の比較。
- **Findings**: 本番prod-node-1はK3sディストリビューションのため、k3dの方がAPIサーバーのデフォルト無効化コンポーネント(Traefik等)やaddon構成を含め本番との差異が小さい。kindは標準k8sのためK3s固有の挙動(組み込みregistry、`--disable`フラグ挙動等)を再現しない。
- **Implications**: Phase 0の検証環境はk3dを採用する(design.md Migration Strategy参照)。

## Risks & Mitigations
- Dynamic Client Registration非対応によるRP追加時の手作業増 — 静的YAMLをGitOps管理下に置きレビュー・変更履歴で運用性を確保する。
- LLDAPカスタム属性名の制約(アンダースコア不可)によるDovecotフィルタ式の書き換え漏れ — 移行時のfilter式一覧を設計書に明記し、変更差分をコードレビューで確認する。
- mailAclGroups手動更新の失念によるACL反映漏れ — オンボーディング/オフボーディング運用手順書(`vaultwarden-rbac.md`相当)にLLDAP属性更新ステップを追加する。
- Authelia SQLite storageの単一障害点化 — 既存mailserver PVCと同様にVolSyncバックアップ対象に含める。

## References
- [Authelia OpenID Connect 1.0 Clients](https://www.authelia.com/configuration/identity-providers/openid-connect/clients/) — OIDC Client設定スキーマ
- [Authelia OpenID Connect 1.0 Claims](https://www.authelia.com/integration/openid-connect/openid-connect-1.0-claims/) — claims_policies仕様
- [Authelia PostgreSQL Storage](https://www.authelia.com/configuration/storage/postgres/) / [SQLite Storage](https://www.authelia.com/configuration/storage/sqlite/) — storage backend比較
- [Authelia Helm Chart Repository](https://charts.authelia.com/) — 公式Helm chart
- [LLDAP GitHub](https://github.com/lldap/lldap) / [lldap/charts](https://github.com/lldap/charts) — LLDAP本体・公式Helm chart
- 前回セッションのローカルDocker PoC(objectClass=inetOrgPerson複合ANDフィルタ検索・auth bind・GraphQL API実機確認、本ファイル作成時点でコンテナは既に破棄済み)
