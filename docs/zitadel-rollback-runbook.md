# Zitadel 切り戻しランブック

Zitadelへの一括カットオーバー(`docs/zitadel-cutover-runbook.md`)後に重大な認証障害が
発生した場合に、旧authentik構成へ切り戻す手順を示す。

自動化: `scripts/zitadel-rollback.sh`。GitOps原則(CLAUDE.md)に従い、このスクリプトは
`kubectl`/`argocd`/`terraform`をいずれも実行しない。マニフェストのrevertとInfisical
シークレットの復元のみを機械的に行い、ArgoCD syncは人間が本手順に従って実行する。

## 切り戻しを判断する基準

以下のいずれかに該当する場合、切り戻しを検討する。

- CMS/Vaultwarden/Roundcube/メール(IMAP/POP3)のいずれかで、影響範囲が一部ユーザーに
  留まらない規模のログイン不能が発生し、かつZitadel側の設定修正(Admin Console等)
  では即時復旧できない
- Zitadel本体(Pod/DB)の障害でOIDC/Session APIが応答しない
- Dovecot Lua Auth Bridgeの不具合で正規ユーザーの認証が広範囲に拒否される

軽微な不具合(一部ユーザーのロール未反映、招待メール未達など)は切り戻し対象では
なく、Zitadel側の個別修正で対応する。

## 前提条件(カットオーバー実行前に必ず満たしておくこと)

切り戻しの可否は、カットオーバー時点で以下が守られているかに懸かっている。

1. **authentikの構成・Podを削除/凍結しないこと**。カットオーバー後も一定期間
   (最低でも切り戻し判断が可能な期間)`gitops/apps/prod/authentik.yaml`の
   ArgoCD Applicationとその配下リソースを稼働させたまま残すこと。
2. **Infisical prod環境の既存キーをカットオーバー実行前にバックアップしておくこと**
   (`scripts/zitadel-rollback.sh backup-secrets prod`)。理由は次節参照。

この2点はtask9.4のカットオーバー手順書には明記されておらず、実際のカットオーバー
実行時にはこのランブックの手順に従って事前に行うこと。

## 重要な注意: Infisicalキーの使い回し

`ansible/roles/zitadel-bootstrap/vars/resources.yml`の`infisical_hint`が
「既存キー更新」と記載している通り、以下のInfisical prod環境のキーは
**authentik時代と同じキー名のまま値だけがZitadelのclient_id/secretで上書き**される。

- `CMS_PROD_OIDC_CLIENT_SECRET`
- `VAULTWARDEN_OIDC_CLIENT_ID`
- `VAULTWARDEN_OIDC_CLIENT_SECRET`
- `MAIL_OAUTH2_CLIENT_SECRET`

そのため、`gitops/`配下のマニフェストをgit revertでauthentik構成に戻しても、
上記4キーの値がZitadelのものに上書きされたままだと認証が復旧しない
(authentikのissuer URLに対してZitadelのclient_secretを送ることになり失敗する)。
**カットオーバー実行前に必ずこれらの値を退避しておくこと。**

(新設キーの`CMS_PROD_OIDC_CLIENT_ID`・`ROUNDCUBE_OIDC_CLIENT_ID`・
`DOVECOT_ZITADEL_AUTH_PAT`・`ZITADEL_INVITE_RECOVERY_SA_PAT`は、切り戻し後の
authentik構成マニフェストからは参照されなくなるだけなので、削除は不要かつ対応不要)

バックアップを取り忘れた場合、authentikのAdmin UI(Applications > Providers)で
既存Providerのclient_id/client_secretを再確認できる(Authentikは登録済み
Providerのclient_secretを後からでも表示できる、Zitadelと異なり発行時一度限りではない)。

## 切り戻し手順

### Step0: カットオーバー実行前(事前準備、済んでいなければここで実施)

```bash
infisical run --env=prod -- \
  scripts/zitadel-rollback.sh backup-secrets prod
```

退避先はデフォルトで`.zitadel-rollback-secrets-backup/`(値そのものはターミナルに
出力されない、`.gitignore`済みであることを確認すること)。

### Step1: 切り戻し対象コミットを特定する

```bash
scripts/zitadel-rollback.sh find-commits
```

