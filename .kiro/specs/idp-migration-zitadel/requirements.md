# Requirements Document

## Project Description (Input)
現行IdP(authentik)をZitadelへ置き換える。

背景:
- prod-node-1(Hetzner シングルノード、RAM容量 約7.7Gi)は現在メモリ81%使用・limits合計112%のオーバーコミット状態。
- authentikはserver/worker合計で実測約800Mi(server 499Mi + worker 302Mi)を消費しており、既に一度チューニング済みでこれ以上絞れない。
- authentikの主要機能: OIDC Provider(CMS/Vaultwarden-RBAC-sync等のRP)、LDAP Outpost(Dovecotのmail=%u形式クエリによるIMAP/SMTP認証委譲)、Discordソーシャルログイン連携(ロール同期・アバター同期・動的グループ判定)。
- 前身spec [[idp-migration-authentik-to-authelia-lldap]] でAuthelia+LLDAP構成を実装・k3d検証環境まで構築し実機動作を確認したが、以下の理由で不採用としZitadelへ切り替える:
  - 管理UIの使いやすさ: LLDAPの管理UIはユーザー・グループ管理に限定され、Autheliaにはユーザー管理UI自体がない。Zitadelは単一管理コンソールでユーザー/グループ/ロール/OIDCクライアントを一元管理でき、運用者体験が大きく上回る。
  - 実装の簡潔さ: Autheliaのclaims_policies/custom_claims/scopesは、LDAP拡張属性名(ハイフン必須)とCEL変数名(ハイフン不可)の不一致など、ドキュメント上の記載が薄く実装時に複数のバグを踏んだ(configmap.yaml参照)。Zitadelは招待・OIDCクライアント管理・ロール管理が単一プロダクト内で完結し実装面の複雑さが少ない。
  - 機能ギャップの解消: authentikにあってAuthelia+LLDAPになかった「招待制オンボーディング」「イベント駆動RBAC即時同期」の2点を、Zitadelは標準機能(招待コードAPI、Actions v2 webhook)でほぼカバーできる。
  - リソース効率: k3d実測でZitadel一式(api/login/proxy/postgres 4コンテナ)合計約367MiB。Authelia+LLDAP(実測30〜65MB)より重いが、authentik実測800Miより明確に軽く、機能面のメリットとのトレードオフでZitadel優位と判断。

候補比較の要点(前spec調査結果 + 今回の追加検証):
- Zitadel: OIDC◎・Terraform provider公式・招待/イベント駆動webhookあり・管理UI良好。弱点はLDAPサーバとして動作する機能が存在しない(公式discussionで明言)ことと、login UIがNode.js別プロセスで追加メモリを要すること。
- Authelia + LLDAP: メモリは最軽量だが、ユーザー管理UI・招待オンボーディング・イベント駆動同期のいずれも欠く。前spec [[idp-migration-authentik-to-authelia-lldap]] で不採用済み。
- GLAuth: LDAPプロキシとしては軽量だが標準backendがconfig/file・ldap-proxy・owncloudのみでOIDC/REST委譲のbackendを持たず、今回の課題(Zitadel⇔LDAP間のパスワード翻訳)の解決にはならないため不採用。

## Requirements

### Requirement 1: IdP基盤のメモリ使用量削減
**Objective:** As a インフラ運用担当者, I want 認証基盤のメモリフットプリントを削減する, so that シングルノードクラスタのメモリオーバーコミットを解消し他サービスへの影響を防げる

#### Acceptance Criteria
1. While 新IdP基盤が稼働している, the 新IdP基盤 shall 合計実メモリ使用量をauthentik実測値(server 499Mi + worker 302Mi ≒ 800Mi)より明確に少ない値に維持する
2. The 新IdP基盤 shall Kubernetes上でHelm chartまたは宣言的manifestとしてデプロイ可能である
3. When 移行完了後にprod-node-1のリソース使用量を確認する, the インフラ shall memory limits合計のオーバーコミット率を移行前(112%)より低い値にする
4. The 新IdP基盤 shall login UIコンポーネント(Node.jsプロセス)を含めた実測値でRequirement 1.1を満たすことを設計フェーズで検証する

### Requirement 2: OIDC Provider機能の継続
**Objective:** As a 各アプリケーション運用者, I want 既存のOIDC連携アプリ(CMS/Vaultwarden/Roundcube等)が新IdPでも認証できる, so that 業務アプリのログイン機能を移行後も維持できる

#### Acceptance Criteria
1. When 既存OIDC RPアプリケーションが認証を要求する, the 新IdP shall Authorization Code Flow + PKCEによるOIDC認証を提供する
2. The 新IdP shall 各RPアプリに対応するOIDC Client/Project/Roleの設定をZitadel公式Terraform providerでコード(IaC)管理する
3. If 新IdPが新規OIDC Client登録を要求する, then 移行手順 shall 対象アプリ側のclient_id/secret/redirect_uri設定変更を含む

### Requirement 3: LDAP認証(メールサーバー)の継続
**Objective:** As a メールサーバー運用者, I want DovecotのIMAP/SMTP認証が新IdP移行後も機能し続ける, so that 既存のメール利用者が移行後も引き続きログインできる

