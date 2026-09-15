# Zitadel セキュリティ検証(モンキーテスト)結果

## 概要

`scripts/zitadel-security-poc-tests.py` による、spec `idp-migration-zitadel` task 7
(セキュリティ検証)の実機検証結果。k3d検証環境(`zitadel-poc`クラスタ)で実施した。

**実行方法**: クラスタ内DNS(`zitadel.zitadel.svc.cluster.local`)経由でのみ疎通するため、
netshoot等のin-cluster Pod(本検証では`poc-apps`namespaceの`toolbox`Pod)へスクリプトを
`kubectl cp`し、Pod内から`python3`で実行した。port-forward + Host偽装はExternalDomain不一致
(`Instance not found`)で失敗するため使えない。

**必要な認証情報**:
- `ZITADEL_TOKEN`: 管理用PAT(Terraform provider用と同じもの)。Session API・招待コード
  発行/検証・ユーザー作成に使う。
- `ZITADEL_LOGIN_CLIENT_TOKEN`: `session.link`権限を持つログインクライアント用PAT
  (`login-client.pat`)。OIDC CreateCallback(`POST /v2/oidc/auth_requests/{id}`)は
  この権限がないと`AUTH-AWfge`(No matching permissions found)で拒否される
  (実機確認済み。管理用PAT/IAM_OWNER相当では不可、ログインUI専用の権限が必要)。

## 実行結果 (2026-08-31、k3d Zitadel v4.12.3、`zitadel-poc`クラスタ)

```bash
ZITADEL_DOMAIN=http://zitadel.zitadel.svc.cluster.local:8080 \
ZITADEL_TOKEN=<管理PAT> ZITADEL_LOGIN_CLIENT_TOKEN=<login-clientPAT> \
ZITADEL_TEST_USERNAME=testuser1@aramakisai.com ZITADEL_TEST_PASSWORD='TestPassw0rd!' \
ZITADEL_CLIENT_ID=388716558723121171 ZITADEL_CLIENT_SECRET=<CMS client secret> \
ZITADEL_REDIRECT_URI=https://cms.aramakisai.com/api/auth/zitadel/callback \
  python3 scripts/zitadel-security-poc-tests.py --all
```

全項目、終了コード0(スクリプト自体はどの検証結果であっても異常終了しない設計。
合否は各セクションのログイベントで判定する)。

### 7.1 ブルートフォース対策とユーザー列挙耐性 — **一部不合格(要対応)**

| 検証項目 | 結果 |
|---|---|
| 誤パスワード連続15回試行後もレート制限/アカウントロックアウトが発火しない | **不合格**。15回連続失敗後も直後の正しいパスワードでのログインが即座に成功した(手動追加検証では18回連続失敗でも同様)。`Session API`のレスポンスは`failedAttempts`カウンタを都度返す(15まで単調増加)にもかかわらず、いかなる時点でもHTTPステータス・エラー内容に変化はなかった |
| 存在しないユーザー名 vs 実在ユーザーの誤パスワードが区別不能なレスポンスを返す | **不合格(Session API生レスポンスのレベルで)**。存在しないユーザー: `HTTP 404 / code 5 "User could not be found"`。実在ユーザー誤パスワード: `HTTP 400 / code 3 "Password is invalid"`。ステータスコード・エラーコード・メッセージいずれも異なり明確に区別可能。加えてレスポンス時間も実在ユーザー側が有意に遅い(パスワードハッシュ照合コストのため、実測で0.65〜2.9秒 vs 存在しないユーザーは0.01秒未満)というタイミングサイドチャネルも存在する |

**原因確認**: `GET /admin/v1/policies/lockout` / `GET /management/v1/policies/lockout`
はいずれも`isDefault: true`のポリシーを返すのみで`maxPasswordAttempts`等の値が
一切含まれない。Zitadelのデフォルトlockout policyはブルートフォース対策が
**無効(MaxPasswordAttempts=0)** な状態であることを実機で確認した。

**緩和されている点**: `gitops/manifests/prod/mailserver/configmap.yaml`の
Dovecot Lua Auth Bridge(`auth_passdb_lookup`)は、401/400(パスワード誤り)・404
(ユーザーなし)を一律`PASSDB_RESULT_PASSWORD_MISMATCH`へ正規化しているため、
**IMAP/POP3クライアントから見えるレスポンス面ではユーザー列挙耐性が確保されている**。
ただしlua側に定数時間化の実装はなく、応答速度差によるタイミングサイドチャネルは
Dovecot経由でも未対策のまま残る可能性がある(本検証ではDovecot越しのタイミングまでは
計測していない)。OIDC RPアプリ(CMS/Vaultwarden/Roundcube)のログイン画面は
Zitadel標準ログインUIを使うため、こちらの表示面での区別可否は本検証の対象外
(生API呼び出しのみを検証した)。

