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

### Authentik LDAP 連携でのメール認証・送信トラブル (2026-06-23 解決)

個人ユーザーのメール送受信は方針上禁止（ML専用、`mailListAddress=true` のみ受信可）。
例外は `noreply`（システム通知送信元）のみ。この前提を崩さずに2つの不具合を修正した。

1. **SMTP認証が全ユーザーで `535 Authentication failed`（パスワードは常に正しい）**
   - 原因: Authentik 2024.8+ で LDAP 全件検索には RBAC 権限 `search_full_directory`
     （旧 `search_group` 設定の後継）が必要になった。`mailserver-service` に未割当だと
     bind したユーザー自身のエントリしか検索できず、dovecot の DN ルックアップが失敗する。
   - 修正: `terraform/authentik_ldap.tf` の `authentik_rbac_role`/`authentik_rbac_permission_role`
     を `mailserver_ldap_search` として付与（LDAP Outpost再起動で `search_mode=cached` のキャッシュをflush）。

2. **SMTP認証成功後に `553 Sender address rejected: not owned by user`**
   - 原因: docker-mailserver は `LDAP_QUERY_FILTER_SENDERS` を設定すると USER/ALIAS/GROUP
     フィルタの自動OR結合を無効化し、SENDERSフィルタのみが送信者認可チェックになる仕様。
     既存値 `(&(objectClass=group)(mail=%s))` は ML(group)宛のなりすまし防止用で、
     個人ユーザー（`noreply`含む）は構造的に一切マッチしなかった。
   - 修正: `gitops/manifests/prod/mailserver/statefulset.yaml` の `LDAP_QUERY_FILTER_SENDERS` を
     `(|(&(objectClass=group)(mail=%s))(&(objectClass=inetOrgPerson)(mail=%s)(cn=noreply)))` に変更。
     `cn=noreply` で個人ユーザー全体ではなく `noreply` のみに例外を限定（方針を維持）。
   - **再発時の確認手順**: `authentik-worker` ログで `SMTPRecipientsRefused`/`exc_type` を grep。
     SMTP認証(`235`)は通るが送信が失敗する場合は、認証問題ではなく Postfix の
     `reject_authenticated_sender_login_mismatch`（`SPOOF_PROTECTION=1` 由来）を疑う。

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

## 参照先

- DR 手順全文: `docs/dr-runbook.md`
- 検出ワークフロー: `.github/workflows/dr-trigger.yml` / `.github/scripts/dr-trigger.sh`
- 自動復旧スクリプト: `.github/scripts/recovery.sh`
- 復旧ワークフロー: `.github/workflows/dr-recovery.yml`
- CNPG 移行時の詳細知見: `.kiro/specs/single-node-migration/design.md` の「実装時の知見」セクション
- DR起動トリガー引き継ぎの設計判断: `.kiro/specs/observability-v2/design.md` の「DR Trigger」セクション
