# DR・運用の知見

このファイルは障害対応・DR・インフラ操作で必ず参照すべき注意事項をまとめたもの。
次の会話でも常に参照されること。

---

## DR の基本方針

- **検知は通知のみ、復旧は人の承認付き**: `dr-trigger.yml` (クラスター外部完結・5分毎cron) が
  Tailscale 上の実在ノード (`prod-node-N`) の接続状態と idp/argocd/webmail の疎通を複合判定し、
  1回の実行内で障害判定が3回連続したときだけ Discord 通知と `dr-incident` Issue 起票/追記を行う。
  復旧ワークフローの自動起動はしない。open な `dr-incident` Issue があれば追記のみ (重複起票しない)
- **復旧は `dr-recovery.yml` を `workflow_dispatch` で人が起動**: GitHub Environment `dr-recovery`
  の required reviewers (team `infra`、起動者本人の承認も可) の承認後にジョブが始まる。入力は `target_node` (必須)・`force`。冒頭で Environment の required reviewers を検査し、`main` 以外では起動しない
- **生存確認ゲート**: `recovery.sh` は冒頭で Hetzner サーバー状態・Tailscale・公開エンドポイント・
  `kubectl get nodes` を読み取り専用で確認し、生存または判定不能を示すシグナルが1つでもあれば停止する
  (`force` でのみ上書き)
- **対象は単一ノード構成のみ**: 他の Hetzner サーバー (残存 etcd メンバー候補) があるときや、
  inventory の cluster-init ホスト以外が対象のときは自動復旧せず停止し、手動手順に委ねる
- **idp単体障害ではノード扱いしない**: 1エンドポイントのみ応答なしでTailscaleオンラインの場合は
  SingleEndpointDown、一部ノードのみ切断でクォーラム維持なら NodeDegraded とし通知のみ
- **Terraform は対象ノードに -target 限定・auto-apply 無効**: plan が「対象サーバー作成 + auth key 置換」
  だけであることを機械検査してから apply する。逸脱した plan は discard して停止する
- **Tailscale は OAuth クライアント統一**: `TAILSCALE_OAUTH_CLIENT_ID/SECRET` (devices:core 書込 +
  auth_keys 書込)。旧デバイスは「対象ノード名一致 かつ offline」のものだけ ID 指定で削除。
  サーバー停止のみのときはデバイスを消さず電源投入だけ行う
- **電源投入のみの経路は Ansible を流さない**: サーバーが停止しているだけなら起動して k3s の Ready を確認するだけ。
  Ansible を流す経路 (再作成・force) は冪等化済みの `k3s-bootstrap.yml` (`tasks/ensure_secret.yml` の存在) が前提
- **再作成後はメール DNS/rDNS を別 run で追従**: `mail_prod_node_1` / `mail_prod_node_1_ipv4` / `mail_ipv4` / `mail_ipv6`
  の4アドレスだけを target にし、plan がこの範囲を出れば停止する
- **メールデータのリストアは自動化しない**: mailserver Application の selfHeal が replicas=0 を戻して稼働中 PVC に書込むため。
  稼働中 PVC には書かず、新名の plain PVC + ReplicationDestination (`copyMethod: Direct`) へリストアし、RD 削除 →
  `claimName` / `sourcePVC` の切替をコミットだけで行う (`docs/dr-runbook.md`、雛形は `docs/templates/mailserver-restore.yaml`)。
  RD を常設しない (再作成で `lastManualSync` が失われ再リストアが走る)。RD には障害発生時刻の `restoreAsOf` を必須とする。
  DR の新クラスターでは ReplicationSource が作成直後に空 PVC をバックアップしてしまうため、起動前に `spec.paused: true` をコミットしておく
  (`recovery.sh` が検査して止める)。新 PVC には `volume.kubernetes.io/selected-node: prod-node-1` を付ける。volume populator は VolSync 0.9.1 + local-path では不可
- **手動手順は例外**: ワークフローが失敗した場合のフォールバックとして `docs/dr-runbook.md` の「手動フォールバック」を使う
- **検出スクリプト**: `.github/scripts/dr-trigger.sh` (ユニットテスト: `scripts/test-dr-trigger-logic.sh`)
- **復旧スクリプト**: `.github/scripts/recovery.sh` (ユニットテスト: `scripts/test-dr-recovery-logic.sh`)
- **zitadel-db は `bootstrap.recovery`**: `s3://aramakisai-backups/cnpg/zitadel-db` から復元する。稼働中クラスターの
  `spec.bootstrap` 変更は CNPG operator が無視する (v1.23.3 の webhook に bootstrap 変更検証が無く、
  primary 生成は PVC 不在かつ `LatestGeneratedNode=0` のときだけ)

---

## kubectl の実行方法

