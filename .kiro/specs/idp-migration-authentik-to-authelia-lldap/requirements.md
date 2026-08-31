# Requirements Document

## Project Description (Input)
現行IdP(authentik)をより軽量な代替へ置き換える。

背景:
- prod-node-1(Hetzner シングルノード、RAM容量 約7.7Gi)は現在メモリ81%使用・limits合計112%のオーバーコミット状態。
- authentikはserver/worker合計で実測約800Mi(server 499Mi + worker 302Mi)を消費しており、既に一度チューニング済みでこれ以上絞れない。
- authentikの主要機能: OIDC Provider(Directus/CMS/Vaultwarden-RBAC-sync等のRP。Directusは本spec作成後に撤去されCMSはPayload CMSへ移行済み)、LDAP Outpost(Dovecotのmail=%u形式クエリによるIMAP/SMTP認証委譲)、Discordソーシャルログイン連携(ロール同期・アバター同期・動的グループ判定)。

候補調査結果(5候補を並列調査済み):
- Zitadel: OIDC◎・Terraform provider公式・軽量だが**LDAP Serverとして動作する機能が存在しない**(公式discussionで明言)ため脱落。
- Keycloak: OIDC◎・Terraform provider公式だが実運用メモリが1〜2GB必要(軽量化目的に反する)、かつLDAP Server機能なし(User Federationのみ)のため脱落。
- Casdoor: 軽量(Go実装)だがLDAP実装がcnベース中心でmail属性複合フィルタの裏付けが薄く、Terraform providerも非公式。
- Kanidm: OIDC○・Rust製で軽量期待できるが、LDAP bind後の権限が匿名相当に留まる制約があり、公式Helm chart・Terraform providerともに非公式。
- **Authelia + LLDAP: 最有力**。メモリ合計30〜65MB程度(authentik比で一桁軽量)。LLDAPは素のLDAPv3実装でDovecotのauth_bind方式・mail属性複合ANDフィルタ検索に対応することをローカルDocker PoCで実機確認済み(objectClass=inetOrgPerson標準搭載、カスタム属性追加可能、readonlyサービスアカウントでの検索・auth bind双方成功)。弱点はDynamic Client Registration未対応(OIDCクライアント登録はYAML静的定義)と2コンポーネント構成による運用複雑化。

スコープ除外の決定:
- Discordロール同期・アバター同期・動的グループ判定(authentik Expression Policyによるログイン時のDiscord API連携)は、どの候補IdPでもネイティブ機能で再現不可なため移行スコープから除外する。
- 上記に依存していたメーリングリストACL関連の属性(mailAclGroups)生成も、Discord連携の道連れとして自動同期をやめ、手動グループ管理に切り替える。
- Discordソーシャルログイン自体(単純なOAuth2ログイン)は移行先での対応可否を別途検討してよい。

移行に伴う既知の追加作業:
- Dovecot(mailserver)のLDAP接続先をauthentik-ldap-outpostからLLDAPへ切り替え、カスタム属性名(ak-active等)をLLDAPの命名制約(アンダースコア不可、ハイフンのみ)に合わせて調整する。
- vaultwarden-rbac-syncがauthentik REST APIに依存しているため、LLDAP GraphQL API(またはAuthelia側)向けに書き換える。
- CMS等の既存OIDC RP設定をAuthelia側のクライアント定義(YAML)に移行する(Directusは本spec作成後に撤去済み)。
- 実行委員グループ判定・メーリングリストACLグループ運用を手動管理フローとして再設計する。

## Requirements

### Requirement 1: IdP基盤のメモリ使用量削減
**Objective:** As a インフラ運用担当者, I want 認証基盤のメモリフットプリントを大幅に削減する, so that シングルノードクラスタのメモリオーバーコミットを解消し他サービスへの影響を防げる

#### Acceptance Criteria
1. While 新IdP基盤が稼働している, the 新IdP基盤 shall 合計実メモリ使用量をauthentik実測値(server 499Mi + worker 302Mi ≒ 800Mi)より明確に少ない値に維持する
2. The 新IdP基盤 shall Kubernetes上でHelm chartまたは宣言的manifestとしてデプロイ可能である
3. When 移行完了後にprod-node-1のリソース使用量を確認する, the インフラ shall memory limits合計のオーバーコミット率を移行前(112%)より低い値にする

### Requirement 2: OIDC Provider機能の継続
**Objective:** As a 各アプリケーション運用者, I want 既存のOIDC連携アプリ(CMS/Vaultwarden/Roundcube等)が新IdPでも認証できる, so that 業務アプリのログイン機能を移行後も維持できる

#### Acceptance Criteria
1. When 既存OIDC RPアプリケーションが認証を要求する, the 新IdP shall Authorization Code Flow + PKCEによるOIDC認証を提供する
2. The 新IdP shall 各RPアプリに対応するOIDC Clientの設定をコード(IaC)で管理可能にする
3. If 新IdPが新規OIDC Client登録を要求する, then 移行手順 shall 対象アプリ側のclient_id/secret/redirect_uri設定変更を含む

