# Zitadel 切り戻しランブック

Zitadelへの一括カットオーバー(`docs/zitadel-cutover-runbook.md`)後に重大な認証障害が
発生した場合に、旧authentik構成へ切り戻す手順を示す。

GitOps原則(CLAUDE.md)に従い、本手順はすべて手動操作で行う。自動化スクリプトは
用意しない(`kubectl`/`argocd`/`terraform`の直接実行や、Infisicalシークレットの
一括書き換えを機械的に行うスクリプトは、実行対象や退避先を誤ると被害が広がる
操作であり、手順書を見ながら人間が1コマンドずつ確認して実行する方が安全なため)。

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

- **authentikの構成・Podを削除/凍結しないこと**。カットオーバー後も一定期間
  (最低でも切り戻し判断が可能な期間)`gitops/apps/prod/authentik.yaml`の
  ArgoCD Applicationとその配下リソースを稼働させたまま残すこと。

この点はtask9.4のカットオーバー手順書には明記されておらず、実際のカットオーバー
実行時にはこのランブックの手順に従って事前に確認すること。

## Infisicalキーについて(退避不要)

`ansible/roles/zitadel-bootstrap/vars/resources.yml`の`infisical_hint`が示す通り、
Zitadelが発行するclient_id/secretは authentik時代のキーを上書きせず、
`_ZITADEL`サフィックス付きの別名キーとして新規に保存する設計になっている。

- `VAULTWARDEN_OIDC_CLIENT_ID_ZITADEL` / `VAULTWARDEN_OIDC_CLIENT_SECRET_ZITADEL`
- `MAIL_OAUTH2_CLIENT_SECRET_ZITADEL`
- `CMS_PROD_OIDC_CLIENT_ID` / `CMS_PROD_OIDC_CLIENT_SECRET`(CMSはauthentik時代に
  存在しなかった新規アプリのため、そもそも上書き対象のキーが無い)

authentik時代のキー(`VAULTWARDEN_OIDC_CLIENT_ID`・`VAULTWARDEN_OIDC_CLIENT_SECRET`・
`MAIL_OAUTH2_CLIENT_SECRET`)はカットオーバー後も変更されずInfisicalに残り続ける。
そのため、**切り戻し時にInfisicalの値を事前に退避・事後に復元する操作は原理上
不要**である。`gitops/`配下のマニフェストをgit revertでauthentik構成の
`remoteRef.key`指定に戻せば、ExternalSecretが自動的に無傷のauthentik時代の値を
再取得する。

## 切り戻し手順

### Step1: 切り戻し対象パスと直前の既知良好コミットを特定する

```bash
git log --oneline -- \
  gitops/manifests/prod/mailserver \
  gitops/manifests/prod/cms-secrets \
  gitops/manifests/prod/cms \
  gitops/manifests/prod/vaultwarden \
  gitops/manifests/prod/roundcube
```

**対象パスは`cms-secrets`だけでなく`cms`自身も含める**(CMSの
`AUTHENTIK_ISSUER_URL`/`AUTHENTIK_CLIENT_ID`は`cms/deployment.yaml`に直書きされて
おり、`cms-secrets`だけでは戻らない。2026-09-17のtask9.6実地検証で本ランブック
自身にこの漏れがあったことを発見・修正した)。

上記のログで、カットオーバー関連コミット(2026-09-17本番カットオーバーでは
`dfcaca4`本体+`47aab29`mailserver修正の2コミット)の直前にある、認証障害が
起きていなかった最後のコミット(以下「良好コミット」)のSHAを控える。

### Step2: GitOpsマニフェストを良好コミットの内容へ戻す

カットオーバーコミットは`gitops/`以外(ansible role・ドキュメント等)も同時に
変更していることが多く、`git revert <カットオーバーコミットのSHA>`をそのまま
使うと対象外ファイルまで巻き込んで無関係なコンフリクトを起こす
(2026-09-17のtask9.6実地検証で実際に`.kiro/specs/.../tasks.md`・
`docs/zitadel-cutover-runbook.md`等で発生することを確認済み)。そのため、対象
パスだけを良好コミットの内容へ戻す方式を使う。

```bash
# 1. 対象パスのみを良好コミットの内容へ戻す(cmsを含む5パス)
git checkout <良好コミットのSHA> -- \
  gitops/manifests/prod/mailserver \
  gitops/manifests/prod/cms-secrets \
  gitops/manifests/prod/cms \
  gitops/manifests/prod/vaultwarden \
  gitops/manifests/prod/roundcube

# 2. カットオーバーで新規追加されたファイル(良好コミットの時点で存在しなかったもの)は
#    checkoutだけでは削除されないため、明示的に洗い出してgit rmする
git diff --name-status <良好コミットのSHA> HEAD -- \
  gitops/manifests/prod/mailserver gitops/manifests/prod/cms-secrets \
  gitops/manifests/prod/cms gitops/manifests/prod/vaultwarden \
  gitops/manifests/prod/roundcube
# ステータス "A"(新規追加)の行に出たパスを git rm する
# (2026-09-17カットオーバーでは gitops/manifests/prod/mailserver/
#  dovecot-lua-auth-external-secret.yaml が該当)
git rm gitops/manifests/prod/mailserver/dovecot-lua-auth-external-secret.yaml

# 3. 良好コミットの内容と完全一致することを確認する(出力が空であること)
git diff <良好コミットのSHA> -- \
  gitops/manifests/prod/mailserver gitops/manifests/prod/cms-secrets \
  gitops/manifests/prod/cms gitops/manifests/prod/vaultwarden \
  gitops/manifests/prod/roundcube

git commit -m "revert(idp): Zitadel認証障害により旧authentik構成へ切り戻す"
git push
# PRを作成しレビュー・マージする(通常のGitOpsフローと同じ)
```