共有 kubeconfig は存在しない。人は `make kube-login` で GitHub 経由の短命クライアント証明書 (有効期限 7 日) を発行し、手元のコンテキスト `aramakisai-prod` を使う。
**必ず `make kubectl ARGS="..."` を使うこと。** 直接 kubectl を叩かない。

```bash
make kube-login                       # 初回・7 日ごと・クラスタ再作成後
make kubectl ARGS="get pods -n prod"
make kubectl ARGS="get applications -n argocd"
```

### DR 時の kube-apiserver 認証

- `dr-recovery` は GitHub Actions OIDC (ユーザー名 `gha:dr-recovery`) で認証する。API サーバーは Environment `dr-recovery` の承認を経たジョブのトークンだけを `gha:dr-recovery` として受け入れ、cluster-admin を持つ。
- k3s は初回起動から OIDC 認証設定を読み込む。`gha:dr-recovery` の binding は ArgoCD の同期を待たず `k3s-bootstrap.yml` が先行適用するため、ArgoCD が壊れていても DR から直せる。
- クラスタを作り直すと server CA と client CA が変わる。`recovery.sh` は bootstrap 後に OIDC の kubeconfig を新しい server CA で作り直す。

### クラスタ再作成後の人の証明書と server CA

- 作り直し前に発行した人の証明書は新しい client CA で検証できず、すべて無効になる。各自が `make kube-login` で再発行する。
- 手元のコンテキストの server CA も古くなる。`make kube-login` は CA の違いを検出すると指紋を表示して止まる。クラスタ再作成が正当な理由であることを確認してから `--accept-new-ca` で更新する。
- server CA は `make kube-login` が tailnet 経由でノードから取得する (tailnet が信頼の根拠)。
- 発行済みの証明書を即時に無効化する必要がある場合は client CA の forced rotation を行う。手順と戻し方は `.kiro/specs/kube-github-auth/design.md` の「client CA forced rotation の手順」。rotation 後も人は同様に再発行する。
- GitHub 障害中は発行できないため、緊急時に限り Tailscale SSH でノード上のローカル admin (`/etc/rancher/k3s/k3s.yaml`) を使う。

---

## シークレット管理

- **Single Source of Truth は Infisical**。`.env` ファイルは参照しない
- すべての CLI 操作は `infisical run -- <command>` で実行する
- `.infisical.json` の `defaultEnvironment` が `"prod"` であることを確認する（空だと dev にフォールバックする）
- 手元の `terraform` 実行は `terraform login` (Terraform Cloud) で認証する。`HCLOUD_TOKEN` 等のプロバイダー認証情報は TFC 側が保持する
- DR 経路 (`recovery.sh`) は TFC API を直接叩くため、Infisical `prod` の `TFC_API_TOKEN` / `TFC_WORKSPACE_ID` を使う。トークンは `owners` team の team token (有効期限なし。期限切れで障害時に DR が黙って止まるのを避けるため。無料プランでは team を `owners` 1つしか作れない。organization token は run を作れず、user token は個人に紐づくため不可)。org 管理者と同等の権限を持ち DR に必要な範囲より広いが、intrusion-response のローテーション対象に含まれる

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

### WAL アーカイブ失敗とディスク肥大化

`hetzner-os-credentials`（`gitops/manifests/shared/eso/hetzner-os-external-secret.yaml`）は `prod` namespace 向けにのみ定義されている（namespace-scoped ExternalSecret）。CNPG クラスターを `prod` 以外の namespace に置く場合（例: `zitadel` namespace の `zitadel-db`）は、そのクラスターと同じ namespace に同名の ExternalSecret を別途配置する必要がある。存在しない場合、`backup.barmanObjectStore` を設定していても Secret 未検出でバックアップ処理自体が起動できない。

WAL アーカイブが失敗し続けると、アーカイブ未完了の WAL セグメントは削除されずにインスタンスの `pg_wal` へ蓄積し続ける。StorageClass が `local-path` の場合 PVC の容量上限（`spec.storage.size`）はホストのボリュームサイズを制限しないため、蓄積した WAL はそのままノードのルートファイルシステムを消費し続け、放置するとノード全体のディスクフルに至る。ルートファイルシステムには root 予約領域があるため kubelet の `DiskPressure` 条件は立たず、この経路では検知できない。

アーカイブ失敗の有無は `kubectl get cluster <name> -n <namespace> -o jsonpath='{.status.conditions}'` の `ContinuousArchiving` 条件（`status: "False"` で失敗、`.message` に失敗理由）で確認できる。この条件とノードルートディスク使用率は `.github/workflows/infra-health-check.yml`（cron）が定期監視し、閾値超過時に Discord へ通知する。

### Zitadel login の PAT 依存