`gitops/manifests/prod/{mailserver,cms,cms-secrets,vaultwarden,roundcube}/`を
変更したコミット一覧が出る。カットオーバーで実際にマージされたコミット(通常は
task9.4のPRのマージコミット1件)のSHAを確認する。

### Step2: GitOpsマニフェストをrevertする

```bash
scripts/zitadel-rollback.sh revert <カットオーバーコミットのSHA>
git diff --cached   # authentik構成に戻っていることを目視確認
git commit -m "revert(idp): Zitadel認証障害により旧authentik構成へ切り戻す"
git push
# PRを作成しレビュー・マージする(通常のGitOpsフローと同じ)
```

`revert`サブコマンドは対象コミットが上記GitOpsパスを実際に変更しているかを
確認してから`git revert --no-commit`を行う。無関係なコミットSHAを渡した場合は
エラーで停止する。

### Step3: ArgoCD syncを実行する

```bash
argocd app sync mailserver --server-side
argocd app sync cms --server-side
argocd app sync vaultwarden --server-side
argocd app sync roundcube --server-side
```

### Step4: Infisicalシークレットを復元する

```bash
infisical run --env=prod -- \
  scripts/zitadel-rollback.sh restore-secrets prod
```

Step0で退避した4キーの値をauthentik時代の値へ書き戻す。ExternalSecretの
`refreshInterval`(既定1h、ファイルごとに異なる場合あり)が経過するまで
反映が遅れる場合は、該当ExternalSecretへの手動リフレッシュ
(`kubectl annotate externalsecret <name> -n prod force-sync=$(date +%s) --overwrite`)
または対象Podの再起動で即時反映させる。

### Step5: 検証

- [ ] CMS: `https://cms.aramakisai.com/admin/login` からauthentikへリダイレクトされ、
      ログインできること
- [ ] Vaultwarden(凍結解除環境の場合): `https://vault.aramakisai.com` から
      authentikへリダイレクトされ、ログインできること
- [ ] Roundcube: `https://webmail.aramakisai.com` からauthentikへリダイレクトされ、
      ログイン後Inboxが表示されること
- [ ] IMAP/POP3クライアント認証がauthentik-ldap-outpost経由で成功すること
      (Step2のrevertでStep1のDovecot Lua Auth Bridge関連ファイルも
      あわせて削除され、`auth-ldap.conf.ext`がDMS自動生成分に戻るため、
      本来のLDAP認証に戻る)
- [ ] カットオーバー後にZitadel招待経由でのみアカウントを作成したユーザー
      (`scripts/zitadel-invite-migration.py`のStep4実行後に追加された新規ユーザー)
      がいる場合、そのユーザーはauthentikに元々アカウントが無いため
      切り戻し後にログインできない。該当者の有無を確認し、必要であれば
      authentik側に個別でアカウントを作成する

## 切り戻しの範囲外(対応不要な理由)

- **Zitadel Project/Role/Application(task9.2で投入したリソース)の削除**:
  不要。RPアプリがZitadelを参照しなくなるだけで、残存していても実害はない。
  再カットオーバー時にそのまま再利用できる。
- **Zitadelへ招待済みのユーザーデータ(task9.4 Step4)の削除**: 不要。
  authentikの既存ユーザーデータはtask9.1のexportで消えておらず、authentik側は
  そのまま利用できる状態のため、Zitadel側のデータを消す必要はない。
- **vaultwarden-rbac-sync webhookの切り戻し**: 本ランブック作成時点でStep3
  (`docs/zitadel-cutover-runbook.md`)は未確定のため本番投入されておらず、
  切り戻し対象に含まれない。Step3が将来投入された場合は、対象のgitops manifest
  もカットオーバーコミットに含めてrevert対象とすること。

## 既知のギャップ

- 本ランブック・`scripts/zitadel-rollback.sh`は文書・スクリプトの整備のみ
  (task9.5)であり、実際のカットオーバー・切り戻しはまだ本番に対して一度も
  実行していない。手順の実地検証はtask9.6で行う。
- `restore-secrets`のInfisical CLI呼び出し自体は、本番Zitadelがまだ稼働開始
  していないため実機(prod環境)では検証していない。ロジック(コマンド構築・
  シークレット値をターミナルへ出力しないこと)は`scripts/test-zitadel-rollback.sh`
  でfake infisicalコマンドに対して検証済み。
