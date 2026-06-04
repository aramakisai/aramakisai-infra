# 調査・知見メモ（2026-06-04 デバッグセッションより）

## 現状の構成

- **メールサーバー**: Stalwart v0.16.6 (K3s, hostNetwork, prod-node-1)
- **認証**: `Authentication.directoryId = authentik-oidc`
- **LDAP Outpost**: `ak-outpost-stalwart-ldap-outpost.prod.svc.cluster.local:389`
- **Roundcube**: OAuth2（client_id=`aramakisai-mail`, Confidential）
- **Authentik**: v2026.5.2

## 確認できた Stalwart v0.16 の制約

### 1. Authentication.directoryId は singleton（最重要）
- `directoryId` は1つしか設定できない
- OAUTHBEARER は `directoryId` が OIDC type でなければ動作しない
  - エラー: `"Failed to decode token. If you are using an external OIDC provider, make sure it is configured as the default directory under the Authentication object."`
- PLAIN/LOGIN は OIDC type だと動作しない
  - エラー: `"Unsupported credentials type for OIDC backend"`
- **LDAP + OIDC の同時サポートは不可**

### 2. per-listener directoryId は存在しない
- NetworkListener に `directoryId` フィールドを付けると `invalidPatch | Invalid property` エラー

### 3. Domain.directoryId はメール配送用のみ
- `Domain.directoryId = authentik-ldap` を設定しても IMAP 認証には効かない
- メール配送（受信者存在確認）には必須

### 4. OIDC directory の issuerUrl は厳格一致を要求
- `issuerUrl` ≠ discovery の `issuer` → `"Issuer mismatch"` エラーで directory が無効化
- Authentik "Same identifier" モード: discovery は per-app URL で提供されるが `issuer` は global (`https://idp.aramakisai.com/`)
- global の `/.well-known/openid-configuration` は 404（Authentik は提供しない）
- **Authentik "Same identifier" + Stalwart の組み合わせは不可**

### 5. Stalwart OidcProvider に clientId/clientSecret フィールドなし
- アカウントポータル（/account）の OAuth2 クライアント認証情報を Stalwart 側で設定できない
- アカウントポータルは `client_id=stalwart-webui` がハードコードされている

### 6. Stalwart アカウントポータルの OAuth2 フロー
- `/account` → Stalwart 自身の `/login` エンドポイント → PKCE で Authentik にリダイレクト
- `stalwart-webui` (Public client) + PKCE → 動作 ✅
- `stalwart-webui` (Confidential) → Stalwart が secret を送らないため失敗 ❌

### 7. Authentik 2026.5.2 は Public client に PKCE を必須とする
- `code_challenge_methods_supported: ["plain", "S256"]`
- PKCE なしのコード交換 → 失敗（"OAuth login failed"）
- Roundcube 1.7 は PKCE 非対応

### 8. ROPC（Resource Owner Password Credentials）未実装
- OIDC directoryId で PLAIN auth を試みても `"Unsupported credentials type"` のまま
- Stalwart は OIDC directory での ROPC をサポートしない

## テスト済みの組み合わせと結果

| directoryId | Roundcube OAUTHBEARER | Thunderbird PLAIN | アカウントポータル |
|---|---|---|---|
| authentik-oidc（aramakisai-mail） | ✅ | ❌ | ❌（issuer mismatch） |
| authentik-oidc（stalwart-webui） | ❌（PKCE必須） | ❌ | ✅ |
| authentik-ldap | ❌ | ✅（TOTP無効） | ❌（Stalwart login form、OIDC non-support） |

**結論: どの組み合わせも全要件を満たせない**

## メールサーバー要件（ユーザー提示）

1. メールの送受信
2. CNPG 対応・WAL アーカイブからリストア（現状は VolSync で代替）
3. VolSync バックアップ/リストア
4. Authentik グループ → メーリングリスト自動作成
5. Gmail 含む全サーバーへ送信可能
6. スパム分類
7. リソース消費が少ない
8. Roundcube 対応
9. **Authentik 単一認証ソース + メジャークライアント全対応（TOTP含む）**
10. K3s/K8s と相性がよい

## Stalwart の評価

- 要件 1〜8, 10: ✅ 全て満たす
- 要件 9: ❌ v0.16 では不可（v0.17+ での改善待ちまたは外部プロキシで回避）

## 解決策の候補

### 案 A: Dex（OIDC プロキシ）を Stalwart の前段に置く
- Dex が単一 issuer を提供 → Stalwart は Dex だけを見る
- Dex のバックエンドに Authentik（LDAP + OIDC）
- Stalwart は変更最小
- Dex は K8s 対応・軽量（Go製）
- 懸念: Dex の設定複雑性、追加サービス管理

### 案 B: Dovecot（IMAP）+ OpenSMTPD（SMTP）に移行
- Dovecot `passdb` スタックで LDAP + oauth2 passdb を同時設定
- スパムは rspamd/SpamAssassin が必要（外部依存増）
- Stalwart が持つ DKIM 自動管理・スパム内蔵等の機能を個別に代替する必要
- K3s 対応: Docker 公式イメージあり
- 実績が最も豊富

### 案 C: Stalwart v0.17+ を待つ
- GitHub に Issue/PR があれば確認
- スケジュール不明

### 案 D: Stalwart を維持し Roundcube を主軸、IMAP クライアントは App Password
- 現状最も安定
- App Password はユーザー自身が /account で作成（stalwart-webui issuer 時のみ機能）
- Roundcube と /account ポータルは排他的（同時に動かない）

## 現在の設定状態（このセッション終了時点）

- Stalwart `Authentication.directoryId = authentik-oidc`（stalwart-webui issuer）
- Roundcube: `client_id=stalwart-webui`, `client_secret=''`（動作しない状態）
- Authentik: `stalwart-webui` Public、`aramakisai-mail` Confidential、両方 Per application

**次のチャットでやること:**
1. Roundcube を動作状態（aramakisai-mail）に戻す
2. 上記解決策のいずれかを実装する

## 関連ファイル

- Stalwart 設定: `gitops/manifests/prod/stalwart/`
- Roundcube 設定: `gitops/manifests/prod/roundcube/`
- DR 知見: `.kiro/steering/dr.md`
- 既存障害記録: `.claude/projects/.../memory/project_stalwart_auth*.md`
