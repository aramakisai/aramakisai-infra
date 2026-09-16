# Zitadel 一括カットオーバー ランブック

authentikからZitadelへ本番の認証基盤を切り替える際の実行順序と、各ステップ後の
検証手順を示す。

**現在の状態**: Step1(Dovecot)・Step2(RPアプリOIDC Client)は本番で稼働中。
Step3(招待ベース移行)は本番Zitadel自体にSMTP設定が存在しないため未実施
(下記「既知のギャップ・未実施事項」参照)。

自動化: `ansible/playbooks/zitadel-cutover.yml`(`ansible/roles/zitadel-cutover`)。
`ZITADEL_CUTOVER_TARGET_ENV`環境変数(`k3d`|`prod`、既定`k3d`)で対象環境を切り替える。

## 実行順序

1. Dovecot Lua Auth Bridge切替
2. RPアプリ(CMS/Vaultwarden/Roundcube)OIDC Client切替
3. 既存ユーザーへの招待ベース移行

この順序である理由: 1と2は互いに独立だが、3(招待)はユーザーが実際にログイン
できる状態(1・2完了後)で行わないと「招待は成功したがどのアプリにもログイン
できない」状態を生む。

vaultwarden-rbac-sync(Actions v2 webhook経由のロール同期)はスコープ外とした
(Vaultwarden自体が本番でreplicas: 0のまま凍結中で使われていないため、tasks.md
task9.2参照)。

## 前提条件

- task9.1(export取得)・task9.2(project/role/application/action等の投入)・
  task9.3(Lockout Policy等のAdmin API import)が本番Zitadelに対して完了していること。
- 本ランブックが対象とするgitops manifest変更(`gitops/manifests/prod/{mailserver,
  cms,cms-secrets,vaultwarden,roundcube}/`)がmainへマージ済みであること。
- Infisical `prod`環境に以下のキーが登録済みであること(task9.2の
  `ansible/roles/zitadel-bootstrap/vars/resources.yml` infisical_hint参照):
  `CMS_PROD_OIDC_CLIENT_ID`/`CMS_PROD_OIDC_CLIENT_SECRET`、
  `VAULTWARDEN_OIDC_CLIENT_ID_ZITADEL`/`VAULTWARDEN_OIDC_CLIENT_SECRET_ZITADEL`、
  `ROUNDCUBE_OIDC_CLIENT_ID`/`MAIL_OAUTH2_CLIENT_SECRET_ZITADEL`、
  `DOVECOT_ZITADEL_AUTH_PAT`、`ZITADEL_INVITE_RECOVERY_SA_PAT`(いずれも登録済み)。

## Step1: Dovecot Lua Auth Bridge切替

対象ファイル: `gitops/manifests/prod/mailserver/{configmap.yaml,statefulset.yaml,
dovecot-lua-auth-external-secret.yaml,dovecot-oauth2-external-secret.yaml}`。

一般IMAP/POP3クライアント認証(`auth-ldap.conf.ext`、ファイル名はDMSの自動生成分を
上書きするため据え置き、中身はLua)をLDAP(authentik-ldap-outpost)からZitadel
Session APIへ切り替える。Postfixのメールボックス存在確認・MLグループ展開は
引き続きLDAPを使用する(design.md Requirement 15、別タスクのスコープ)。

**既知の未検証事項**: `docker-mailserver:15.1.0`イメージ(Debianベース)に
`dovecot-lua`パッケージおよび`lua5.3-socket`/`lua5.3-cjson`相当が同梱されているか
は未確認(k3d実機検証はAlpineベースの軽量テストハーネスで実施したため)。適用前に
`mailserver-0`のコンテナ内で`dpkg -l | grep dovecot`等により確認すること。同梱が
無い場合、`user-patches.sh`(DMS公式拡張フック)経由での追加インストールが必要になる。

### 実行

```bash
# GitOps原則により、mailserver Podへの変更はPRマージ+ArgoCD syncで行う
git log --oneline -- gitops/manifests/prod/mailserver/  # マージ済み確認
argocd app sync mailserver --server-side
```

### 検証チェックリスト

- [ ] `make kubectl ARGS="get pods -n prod -l app=mailserver"` で `mailserver-0` が
      `Running`(再起動後、CrashLoopBackOffでないこと)
- [ ] `make kubectl ARGS="logs -n prod mailserver-0 | grep -i lua"` にエラーが
      出ていないこと
- [ ] 実在ユーザーの正しいパスワードでIMAP LOGINが成功すること
      (`python3 imaplib`等、`docs/zitadel-security-poc-tests-results.md`の
      手法を参照)
- [ ] 誤ったパスワードでIMAP LOGINが拒否されること
- [ ] `master userdb out:` ログに`acl_groups=<department>`が正しい値で
      出力されること(部署メンバーのログを1件確認)
- [ ] RoundcubeからのOAUTHBEARERログインが成功すること(dovecot-oauth2側の
      introspection先切替、後述Step2完了後に確認可能)
- [ ] メーリングリスト宛メールの配送(Postfix経由、LDAP側は無変更)が
      従来通り機能すること

## Step2: RPアプリ(CMS/Vaultwarden/Roundcube) OIDC Client切替

