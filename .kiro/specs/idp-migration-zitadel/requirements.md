# Requirements Document

## Project Description (Input)
現行IdP(authentik)をZitadelへ置き換える。

**本specの位置づけ(PoC)**: 本specはまずPoC(概念実証)として、k3d等の使い捨て検証環境でZitadelの実現可能性(メモリ効率・招待フロー・RBAC同期・Dovecot認証委譲)を検証することを目的とする。prod-node-1への実際のカットオーバー(段階的移行・authentik撤去)は、本PoCの結果を踏まえて別途承認・着手を判断する。tasks.mdの各タスクもこの前提でk3d検証環境を主対象とする。

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
1. The 新IdP基盤 shall LDAPサーバとして動作しない制約(公式に非対応)を前提に、Dovecot認証をZitadelへ直接委譲する構成を採用しLLDAP等の翻訳層コンポーネントを新設・存続させない
2. Where Webmail(Roundcube)経由のOAUTHBEARER/XOAUTH2認証を使う場合, the 新IdP shall Dovecotのoauth2 passdb向けにtoken introspectionエンドポイントを提供する
3. Where 一般IMAP/POP3クライアントの認証を維持する場合, the 移行手順 shall Dovecot lua passdb経由でZitadel Session API(`POST /v2/sessions`)へ認証を委譲する構成を採用する
4. The 設計 shall Dovecot lua passdb実装について以下を満たすこと:
   - パスワードをJSONへ埋め込む際は正規のエスケープ処理(文字列連結禁止)を用いる
   - ZitadelのService User/PATに割り当てる権限をSession API呼び出しに必要な最小スコープへ絞る(実装前に実機検証する)
   - Zitadel API障害時のDovecot認証フェイルモード(タイムアウト・フォールバック挙動)を定義する
5. 前spec [[idp-migration-authentik-to-authelia-lldap]] で構築したLLDAP関連資産(gitops/manifests/prod/lldap/)は本specの実装対象から除外し、移行対象としない

### Requirement 4: Vaultwardenグループ権限同期の継続
**Objective:** As a Vaultwarden RBAC運用担当者, I want vaultwarden-rbac-syncが新IdPのグループ情報をイベント駆動で取得できる, so that グループ⇔Vaultwarden Collection権限の自動同期を移行後も維持できる

