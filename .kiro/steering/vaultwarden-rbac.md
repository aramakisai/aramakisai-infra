# Vaultwarden RBAC 運用手順

## 前提

Vaultwarden (OSS) は Collection 権限を**ユーザー単位**でのみ設定可能。
グループ単位の権限設定は未対応のため、**Authentik グループ名と Vaultwarden Collection 名を一致させ、運用手順でマッピング**を維持する。

---

## 命名規則

| 層 | 命名 | 例 |
|---|---|---|
| Authentik Group | 機能・役割名 | `SNS`, `会計`, `広報`, `企画` |
| Vaultwarden Organization | 資格情報カテゴリ | `SNSアカウント`, `口座情報`, `Googleアカウント` |
| Vaultwarden Collection | **Authentik Group と同一** | `SNS`, `会計`, `広報`, `企画` |

**ルール**: Collection 名は Authentik グループ名と**必ず一致**させる。これにより、どのグループがどの Collection にアクセス可能か一目で判断できる。

---

## 権限マトリクス

| Vaultwarden 権限 | 説明 | 使用例 |
|---|---|---|
| `Can View` | 閲覧のみ（パスワード表示含む） | 一般メンバーへの閲覧許可 |
| `Can View Except Passwords` | 自動入力可、パスワード非表示 | 外部協力者への制限付きアクセス |
| `Can Edit` | 編集・追加・削除可 | 班長・責任者 |
| `Can Manage` | 管理者（メンバー招待・権限変更） | 部門長・システム管理者 |

---

## オンボーディング手順

新規メンバーが Authentik グループに追加された場合:

1. **Authentik 確認**
   ```bash
   # 対象ユーザーが正しいグループに所属しているか確認
   # Authentik Admin UI → Directory → Groups → <group名> → Users
   ```

2. **Vaultwarden Organization へ招待**
   - Vaultwarden Web UI → 対象 Organization → Manage → People → Invite
   - メールアドレスを入力（Authentik で登録されているメールアドレス）
   - **注意**: SMTP 未設定のため、招待メールは送信されない。ユーザーに「Organization に参加してください」と直接連絡する

3. **Collection 権限付与**
   - Organization → Manage → Collections → 対象 Collection
   - 「Access」タブ → 対象ユーザーを選択 → 権限を設定
   - **Collection 名 = Authentik グループ名** で対応関係を確認

4. **確認**
   - ユーザーに SSO ログイン → Organization 切り替え → Collection 表示 を確認させる

---

## オフボーディング手順

メンバーが Authentik グループから削除・退会した場合:

1. **Vaultwarden 権限削除**
   - Organization → Manage → People → 対象ユーザー → Remove
   - または Collection → Access → 対象ユーザーの権限を削除

2. **Authentik 確認**
   - 対象ユーザーが Authentik グループから削除されているか確認
   - 退会の場合は Authentik ユーザー自体も無効化・削除

---

## 具体例

### 例: 広報班の Twitter アカウント

**Authentik**:
- Group: `広報`
- Members: `taro@aramakisai.invalid`, `hanako@aramakisai.invalid`

**Vaultwarden**:
- Organization: `SNSアカウント`
- Collection: `広報`（Authentik Group と同名）
- Items: `Twitter 公式アカウント`
- Access:
  - `taro@aramakisai.invalid` → `Can Edit`
  - `hanako@aramakisai.invalid` → `Can View`

**運用フロー**:
1. 新メンバー `jiro@aramakisai.invalid` が Authentik グループ `広報` に追加される
2. 管理者が Vaultwarden Organization `SNSアカウント` に `jiro@aramakisai.invalid` を招待
3. Collection `広報` に `jiro@aramakisai.invalid` を `Can View` で追加
4. `jiro@aramakisai.invalid` が SSO ログイン後、`SNSアカウント` Organization を選択し、`Twitter 公式アカウント` を閲覧

---

## 制約事項

- **グループ単位権限自動連携なし**: Vaultwarden OSS はユーザーベースのみ。Authentik グループ変更は手動反映が必要
- **SMTP 設定済み**(`8f65183`): 招待メールは実際に送信される。送信元は`noreply@aramakisai.com` <!-- confidential:allow -->
- **DMS個人メール受信廃止**: mailserverのLDAP連携(`LDAP_QUERY_FILTER_USER`)は`mailListAddress=true`属性を持つメーリングリスト専用ユーザーのみ配送対象。`@aramakisai.com`の新規個人宛アドレスは原則受信不可
- **SSO_ONLY=true**: パスワードログインは不可。SSO 経由のみアクセス可能

