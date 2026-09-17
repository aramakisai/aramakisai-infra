# DR・運用の知見

このファイルは障害対応・DR・インフラ操作で必ず参照すべき注意事項をまとめたもの。
次の会話でも常に参照されること。

---

## DR の基本方針

- **自動復旧が前提**: `dr-trigger.yml` (クラスター外部完結・5分毎cron) が複合検出
  (Tailscaleオフライン、または idp/argocd/webmail のうち2つ以上が同時応答なし) でノード障害と
  判定し、Discord通知 + 猶予期間 (既定10分) オプトアウトを経て `repository_dispatch` →
  `.github/workflows/dr-recovery.yml` が無人で復旧する。Grafana Cloud解約に伴いこの起点を
  GitHub Actions完結型へ引き継いだ (`.kiro/specs/observability-v2` 参照)
- **idp単体障害ではノード再作成しない**: 1エンドポイントのみ応答なしでTailscaleオンラインの場合は
  単体サービス障害 (SingleEndpointDown) と判定しDiscord通知のみ行う (誤ったノード再作成を防ぐ)
- **誤検知時は人手で中止できる**: 猶予期間中にOWNER/MEMBER/COLLABORATOR権限を持つアカウントが
  `dr-incident`ラベルのIssueへ`abort`/`中止`を含むコメントを付ける、またはIssueをクローズすると
  `repository_dispatch`は発火しない (権限のないコメントは無視される)
- **人手は復旧後確認、または猶予期間中の中止操作のみ**: `docs/dr-runbook.md` の「復旧後の確認」
  「dr-trigger.yml の運用」セクションを参照
- **手動手順は例外**: ワークフローが失敗した場合のフォールバックとして `docs/dr-runbook.md` の「手動フォールバック」を使う
- **検出スクリプト**: `.github/scripts/dr-trigger.sh` (複合検出・通知・猶予期間・dispatch、ユニットテスト: `scripts/test-dr-trigger-logic.sh`)
- **復旧スクリプト**: `.github/scripts/recovery.sh` (旧 `raspberry-pi/recovery/recovery.sh` から移動、内部ロジックは変更なし)

---

## kubectl の実行方法

KUBECONFIG は Infisical に YAML 内容として保存されている（ファイルパスではない）。  
**必ず `make kubectl ARGS="..."` を使うこと。** 直接 kubectl を叩かない。

```bash
make kubectl ARGS="get pods -n prod"
make kubectl ARGS="get applications -n argocd"
```

内部的に Infisical から KUBECONFIG を取得して `/tmp/kubeconfig-aramakisai` に書き出す。

---

## シークレット管理

- **Single Source of Truth は Infisical**。`.env` ファイルは参照しない
- すべての CLI 操作は `infisical run -- <command>` で実行する
- `.infisical.json` の `defaultEnvironment` が `"prod"` であることを確認する（空だと dev にフォールバックする）
- Terraform 認証情報 (`HCLOUD_TOKEN` 等) は `terraform login` (Terraform Cloud) が担う。Infisical には入っているが TFC が自動参照するため二重管理になっている

---

## CNPG (CloudNativePG) の注意事項

### recovery bootstrap を使う場合の必須設定

同一 S3 パスで `bootstrap.recovery` と `backup.barmanObjectStore` を共存させるには以下が両方必要：

```yaml
metadata:
  annotations:
    cnpg.io/skipEmptyWalArchiveCheck: enabled   # "true" では効かない

spec:
  imageName: ghcr.io/cloudnative-pg/postgresql:16.8  # 必ず明示する
```

**理由**:
- 古い PostgreSQL イメージ（`16.3` など）に埋め込まれた instance manager はアノテーションを認識しない
- CNPG Operator (1.23.3) と PostgreSQL イメージのバージョンは独立しており、明示しないと古いイメージが使われる

### クラスターを削除・再作成する際の手順