手順3の`git diff`が空でない場合、良好コミットの選定が誤っているか、対象外の
差分が混入している。空になるまでStep1からやり直すこと。

### Step3: Cloudflare Tunnelのidp.aramakisai.comルーティングをterraformで切り戻す

**重要**: `idp.aramakisai.com`は`gitops/`ではなく`terraform/tunnel.tf`の
`cloudflare_zero_trust_tunnel_cloudflared_config.main`が管理しており、Step2の
GitOps revertだけでは戻らない。この手順を飛ばすと、Step2でRPアプリのOIDC設定を
authentik構成に戻しても、ブラウザは実際には`idp.aramakisai.com`経由でZitadelへ
リダイレクトされ続け、切り戻しが機能しない(2026-09-17のtask9.6実地検証で
本番の実設定から判明した既知のギャップ)。

`terraform/tunnel.tf`の`idp.aramakisai.com`向けingress_ruleを、Zitadel向けの
2ルール(login v2 UI:3000 / API:8080)から、切り戻し前の単一ルールへ戻す。

```hcl
    ingress_rule {
      hostname = "idp.aramakisai.com"
      service  = "http://authentik-server.prod.svc.cluster.local:80"
    }
```

適用は必ず対象リソースへ`-target`を付けて実行すること。**素の`terraform apply`は
実行しないこと。** 2026-09-17時点で本specと無関係な既存の未適用差分
(Directus撤去・Cloudflare Access IdP切替等、`4 to add, 6 to change, 12 to destroy`)が
蓄積しており、素の`apply`はこれらを巻き込んで意図しない破壊的変更を本番に
適用してしまう。

```bash
cd terraform
infisical run --env=prod -- terraform apply \
  -target=cloudflare_zero_trust_tunnel_cloudflared_config.main
# 出力が "Apply complete! Resources: 0 added, 1 changed, 0 destroyed" であることを確認する
# (カットオーバー時の適用(865364a)と同じ対象・同じ変更点数になるはず)
```

Dovecot Lua Auth Bridge(mail/IMAP・POP3)はこの公開ホスト名を経由せず
クラスタ内DNS(`zitadel.zitadel.svc.cluster.local`、
`ansible/roles/zitadel-cutover/defaults/main.yml`の`zitadel_cutover_api_base_url`)
を直接参照するため、本Stepの影響を受けない。Step2のrevertのみで復旧する。

### Step4: ArgoCD syncを実行する

```bash
argocd app sync mailserver --server-side
argocd app sync cms --server-side
argocd app sync vaultwarden --server-side
argocd app sync roundcube --server-side
```

### Step5: 検証

- [ ] `https://idp.aramakisai.com/.well-known/openid-configuration` の応答が
      Zitadelではなくauthentikのものに戻っていること(Step3の効果を直接確認する)
- [ ] CMS: `https://cms.aramakisai.com/admin/login` からauthentikへリダイレクトされ、
      ログインできること
- [ ] Vaultwarden(凍結解除環境の場合): `https://vault.aramakisai.com` から
      authentikへリダイレクトされ、ログインできること
- [ ] Roundcube: `https://webmail.aramakisai.com` からauthentikへリダイレクトされ、
      ログイン後Inboxが表示されること
- [ ] IMAP/POP3クライアント認証がauthentik-ldap-outpost経由で成功すること
      (Step2のrevertでDovecot Lua Auth Bridge関連ファイルもあわせて削除され、
      `auth-ldap.conf.ext`がDMS自動生成分に戻るため、本来のLDAP認証に戻る)
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
## 既知のギャップ

- task9.6(2026-09-17)時点で、実際の本番切り戻し(Step2〜5の実行)そのものは
  まだ一度も実行していない。task9.4のStep1(mailserver)・Step2(CMS/Roundcube)は
  この時点で本番稼働中(idp.aramakisai.com経由のZitadelログインが実際に
  使われている状態)であり、確認目的だけで実際に本番認証を切り戻す/戻す往復は
  可用性への影響が大きいため見送った。本ランブックの正しさは、(1)実リポジトリの
  使い捨てクローンに対してStep2のコマンド列を実際に実行し、対象5パスが良好
  コミットの内容と`git diff`で完全一致(出力ゼロ)することを確認、(2)Step3で
  必要な`terraform/tunnel.tf`の切り戻しが従来漏れていたことを本番の実設定
  (`make kubectl`によるArgoCD Application参照・`terraform/tunnel.tf`の実内容)
  から発見・追記、という形で行った(本番リポジトリへのrevertコミットや本番への
  terraform applyそのものは未実行)。使い捨てクローンは検証後に削除済み。
- 実際の切り戻し実行は、次にDovecot Lua Auth Bridge・RPアプリのいずれかで
  Step3記載の切り戻し基準に該当する障害が発生したタイミング、またはユーザーが
  意図的な切り戻しリハーサルの実施を承認したタイミングで行うこと。