**推奨対応(本番カットオーバー前)**: Terraformまたは`POST /admin/v1/policies/lockout`で
`maxPasswordAttempts`を明示的に設定し、ブルートフォース対策を有効化すること。
現状はauthentik(標準でリトライ制限ポリシーが有効)からの後退となる。

### 7.2 認可コードreplay・PKCE不一致・redirect_uri改ざん — **合格**

CMSアプリ(`client_id=388716558723121171`)のAuthorization Code Flow + PKCEで検証。

| 検証項目 | 結果 |
|---|---|
| 未登録redirect_uriでの認可リクエスト | **合格**。`/oauth/v2/authorize`が`HTTP 400 invalid_request`("redirect_uri is missing in the client configuration")を即座に返し、認可コード自体発行されない |
| PKCE code_verifier不一致でのトークン交換 | **合格**。`HTTP 400 invalid_grant "invalid code_verifier"`で拒否 |
| 正規verifierでの初回トークン交換 | 成功(`HTTP 200`、`access_token`/`id_token`取得。id_tokenのroles claimも設計通り含まれることを併せて確認) |
| 発行済み認可コードの2回目使用(リプレイ) | **合格**。`HTTP 400 invalid_request "Errors.AuthRequest.NoCode"`で拒否 |

セッション作成(Session API)→`POST /v2/oidc/auth_requests/{authRequestId}`
(CreateCallback、`session.link`権限のPATが必須)→codeでの`/oauth/v2/token`という、
Zitadel標準ログインUI相当の手順をAPIで再現して検証した。

### 7.3 招待コードの期限切れ・再利用防止 — **再利用防止は合格、期限切れは未確認**

| 検証項目 | 結果 |
|---|---|
| 検証済み招待コードの2回目使用 | **合格**。`POST /v2/users/{id}/invite_code/verify`の1回目は成功、同一コードでの2回目は`HTTP 400, code 3 "Code is invalid"`で拒否 |
| 有効期限切れコードでの拒否 | **未確認**。`CreateInviteCode`に`expiration: "3s"`を指定して8秒待機後に検証したが、コードは無効化されず検証に成功した。V2招待コードのAPIは有効期限のカスタマイズを受け付けない(サイレントに無視される)ことを実機で確認した。upstream issue [zitadel/zitadel#10474](https://github.com/zitadel/zitadel/issues/10474)が報告する「V2招待コードのsecret-generator設定(有効期限含む)はAdmin API/Consoleに未公開、デフォルト固定(約72時間)」という内容と整合する。72時間の実待機は本PoCの時間スケールでは非現実的なため、期限切れによる拒否そのものは確認できていない |

## 総合所見

- **最優先で対応すべき問題**: 7.1のブルートフォース対策(lockout policy)がデフォルトで
  無効。本番カットオーバー前に`maxPasswordAttempts`等のlockout policyをTerraformまたは
  Admin APIで明示的に有効化することを強く推奨する
- 7.2(認可コードreplay・PKCE・redirect_uri改ざん)はすべてZitadel標準機能により合格
- 7.3は再利用防止のみ確認済み。期限切れ拒否はAPI経由での期限短縮ができず未検証
  (デフォルト約72時間固定、本番相当データでの長時間検証機会があれば別途確認が望ましい)
- ユーザー列挙耐性はSession API生レスポンスのレベルでは不合格だが、実際にエンドユーザーへ
  露出する経路(Dovecot Lua Auth Bridge)ではステータスコード面は緩和されている。
  タイミングサイドチャネルは未対策

## 使用したテストスクリプト

`scripts/zitadel-security-poc-tests.py`(stdlib `urllib`のみ、外部依存なし。
`scripts/zitadel-invite-migration.py`と同一パターン: argparse + `log_event` JSON Lines)。
`--all`/`--bruteforce`/`--oidc`/`--invite`で個別実行も可能。実行ログ全文は
本ドキュメント作成時の記録として`docs/`配下には含めていない(JSON Linesはこの
ドキュメントの表へ要約転記済み)。再実行すれば同じログが再現できる。