```bash
# 1. 古い Job を先に削除する（残っていると PVC が initializing で stuck する）
make kubectl ARGS="delete jobs -n prod -l cnpg.io/cluster=<name>"

# 2. クラスターを削除
make kubectl ARGS="delete cluster <name> -n prod"

# 3. ArgoCD が自動で再作成する（PVC も新規作成される）
```

古い `full-recovery` Job が残ったまま再作成すると PVC が `initializing` のまま止まる。

### Directus の WAL について

移行初期は旧クラスターからの WAL アーカイブが存在しなかったため `initdb` で起動していましたが、現在は B2 に WAL バックアップが蓄積されています。  
**DR 時は `bootstrap.recovery` で B2 から自動復元される**設計（`gitops/manifests/prod/directus/db-cluster.yaml` 参照）に更新されました。これにより、コンテンツの再投入は原則不要となっています。

---

## メールサーバー (Docker Mailserver) の注意事項

Stalwart から Docker Mailserver (DMS) v14 に移行済み。管理 CLI やアドミン Web UI は存在しない。

### メールデータのバックアップ・復元

メールデータは VolSync (ReplicationSource) で Backblaze B2 に定期バックアップ。  
DR 時は `recovery.sh` が自動で VolSync リストアを行う。  
手動で行う場合は `gitops/manifests/prod/mailserver/replication-source.yaml` を参照。

### DKIM / TLS の注意事項

- DKIM 鍵は `dkim-external-secret.yaml` から Infisical 経由で注入。Infisical に鍵が登録済みであること
- TLS は cert-manager (`mail-tls` Certificate) で管理。DR 後は ArgoCD sync で自動適用される
  - `mail-tls` が未作成の場合は `gitops/manifests/prod/cert-manager/` を先に手動 apply する

### アカウント・認証・送信者認可の構成

LDAP は使わない。関連設定は `gitops/manifests/prod/mailserver/configmap.yaml` に集約されている。

- **ML アドレス(配送専用)**: DMS の `ACCOUNT_PROVISIONER=FILE` で `postfix-accounts.cf`(ML 7件 + noreply)と
  `postfix-virtual.cf`(admin@ の5エイリアス)を静的定義する。passwd-file passdb を置かない
  (`auth-passwdfile.inc` で上書き)ため、これらのアドレスではログインできない。
- **ログイン認証**: PLAIN/LOGIN は Dovecot Lua Auth Bridge(`zitadel-auth.lua`)が Zitadel Session API へ委譲、
  OAUTHBEARER(Roundcube)は Zitadel introspection。noreply も Zitadel の human user として認証する。
- **ML 閲覧**: Zitadel 認証済みの全ユーザーが全 ML 共有メールボックスを同等権限で読み書きできる
  (`acl-postsync-job.yaml` が各部署の `dovecot-acl` に `authenticated` を書き出す)。
- **送信者認可**: `user-patches.sh` が `mua_sender_restrictions` を上書きする。From が ML アドレス(エイリアス含む)なら
  SASL 認証済みの誰でも許可、noreply は SASL ユーザー noreply のみ許可、それ以外(個人アドレス等)は拒否。
- **再発時の確認手順**:
  - `postconf -h smtpd_sender_login_maps mua_sender_restrictions` が上記の texthash マップを指していること
  - `doveadm user <ML アドレス>` が `/var/mail/aramakisai.com/<localpart>` を返すこと
  - SMTP 認証(`235`)は通るが `553 5.7.1 Sender address rejected: not owned by user` になる場合は、
    From が `ml-sender-access.cf` / `sender-login-maps.cf` に載っているかを確認する

### fail2ban によるクラスタ内 Pod の BAN

