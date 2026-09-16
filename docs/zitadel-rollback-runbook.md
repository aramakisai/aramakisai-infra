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

### Step1: 切り戻し対象コミットを特定する

```bash
git log --oneline -- \
  gitops/manifests/prod/mailserver \
  gitops/manifests/prod/cms-secrets \
  gitops/manifests/prod/vaultwarden \
  gitops/manifests/prod/roundcube
```

カットオーバーで実際にマージされたコミット(通常はtask9.4のPRのマージコミット
1件)のSHAを確認する。

### Step2: GitOpsマニフェストをrevertする

```bash
git revert --no-commit <カットオーバーコミットのSHA>
git diff --cached   # authentik構成(remoteRef.keyが旧キー名)に戻っていることを目視確認
git commit -m "revert(idp): Zitadel認証障害により旧authentik構成へ切り戻す"
git push
# PRを作成しレビュー・マージする(通常のGitOpsフローと同じ)
```

対象コミットがカットオーバー用のマージコミットの場合、`--no-commit`のままでは
親が2つある(`-m 1`が必要)ため、実際には
`git revert --no-commit -m 1 <SHA>`となる場合がある。`git diff --cached`で
`gitops/manifests/prod/{mailserver,cms-secrets,vaultwarden,roundcube}/`以外に
意図しない差分が含まれていないことを必ず確認する。

### Step3: ArgoCD syncを実行する

```bash
argocd app sync mailserver --server-side
argocd app sync cms --server-side
argocd app sync vaultwarden --server-side
argocd app sync roundcube --server-side
```

### Step4: 検証

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
- **vaultwarden-rbac-sync webhook(`terraform/tunnel.tf`の`idp.aramakisai.com`
  向け`/webhook/rbac-sync`ingress、`ansible/roles/zitadel-bootstrap`のaction_target)の
  切り戻し**: 不要。この経路はZitadelのAction Target/Executionという
  Zitadel側リソースのみで完結しており、authentik構成への切り戻し(RPアプリの
  OIDC接続先変更)とは独立している。切り戻し後もこのTunnel ingress自体は残置して
  問題ない(呼び出し元のZitadelが使われなくなるだけで、単体では実害がない)。

## 既知のギャップ

- 本ランブックは文書の整備のみ(task9.5)であり、実際のカットオーバー・切り戻しは
  まだ本番に対して一度も実行していない。手順の実地検証はtask9.6でk3d PoC上のみ
  行った(本番のArgoCD/Infisical/DNSへの実操作は含まない)。