### Requirement 3: LDAP認証(メールサーバー)の継続
**Objective:** As a メールサーバー運用者, I want DovecotのIMAP/SMTP認証が新IdPのLDAP互換層で機能し続ける, so that 既存のメール利用者が移行後も引き続きログインできる

#### Acceptance Criteria
1. When Dovecotが`mail=%u`形式のuserdb/passdbクエリを発行する, the LDAP互換サーバー shall 該当ユーザーエントリを正しく返す
2. While mailserver-service相当のサービスアカウントでLDAP bindしている, the LDAP互換サーバー shall `objectClass=inetOrgPerson`を含む複合ANDフィルタでの検索に対応する
3. When ユーザーが実パスワードでauth bindを試行する, the LDAP互換サーバー shall 正しいパスワードでのbind成功、誤ったパスワードでのbind拒否をそれぞれ返す
4. The 移行手順 shall 既存authentikカスタム属性(`ak-active`, `mailListAddress`等)をLDAP互換サーバーの命名制約に適合させた形で再現する

### Requirement 4: Vaultwardenグループ権限同期の継続
**Objective:** As a Vaultwarden RBAC運用担当者, I want vaultwarden-rbac-syncが新IdPのグループ情報を取得できる, so that グループ⇔Vaultwarden Collection権限の自動同期を移行後も維持できる

#### Acceptance Criteria
1. When vaultwarden-rbac-syncがグループ・メンバー情報を要求する, the 新IdP shall REST/GraphQL API経由でグループメンバーシップを提供する
2. The vaultwarden-rbac-sync shall authentik固有APIへの依存を除去し新IdPのAPIに置き換えられている

### Requirement 5: Discord連携のスコープ縮小と手動運用への移行
**Objective:** As a 実行委員会運営担当者, I want Discordロール同期を廃止しグループ管理を手動運用に切り替える, so that 新IdPへの移行を過度な追加開発なしに実現できる

#### Acceptance Criteria
1. The 新IdP基盤 shall Discordロールの自動同期・アバター自動取得・ログイン時の動的グループ判定を実装しない
2. Where Discordソーシャルログイン(単純なOAuth2ログインのみ)を提供する場合, the 新IdP shall 標準的なOAuth2 Source機能で対応する
3. The 移行手順 shall `executive`グループおよび`mailAclGroups`相当のメーリングリストACLグループの手動管理手順を運用ドキュメントとして提供する

### Requirement 6: 既存ユーザー・グループデータの移行
**Objective:** As a インフラ運用担当者, I want 既存authentikに登録済みの全ユーザー・グループを新IdPへ移行する, so that 移行後もサービス利用者がアカウント(所属グループ・権限)を失わずログインできる

#### Acceptance Criteria
1. When 移行が実行される, the 移行手順 shall 既存authentikの全ユーザー(email・グループ所属含む)を新IdPへ引き継ぐ
2. If 移行後にあるユーザーが新IdPへログインできない, then 移行手順 shall 原因調査手順とロールバック手順を提供する
3. LLDAPはauthentikのパスワードハッシュを移植できないため、移行手順 shall 全ユーザーに対し新IdPでの初回パスワードリセットを前提として設計し、移行実行と同じタイミングでリセット案内を送付する

### Requirement 7: 段階的カットオーバーとロールバック
**Objective:** As a インフラ運用担当者, I want IdP切替を安全に実施しリスクを最小化する, so that 実行委員会の業務アプリに障害を起こさず移行できる

#### Acceptance Criteria
1. While 移行作業中に一部アプリが旧authentikを参照している, the 移行手順 shall 新旧並行稼働可能な切替順序を定義する
2. If 新IdPへの切替後に重大な認証障害が発生する, then 移行手順 shall 旧authentik構成への切り戻し手順を含む
3. The 移行手順 shall GitOps原則(`gitops/`配下のマニフェスト変更→ArgoCD sync)に従い、クラスタへの直接kubectl操作を行わない

## Boundary Context
- **In scope**: OIDC Provider機能移行、LDAP認証(Dovecot連携)移行、vaultwarden-rbac-syncのAPI依存先切り替え、既存ユーザー・グループデータの移行、段階的カットオーバー手順、Discordソーシャルログイン(単純ログインのみ)の対応可否検討
- **Out of scope**: Discordロール自動同期・アバター自動取得・ログイン時動的グループ判定の再実装、新規認証機能の追加、mailAclGroupsの自動生成ロジック維持(手動管理へ移行)
- **Adjacent expectations**: mailserver(DMS)のLDAP接続先変更、CMS/Roundcube/Vaultwarden等各アプリのOIDC Client設定変更は本specの実施範囲に含むが、各アプリ内部のビジネスロジック変更は含まない。IdP製品の最終選定(Authelia+LLDAP等)は設計フェーズで確定する。
