# Zitadel 招待移行ランブック

## 概要

`scripts/zitadel-invite-migration.py` による、既存authentikユーザーのZitadelへの
招待ベース移行(spec `idp-migration-zitadel` task 6.1)の実行手順・障害調査・
ロールバック手順。

**スコープ**: 本ドキュメントは招待発行〜ユーザー初回ログインまでの単体手順が対象。
prod-node-1でのauthentik構成への全面的な切り戻し(IdPそのもののロールバック)は
task 9.5/9.6(バックアップ移行による本番カットオーバー)の対象であり、現時点で未実装。

## 実行手順

### 入力データ

デフォルトではPoC検証用のダミーユーザー(`SAMPLE_USERS`、5名・複数グループ所属含む)を使う。
実データを移行する場合は `--input` に以下のJSON配列を渡す:

```json
[
  {"email": "user@example.com", "given_name": "太郎", "family_name": "山田", "groups": ["企画", "リーダー"]}
]
```

`groups`はauthentikのグループ表示名(`terraform/authentik_vaultwarden_rbac_sync.tf`等で定義)。
`GROUP_DISPLAY_TO_ROLE_KEY`(`terraform/zitadel_projects.tf`の`aramakisai_project_roles`と同期)
でZitadel project role_keyへマッピングされる。未知のグループ名は`unknown_group_skipped`として
ログに記録されスキップされる(移行自体は失敗しない)。

### 実行

```bash
# 内容確認(API呼び出しなし)
ZITADEL_DOMAIN=<endpoint> ZITADEL_PROJECT_ID=<project_id> ZITADEL_TOKEN=<PAT> \
  python3 scripts/zitadel-invite-migration.py --dry-run

# 実行(SMTP未設定環境: 招待コードをAPIレスポンスで受け取る)
ZITADEL_DOMAIN=<endpoint> ZITADEL_PROJECT_ID=<project_id> ZITADEL_TOKEN=<PAT> \
  python3 scripts/zitadel-invite-migration.py --input users.json

# 実行(SMTP設定済みの本番相当環境: Zitadelのメール送信に委ねる)
... python3 scripts/zitadel-invite-migration.py --input users.json --send-email
```

再実行しても安全(冪等)。ユーザー作成・ロール付与ともにZitadelが返すHTTP 409
(`User already exists` / `User grant already exists`)を「既に完了済み」として扱う。
ただし招待コード発行(`CreateInviteCode`)は再実行のたびに新しいコードを発行し、
**旧コードを無効化する**(実機確認済み、後述)。再実行時は対象ユーザーへ再度招待を
案内すること。

### ログの読み方

各行はJSON。`invite_code_issued`イベントの`invite_code`フィールドは、
`--send-email`未指定時(returnCodeモード)にのみ出力される。このコードは平文で
初回パスワード設定に使えるため、**本番でSMTP配信が可能な場合は必ず`--send-email`を
使い、ログにコードを残さない**こと。returnCodeモードはSMTP基盤がないk3d検証専用。

## 障害調査: 移行後にログインできないユーザーが発生した場合

1. **ユーザーのstate確認**: `POST /v2/users` (`userNameQuery`)で対象ユーザーを検索し、
   `state`が`USER_STATE_ACTIVE`か確認する。
2. **招待コードの状態確認**: 招待コードは一度検証(`VerifyInviteCode`)に成功すると
   再利用できない(`Code is invalid`, code=3, HTTP 400、実機確認済み)。ユーザーが
   「リンクが無効」と申告した場合、二重クリックや古いメールの再利用が典型的な原因。
   → `CreateInviteCode`を再実行し新しいコードを再案内する(スクリプト再実行、または
   個別に`POST /v2/users/{id}/invite_code`)。
3. **ロール割当の確認**: `POST /management/v1/users/grants/_search`
   (`roleKeyQuery`または`userIdQuery`)で該当ユーザーのuser_grantを確認する。
   ログインはできてもRPアプリ側で権限エラーになる場合はここを疑う。
4. **メールアドレスのtypo**: 誤ったメールで作成した場合はユーザーを削除し
   (下記「個別ユーザーの取り消し」)、正しいメールで再実行する。

## ロールバック手順

### 個別ユーザーの取り消し(誤招待・typo等)

```bash
curl -X DELETE "$ZITADEL_DOMAIN/v2/users/$USER_ID" \
  -H "Authorization: Bearer $ZITADEL_TOKEN"
```

実機確認済み(HTTP 200)。ユーザー削除によりuser_grant・招待コードも合わせて失効する。

### 移行バッチ全体の取り消し

本スクリプトは対象ユーザー一覧を`--input`のJSONファイルまたは`SAMPLE_USERS`で
管理しているため、実行ログ(`user_ready`イベントの`user_id`)を保存しておけば
対象を機械的に列挙できる。全体を取り消す場合は保存した`user_id`一覧に対して
上記DELETEをループで実行する。ロールとしてZitadel project roleそのもの
(`terraform/zitadel_projects.tf`)やproject/application定義には触れない
(このスクリプトが変更するのはユーザー・グラント・招待コードのみ)。

## 実機検証結果 (2026-08-31、k3d Zitadel v4.12.3、`zitadel-poc`クラスタ)

サンプル5ユーザー(佐藤太郎: 企画+リーダー、鈴木花子: 会計、田中一郎: 出店+出演、
山本恵: 広報+総務、高橋修: 管理者+実行委員 — ダミーメールアドレス、実authentikデータ不使用)で
以下をすべて実機確認済み:

- ユーザー作成 + ロール付与(user_grant) + 招待コード発行(returnCodeモード): 5/5成功
- スクリプト再実行による冪等動作: 5/5とも`created: false` / `already_granted`で正常終了
- 招待コード再発行による旧コードの無効化: 旧コードでの`VerifyInviteCode`が
  `Code is invalid`(code=3, HTTP 400)で拒否されることを確認
- E2E(佐藤太郎で実施): 招待コード取得(API returnCode) → `VerifyInviteCode`成功
  → `SetPassword`成功 → Session API(`POST /v2/sessions`、username+password)による
  初回ログイン成功(`factors.password.verifiedAt`が付与されることを確認)
- 招待コードのリプレイ防止: 検証済みコードの再送信が`Code is invalid`で拒否されることを確認
  (task 7.3のセキュリティ検証と一部重複するが、招待フロー自体の健全性確認として実施)

k3dクラスタ(`zitadel-poc`)は削除せず、作成したサンプル5ユーザーはPoCの実施記録として
残置している。