mailserver は `hostNetwork: true` + `NET_ADMIN` のため、fail2ban の BAN はノード全体の nftables
(`inet f2b-table` の input フック) に入る。クラスタ内 Pod の IP が BAN されると、SMTP だけでなく
ノード(および hostNetwork Pod)とその Pod 間の全 TCP が破棄される。Zitadel Pod が BAN されると
Dovecot Lua Auth Bridge が到達不能になり `doveadm auth test` が `code=temp_fail` を返し、
Zitadel からの SMTP 送信も失敗→認証失敗で再 BAN のループになる。

- 対策: `configmap.yaml` の `fail2ban-jail.cf` で Pod CIDR (`10.42.0.0/16`) を `ignoreip` に入れている
- 再発時の確認手順:
  ```bash
  ssh root@prod-node-1 "nft list table inet f2b-table"
  make kubectl ARGS="exec -n prod mailserver-0 -- fail2ban-client status postfix"
  make kubectl ARGS="exec -n prod mailserver-0 -- fail2ban-client get postfix ignoreip"
  ```
- 解除: `fail2ban-client set <jail> unbanip <IP>`。BAN 元の認証失敗が続いている場合は即再 BAN されるため、
  先に `ignoreip` が効いていることを確認する

---

## Tailscale デバイス削除の必要性

`ephemeral: false` のため Hetzner でノードが削除されても Tailscale デバイスが残存する。  
**terraform apply より前に必ず削除すること**（残っていると新ノードが `prod-node-1-1` として登録されて Ansible が接続できなくなる）。

これは `recovery.sh` のステップ 1 で自動化済み。手動で行う場合：

```bash
infisical run -- bash -c '
ID=$(curl -sf -H "Authorization: Bearer $TAILSCALE_API_KEY" \
  "https://api.tailscale.com/api/v2/tailnet/$TAILSCALE_TAILNET/devices" \
  | jq -r '"'"'.devices[] | select(.hostname == "prod-node-1") | .id'"'"')
[ -n "$ID" ] && curl -sf -X DELETE -H "Authorization: Bearer $TAILSCALE_API_KEY" \
  "https://api.tailscale.com/api/v2/device/$ID"
'
```

---

## ランタイム侵入検知 (Falco + intrusion-response.yml)

- **Falco**: `gitops/apps/prod/falco.yaml` で DaemonSet としてデプロイ済み。falcosidekick 経由で `DISCORD_OPS_WEBHOOK_URL` に通知
- **自動 dispatch は行わない**: 過検知リスク（CNPG barman backup・mailserver 設定生成等の正常動作も検知しうる）と falcosidekick のペイロード非互換性のため、Falco アラートから `intrusion-response` への自動 dispatch は実装していない
- **手動 dispatch**: Falco 通知を受けた人間が侵害を判断し、`.github/workflows/intrusion-response.yml` を `workflow_dispatch` で手動発火する
- **intrusion-response.yml の動作**:
  1. forensics: `kubectl logs`・`kubectl get events`・`kubectl get networkpolicy` を GitHub Actions Artifacts に保存 (90日保持)
  2. isolate: 指定 namespace に egress/ingress 全拒否 NetworkPolicy を適用
  3. notify: Discord に全シークレット一覧とローテーション手順を通知
- **再構築は手動**: シークレットローテーション完了後に人間が手動で `dr-recovery` を dispatch する。ローテーション前の自動再構築は行わない

---

## 参照先

- DR 手順全文: `docs/dr-runbook.md`
- 検出ワークフロー: `.github/workflows/dr-trigger.yml` / `.github/scripts/dr-trigger.sh`
- 自動復旧スクリプト: `.github/scripts/recovery.sh`
- 復旧ワークフロー: `.github/workflows/dr-recovery.yml`
- 侵入対応ワークフロー: `.github/workflows/intrusion-response.yml`
- CNPG 移行時の詳細知見: `.kiro/specs/single-node-migration/design.md` の「実装時の知見」セクション
- DR起動トリガー引き継ぎの設計判断: `.kiro/specs/observability-v2/design.md` の「DR Trigger」セクション
