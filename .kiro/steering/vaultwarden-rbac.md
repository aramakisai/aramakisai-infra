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
- **SMTP 未設定**: 招待メールが送信されない。直接連絡または管理画面での手動承認が必要
- **SSO_ONLY=true**: パスワードログインは不可。SSO 経由のみアクセス可能

---

## 参照

- [Vaultwarden Admin API](https://github.com/dani-garcia/vaultwarden/wiki/Enabling-admin-page)
- Authentik Groups: `https://idp.aramakisai.com/if/admin/#/identity/groups`