#### Acceptance Criteria
1. The 新IdP基盤 shall LDAPサーバとして動作しない制約を前提に、Dovecot認証を継続させる構成を設計フェーズで確定する
2. Where Webmail(Roundcube)経由のOAUTHBEARER/XOAUTH2認証を使う場合, the 新IdP shall Dovecotのoauth2 passdb向けにtoken introspectionエンドポイントを提供する
3. Where 一般IMAP/POP3クライアントのLDAP simple bind認証を維持する場合, the 移行手順 shall LLDAPを認証情報の翻訳層として残すハイブリッド構成、またはDovecot lua passdb経由でZitadel Session API(`POST /v2/sessions`)へ認証を委譲する構成のいずれかを比較検討し設計に明記する
4. If Dovecot lua passdb経由の認証委譲を採用する場合, then 設計 shall 以下を満たす実装であること:
   - パスワードをJSONへ埋め込む際は正規のエスケープ処理(文字列連結禁止)を用いる
   - ZitadelのService User/PATに割り当てる権限をSession API呼び出しに必要な最小スコープへ絞る
   - Zitadel API障害時のDovecot認証フェイルモード(タイムアウト・フォールバック挙動)を定義する
5. The 移行手順 shall Zitadel Actions v2のパスワード変更イベントに平文パスワードが含まれない制約を前提とし、LLDAPへのパスワード直接同期に依存しない設計とする

### Requirement 4: Vaultwardenグループ権限同期の継続
**Objective:** As a Vaultwarden RBAC運用担当者, I want vaultwarden-rbac-syncが新IdPのグループ情報をイベント駆動で取得できる, so that グループ⇔Vaultwarden Collection権限の自動同期を移行後も維持できる

#### Acceptance Criteria
1. When ユーザーのグループ/ロール割り当てが変更される, the 新IdP shall Actions v2 webhookで変更イベントを外部エンドポイントへ通知する
2. The vaultwarden-rbac-sync shall authentik固有APIへの依存を除去しZitadel API(Actions v2 webhook受信 + Management/User API問い合わせ)に置き換えられている
3. Before 本番投入する, the 移行手順 shall ステージング環境でActions v2のEvent条件トリガーの安定性を検証する(既知のリグレッション事例: zitadel/zitadel#12225)

### Requirement 5: Discord連携のスコープ縮小と手動運用への移行
**Objective:** As a 実行委員会運営担当者, I want Discordロール同期を廃止しグループ管理を手動運用に切り替える, so that 新IdPへの移行を過度な追加開発なしに実現できる

#### Acceptance Criteria
1. The 新IdP基盤 shall Discordロールの自動同期・アバター自動取得・ログイン時の動的グループ判定を実装しない
2. Where Discordソーシャルログイン(単純なOAuth2ログインのみ)を提供する場合, the 新IdP shall 標準的なOAuth2/OIDC Identity Provider機能で対応する
3. The 移行手順 shall `executive`グループおよび`mailAclGroups`相当のメーリングリストACLグループの手動管理手順を運用ドキュメントとして提供する

### Requirement 6: 既存ユーザー・グループデータの移行
**Objective:** As a インフラ運用担当者, I want 既存authentikに登録済みの全ユーザー・グループを新IdPへ移行する, so that 移行後もサービス利用者がアカウント(所属グループ・権限)を失わずログインできる

#### Acceptance Criteria
1. When 移行が実行される, the 移行手順 shall 既存authentikの全ユーザー(email・グループ所属含む)を新IdPへ引き継ぐ
2. The 移行手順 shall Zitadelの招待コードAPI(`CreateInviteCode`/`ResendInviteCode`)を用いた招待メール送信フローで初回オンボーディングを行う
3. If 移行後にあるユーザーが新IdPへログインできない, then 移行手順 shall 原因調査手順とロールバック手順を提供する

### Requirement 7: 段階的カットオーバーとロールバック
**Objective:** As a インフラ運用担当者, I want IdP切替を安全に実施しリスクを最小化する, so that 実行委員会の業務アプリに障害を起こさず移行できる

#### Acceptance Criteria
1. While 移行作業中に一部アプリが旧authentikを参照している, the 移行手順 shall 新旧並行稼働可能な切替順序を定義する
2. If 新IdPへの切替後に重大な認証障害が発生する, then 移行手順 shall 旧authentik構成への切り戻し手順を含む
3. The 移行手順 shall GitOps原則(`gitops/`配下のマニフェスト変更→ArgoCD sync)に従い、クラスタへの直接kubectl操作を行わない

### Requirement 8: RBAC設計方針(シンプルなロール運用)
**Objective:** As a 実行委員会運営担当者, I want authentikにあった細粒度permission管理(view_group/reset_user_password等)ではなくDiscordロール相当の単純なロール運用を行う, so that 権限設計の学習・運用コストを最小化できる

#### Acceptance Criteria
1. The 新IdP基盤 shall プロジェクト単位のフラットなロール(キー・表示名のみ)でユーザー権限を表現し、permission行列による細粒度制御を導入しない
2. The 新IdP基盤 shall ロール名をOIDC ID Token/UserinfoのクレームとしてRPアプリへ配布し、権限の意味づけ(何ができるか)はRPアプリ側で解釈する設計とする
3. The 移行手順 shall authentikのrbac_role/permission_role相当の細粒度権限管理機能を移行対象から除外する

## Boundary Context
- **In scope**: OIDC Provider機能移行、LDAP認証(Dovecot連携)の翻訳層/委譲方式の設計確定、vaultwarden-rbac-syncのイベント駆動化、既存ユーザー・グループデータの招待ベース移行、段階的カットオーバー手順、Discordソーシャルログイン(単純ログインのみ)の対応可否検討、シンプルなフラットロールRBAC設計
- **Out of scope**: Discordロール自動同期・アバター自動取得・ログイン時動的グループ判定の再実装、authentik相当の細粒度permission管理の再現、新規認証機能の追加
- **Adjacent expectations**: mailserver(DMS)のDovecot認証方式変更、CMS/Roundcube/Vaultwarden等各アプリのOIDC Client設定変更は本specの実施範囲に含むが、各アプリ内部のビジネスロジック変更は含まない。Dovecot認証委譲方式(LLDAP翻訳層 vs lua+Session API)の最終選定は設計フェーズで確定する。