---

## サービスアカウント (vaultwarden-rbac-sync) 初回ブートストラップ

`vaultwarden-rbac-sync` (Authentikグループ→Vaultwarden Collection権限の自動同期) が使う専用Vaultwardenサービスアカウントの
Personal API Key発行は、`SSO_ONLY=true`下では実行できない（鶏と卵問題、`research.md`参照）ため一回限りの手動作業が必要。
クラウド側の自動化対象外（`design.md` Out of Boundary）。

**重要**: このサービスアカウントはAuthentikでは作成しない。AuthentikユーザーでSSOログインさせると
`SSO_ALLOW_UNKNOWN_EMAIL_VERIFICATION=true`の初回SSOメール検証フローに乗ってしまい、検証メールが
届かない限りログイン不能になる（人間ユーザーの初回SSO向け機能であり、サービスアカウント用途ではない）。
SSOを一切経由しない**Vaultwardenネイティブアカウント**として作成すること。

**前提**:
- 対象Organization全てに既に参加しているOrganization Owner/Adminアカウントでの作業が必要
  （招待後の「メンバー確認 (Confirm)」はOrg Ownerの復号済み鍵でしか行えないため、CLIや自動化では代替不可）
- 招待先メールアドレスは**実際に受信できるもの**を使う（`SMTP設定済み`のため招待メールは実送信される。
  `rbac-sync-bot@aramakisai.invalid`はドキュメントのサンプル表記であり実際には受信不可。DMSは
  メーリングリスト専用ユーザー以外の個人メール受信を廃止済みのため、`@aramakisai.com`の新規アドレスも
  原則使えない。運営者本人の実メールアドレス等を一時的に使う）

1. **対象Organization全てに招待**
   - Vaultwarden Web UI → 対象 Organization → Manage → People → Invite
   - 実際に受信できるメールアドレスを **Admin** 権限で招待（招待メールが実際に送信される）

2. **招待を受諾 → マスターパスワード設定**
   - 届いたメールの招待リンクから受諾し、マスターパスワードを設定（この時点ではSSO_ONLYの影響を受けない。
     `/identity/connect/token`の`password`グラントのみがブロック対象のため）

3. **SSO_ONLY を一時解除 → ログイン → Personal API Key 発行**
   - `gitops/manifests/prod/vaultwarden/deployment.yaml` の `SSO_ONLY` を `false` に変更し、コミット&反映
   - 反映確認: `make kubectl ARGS="get pods -n prod -l app=vaultwarden"` で再起動完了を確認
   - Web Vaultにマスターパスワードでログイン
   - 受諾後、Organization Owner が Web UI で当該メンバーを **Confirm**（この操作のみOrg Owner本人のブラウザ作業が必須）
   - サービスアカウント自身でPersonal API Keyを発行: Web Vault → Settings → Security → Keys → API Key（マスターパスワード再入力が必要）
   - 得られた `client_id`(`user.<uuid>`) / `client_secret` を Infisical に登録
     ```bash
     # --silent でも値がテーブル表示されるため出力を必ず抑制する
     infisical secrets set VAULTWARDEN_RBAC_SYNC_SERVICE_ACCOUNT_CLIENT_ID="user.<uuid>" --env=prod >/dev/null 2>&1
     infisical secrets set VAULTWARDEN_RBAC_SYNC_SERVICE_ACCOUNT_CLIENT_SECRET="<client_secret>" --env=prod >/dev/null 2>&1
     ```

4. **SSO_ONLY を復元**
   - `SSO_ONLY` を `true` に戻しコミット&反映（作業時間を最小化すること）

5. **動作確認**
   - `kubectl get secret vaultwarden-rbac-sync-secrets -n prod -o jsonpath='{.data}'` で全キーが存在することを確認

マスターパスワードは手順3の発行後は不要（API Keyのみで運用）。紛失してもAPI Key発行済みなら再ログイン不要だが、
ローテーション（API Key再発行）には再度マスターパスワードでのログインが必要なため、運用者間で安全に引き継ぐこと。

---

## 参照

- [Vaultwarden Admin API](https://github.com/dani-garcia/vaultwarden/wiki/Enabling-admin-page)
- Authentik Groups: `https://idp.aramakisai.com/if/admin/#/identity/groups`