対象ファイル: `gitops/manifests/prod/cms/deployment.yaml`、
`gitops/manifests/prod/cms-secrets/external-secret.yaml`、
`gitops/manifests/prod/vaultwarden/deployment.yaml`、
`gitops/manifests/prod/roundcube/{config-configmap.yaml,deployment.yaml,
external-secret.yaml}`。

### 実行

```bash
argocd app sync cms --server-side
argocd app sync vaultwarden --server-side   # Vaultwarden本体が凍結中(replicas:0)の場合は別途解凍が必要、後述の注記参照
argocd app sync roundcube --server-side
```

**注記(Vaultwarden凍結)**: `vaultwarden/deployment.yaml`は2026-07-11から
`replicas: 0`で凍結中(認証基盤の切替とは無関係な理由、ファイル内コメント参照)。
このステップはOIDC設定の切替のみを行い、凍結解除(`replicas: 1`への変更、
`db-cluster.yaml`のinstances復元、`scheduled-backup.yaml`/
`vaultwarden-rbac-sync`のsuspend解除)は別途の運用判断とする。

### 検証チェックリスト

- [ ] CMS: `https://cms.aramakisai.com/admin/login` からZitadelへリダイレクトされ、
      ログイン後セッションが開始されること
- [ ] Vaultwarden(凍結解除後): `https://vault.aramakisai.com` からZitadelへ
      リダイレクトされ、ログイン後セッションが開始されること
- [ ] Roundcube: `https://webmail.aramakisai.com` からZitadelへリダイレクトされ、
      ログイン後Inboxが表示されること(Step1のDovecot oauth2 introspection切替と
      合わせて確認)
- [ ] 3アプリいずれもid_token/userinfoの
      `urn:zitadel:iam:org:project:roles`クレームに正しいroleが含まれること
- [ ] `scripts/zitadel-cutover-verify.py oidc-e2e`を各アプリの実client_id/secret
      (Infisical prod環境から取得、`--client-secret`をターミナル出力に残さないこと)
      で実行し、Authorization Code + PKCE + userinfoまで成功することを確認する

## Step3: 既存ユーザーへの招待ベース移行

`scripts/zitadel-invite-migration.py`(task6.1/task9.4)を使う。CSVフォーマット:
`email,given_name,family_name,role_keys`(role_keysはセミコロン区切り)。

既存authentikユーザー一覧のエクスポート手順自体は本ランブックのスコープ外
(authentik管理画面またはAPIから別途取得すること)。

### 実行

```bash
export ZITADEL_API_BASE_URL=https://idp.aramakisai.com
export ZITADEL_EXTERNAL_DOMAIN=idp.aramakisai.com
export ZITADEL_INVITE_RECOVERY_SA_PAT=$(infisical run --env=prod -- printenv ZITADEL_INVITE_RECOVERY_SA_PAT)
python3 scripts/zitadel-invite-migration.py --csv=/path/to/existing-users.csv --send-email
```

`--send-email`を付けない場合、招待コードは`returnCode`で標準出力(JSON Lines)へ
返るのみでメール送信は行われない(SMTP未設定環境向け)。本番では`--send-email`を
使い、事前に本番Zitadelの`ZITADEL_SMTP_*`設定が完了していることを確認すること。

冪等性: ユーザー作成・ロール付与は既存確認してから実行するため再実行安全。
招待コードは再実行のたびに新規発行され旧コードは無効化される(design.md
「招待オンボーディングフロー」、task6.1実機確認済み)。

### 検証チェックリスト

- [ ] `scripts/zitadel-invite-migration.py`の出力(JSON Lines)で`failed`が0件で
      あること
- [ ] サンプル数名について招待メールが実際に届くこと
- [ ] 招待リンクからパスワード設定・初回ログインが成功すること
- [ ] 初回ログイン後、CMS/Vaultwarden/Roundcube/メールいずれもそのユーザーの
      ロールに応じた権限で利用できること

## 全ステップ完了後の最終確認

- [ ] CMS/Vaultwarden/Roundcube/メール(IMAP/POP3/Roundcube経由)のいずれも
      Zitadel経由で正常にログインできること
- [ ] authentik(LDAP outpost含む)への依存が、design.md Requirement 15の
      対象(Postfixのメールボックス・ML展開)以外に残っていないこと
- [ ] Cloudflare Access(`cloudflare-access` OIDC Application)経由の管理画面
      アクセスが引き続き機能すること
- [ ] 障害発生時はtask9.5(切り戻し手順)を参照する

## 既知のギャップ・未実施事項

- **Step3(招待ベース移行)は未実施**。本番Zitadelインスタンス自体にSMTP設定が
  存在しない(`GET /admin/v1/smtp`が`404 SMTP configuration not found`)ため、
  `scripts/zitadel-invite-migration.py --send-email`は招待メールを配送できない。
  本specはZitadel自身のSMTP設定投入をタスク化しておらず、送信元アドレス・
  リレー方式の方針決定とgitops/Infisical実装が別途必要。
- Step3の実行には既存authentikユーザーの実CSV(`email,given_name,family_name,
  role_keys`)も別途準備が必要(本ランブックのスコープ外、authentik管理画面
  またはAPIから取得すること)。