login v4.18 以降は PAT 未発行だと CrashLoop する。新規構築や DR 時の `zitadel-bootstrap` で
`kubectl exec -c login` を使う PAT 回収は、login が起動しないと実行できないため影響しうる。
`ZITADEL_LOGIN_SESSION_COOKIE_SECRET` も Infisical に存在しないと login が ready にならない。

---

## メールサーバー (Docker Mailserver) の注意事項

Stalwart から Docker Mailserver (DMS) v14 に移行済み。管理 CLI やアドミン Web UI は存在しない。

### メールデータのバックアップ・復元

メールデータは VolSync (ReplicationSource) で Backblaze B2 に定期バックアップ。  
DR 時の VolSync リストアは自動化していない (`docs/dr-runbook.md` の手動手順)。  
手動で行う場合は `gitops/manifests/prod/mailserver/replication-source.yaml` を参照。

VolSync の対象は `mailserver-data` PVC のみ。次の PVC は `ReplicationSource` の対象外で、再構築時に復元されない。

- `mailserver-state` (配送状態・fail2ban の BAN DB 等) と `mailserver-logs` (メールログ): 空の PVC として作り直される。BAN 状態は失われる。
- `mailserver-ops-reports` (`ops-reports@` の Maildir。DMARC・TLS-RPT の集約レポートを受ける): 空で作り直される。レポートは送信元が再送するため、新規分から再び溜まる。

### 運用ダッシュボードのデータ (collector の SQLite)

collector の `collector-data` PVC (SQLite) に保存する Falco 検知・認証イベント・DMARC・TLS-RPT・配送失敗は、再構築時の復元対象外 (バックアップなし)。DMARC・TLS-RPT は、`mailserver-ops-reports` に残っているレポートメールから mail-agent 経由で再取り込みされる (PVC ごと失った場合は上記のとおり再送待ち)。Falco 検知・認証イベント・配送失敗は再取得できない。

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
  (`user-patches.sh` が起動時に各部署の `dovecot-acl` に `authenticated` を書き出す)。
- **送信者認可**: `user-patches.sh` が `mua_sender_restrictions` を上書きする。From が ML アドレス(エイリアス含む)なら
  SASL 認証済みの誰でも許可、noreply は SASL ユーザー noreply のみ許可、それ以外(個人アドレス等)は拒否。
- **未登録の自ドメイン宛**: 25/587/465 すべてで RCPT TO 段階に `550 5.1.1` で拒否する(`user-patches.sh` が DMS 既定の
  submission/submissions の `smtpd_reject_unlisted_recipient=no` を `yes` に上書き)。受理すると LMTP で 451 のまま滞留する。
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

`recovery.sh` が Terraform apply の直前に自動で行う (対象ノード名一致 かつ offline のデバイスのみ、ID指定)。
手動で行う場合は `docs/dr-runbook.md` の手動フォールバックを参照。

---

## ランタイム侵入検知 (Falco + intrusion-response.yml)

- **Falco**: `gitops/apps/prod/falco.yaml` で DaemonSet としてデプロイ済み。falcosidekick 経由で `DISCORD_OPS_WEBHOOK_URL` に通知
- **自動 dispatch は行わない**: 過検知リスク（CNPG barman backup・mailserver 設定生成等の正常動作も検知しうる）と falcosidekick のペイロード非互換性のため、Falco アラートから `intrusion-response` への自動 dispatch は実装していない
- **手動 dispatch**: Falco 通知を受けた人間が侵害を判断し、`.github/workflows/intrusion-response.yml` を `workflow_dispatch` で手動発火する
- **intrusion-response.yml の動作**:
  1. forensics: `kubectl logs`・`kubectl get events`・`kubectl get networkpolicy` を GitHub Actions Artifacts に保存 (90日保持)
  2. isolate: 指定 namespace に egress/ingress 全拒否 NetworkPolicy を適用
  3. notify: Discord に全シークレット一覧とローテーション手順を通知
- **再構築は手動**: シークレットローテーション完了後に人間が `dr-recovery` を `workflow_dispatch` で起動し承認する。ローテーション前の再構築は行わない

---

## 参照先

- DR 手順全文: `docs/dr-runbook.md`
- 検出ワークフロー: `.github/workflows/dr-trigger.yml` / `.github/scripts/dr-trigger.sh`
- 自動復旧スクリプト: `.github/scripts/recovery.sh`
- 復旧ワークフロー: `.github/workflows/dr-recovery.yml`
- 侵入対応ワークフロー: `.github/workflows/intrusion-response.yml`
- CNPG 移行時の詳細知見: `.kiro/specs/single-node-migration/design.md` の「実装時の知見」セクション
- DR起動トリガー引き継ぎの設計判断: `.kiro/specs/observability-v2/design.md` の「DR Trigger」セクション