#### Acceptance Criteria
1. When ユーザーのグループ/ロール割り当てが変更される, the 新IdP shall Actions v2 webhookで変更イベントを外部エンドポイントへ通知する
2. The vaultwarden-rbac-sync shall authentik固有APIへの依存を除去しZitadel API(Actions v2 webhook受信 + Management/User API問い合わせ)に置き換えられている
3. Before 本番投入する, the 移行手順 shall k3d等の使い捨て検証環境でActions v2のEvent条件トリガーの安定性を検証する(既知のリグレッション事例: zitadel/zitadel#12225)。prod-node-1と同一ノードのstaging namespaceへZitadelを追加デプロイして検証することはメモリオーバーコミット悪化につながるため行わない

### Requirement 5: Discord連携のスコープ縮小と手動運用への移行
**Objective:** As a 実行委員会運営担当者, I want Discordロール同期を廃止しグループ管理を手動運用に切り替える, so that 新IdPへの移行を過度な追加開発なしに実現できる

#### Acceptance Criteria
1. The 新IdP基盤 shall Discordロールの自動同期・アバター自動取得・ログイン時の動的グループ判定を実装しない
2. Where Discordソーシャルログイン(単純なOAuth2ログインのみ)を提供する場合, the 新IdP shall 標準的なOAuth2/OIDC Identity Provider機能で対応する
3. The 移行手順 shall `executive`グループおよび`mailAclGroups`相当のメーリングリストACLグループについて、Zitadelのproject role割り当てを唯一の真実源泉(Source of Truth)と定め、Dovecot側のACL判定はZitadelロール情報から都度導出する構成とし、Dovecot側に独立した第二のグループ管理台帳を持たせない

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

### Requirement 9: セキュリティ検証(モンキーテスト)
**Objective:** As a インフラ運用担当者, I want 前spec [[idp-migration-authentik-to-authelia-lldap]] のk3d検証で実施したものと同水準のセキュリティ検証を新IdPに対しても行う, so that 本番切替前にOIDC/認証フローの既知の攻撃パターンへの耐性を確認できる

#### Acceptance Criteria
1. When 誤ったパスワードで連続ログイン試行する, the 新IdP shall ブルートフォース対策(レート制限またはアカウント一時ロックアウト)を適用する
2. When 存在しないユーザー名でログイン試行する, the 新IdP shall 実在ユーザーの誤パスワード試行と区別不能なレスポンス(同一ステータスコード・同一応答本文)を返しユーザー列挙を防ぐ
3. When 発行済みの認可コード(authorization code)を2回目に使用する, the 新IdP shall 2回目のトークン交換を拒否する
4. When PKCEのcode_verifierがcode_challengeと一致しないトークン交換要求を行う, the 新IdP shall トークン発行を拒否する
5. When 認可リクエストのredirect_uriを事前登録値から改ざんする, the 新IdP shall 認可コード発行を拒否しエラーを返す
6. When 招待コード(invite code)を有効期限切れ後または2回目に使用する, the 新IdP shall 招待の完了(パスワード設定)を拒否する
7. The 移行手順 shall 上記1〜6の検証をk3d等の使い捨て検証環境で実機テストとして実施し、テストスクリプトと結果を記録として残す

### Requirement 10: 機能検証(正常系動作確認)
**Objective:** As a インフラ運用担当者, I want 各Requirementで定義した機能が実機で正しく動くことを確認する, so that 本番切替前に「動くはず」ではなく「動くことを確認済み」の状態にできる

#### Acceptance Criteria
1. When 正しいユーザー名・パスワードでOIDC Authorization Code Flow + PKCEを実行する, the 新IdP shall 認可コード発行・トークン交換・Userinfo取得までEnd-to-Endで成功する
2. The 移行手順 shall CMS/Vaultwarden/Roundcubeそれぞれについて実際のOIDCログインを実施し、各アプリで正常にセッションが開始されることを確認する
3. When 一般IMAP/POP3クライアントが正しいパスワードで認証する, the Dovecot lua passdb経由のZitadel Session API委譲 shall 認証成功しmail属性・ACLグループ(Zitadelロールから導出)を正しく返す
4. When Roundcube経由でOAUTHBEARER/XOAUTH2認証する, the Dovecot oauth2 passdb shall introspection成功後にログインを許可する
5. When ユーザーのグループ/ロール割り当てを変更する, the vaultwarden-rbac-sync shall Actions v2 webhook経由で変更を検知しVaultwarden Collection権限へ反映する(反映までの実測遅延を記録する)
6. When 新規ユーザーへ招待コードを発行する, the 移行手順 shall 招待メール受信→リンク遷移→パスワード設定→初回ログインまでEnd-to-Endで成功することを確認する
7. When ユーザーにロールを付与する, the 新IdP shall OIDC ID Token/UserinfoのクレームにロールがRequirement 8の設計通り反映されることを確認する
8. The 移行手順 shall 旧authentik構成への切り戻し手順(Requirement 7.2)を実際に実行し、切り戻し後に既存アプリのログインが復旧することを検証する

### Requirement 11: Terraformプロバイダー認証のブートストラップ
**Objective:** As a インフラ運用担当者, I want ZitadelのTerraform providerが必要とする管理者トークンを安全にブートストラップする, so that project/role/applicationのIaC管理を開始できる

#### Acceptance Criteria
1. The ブートストラップ手順 shall Zitadelインスタンス初回起動後、Ansibleで組織/管理者ユーザー作成とService User/PAT発行を行いInfisicalへ登録する
2. The ブートストラップ手順 shall 既存の`infisical-auth` Secret作成と同様に、ArgoCD/GitOpsが管理できない領域(ESO自体を起動する前提条件)への対応として、GitOps原則の明示的な例外に位置づける
3. If ブートストラップ済みのPAT/Service User Tokenが失効・漏洩した場合, then 運用手順 shall 再発行・Infisical更新の手順を提供する

## Boundary Context
- **In scope**: OIDC Provider機能移行、Dovecot lua passdb経由のZitadel Session API認証委譲の実装、vaultwarden-rbac-syncのイベント駆動化、既存ユーザー・グループデータの招待ベース移行、段階的カットオーバー手順、Discordソーシャルログイン(単純ログインのみ)の対応可否検討、シンプルなフラットロールRBAC設計、OIDC/認証フローのセキュリティ検証(モンキーテスト)、各機能の正常系End-to-End動作確認、Terraformプロバイダー認証のブートストラップ
- **Out of scope**: Discordロール自動同期・アバター自動取得・ログイン時動的グループ判定の再実装、authentik相当の細粒度permission管理の再現、新規認証機能の追加、LLDAP関連資産(前spec由来)の継続利用
- **Adjacent expectations**: mailserver(DMS)のDovecot認証方式変更、CMS/Roundcube/Vaultwarden等各アプリのOIDC Client設定変更は本specの実施範囲に含むが、各アプリ内部のビジネスロジック変更は含まない。
