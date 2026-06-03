# DR・運用の知見

このファイルは障害対応・DR・インフラ操作で必ず参照すべき注意事項をまとめたもの。
次の会話でも常に参照されること。

---

## DR の基本方針

- **自動復旧が前提**: Grafana Cloud → GitHub Actions (`.github/workflows/dr-recovery.yml`) が無人で復旧する
- **人手は復旧後確認のみ**: `docs/dr-runbook.md` の「復旧後の確認」セクションを参照
- **手動手順は例外**: ワークフローが失敗した場合のフォールバックとして `docs/dr-runbook.md` の「手動フォールバック」を使う
- **復旧スクリプト**: `.github/scripts/recovery.sh` (旧 `raspberry-pi/recovery/recovery.sh` から移動)

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

Directus は旧クラスターで WAL アーカイブが設定されていなかったため S3 に `wals/` ディレクトリが存在しない。  
**DR 時は `bootstrap.initdb` で空 DB 起動する**設計（`gitops/manifests/prod/directus/db-cluster.yaml` 参照）。  
コンテンツの再投入が必要になる。

---

## Stalwart の注意事項

### TLS 証明書 (`mail-tls`) のタイミング問題

DR 後、`stalwart-0` が `ContainerCreating` のまま止まる場合がある。  
原因: cert-manager の `Certificate` リソースが ArgoCD sync タイミングにより未適用。

```bash
# mail-tls が存在しない場合のみ実行
make kubectl ARGS="get certificate mail-tls -n prod" || \
make kubectl ARGS="apply \
  -f gitops/manifests/prod/stalwart/certificate.yaml \
  -f gitops/manifests/prod/stalwart/external-secret.yaml \
  -f gitops/manifests/prod/stalwart/restic-external-secret.yaml"
```

### VolSync リストアとアカウント ID の整合性

DR 時の Stalwart メールデータ復元は `recovery.sh` が自動で行う（ステップ 7）。  
手動で行う場合は `gitops/manifests/prod/stalwart/replication-destination.yaml` を apply する。

**重要**: VolSync バックアップが `settings.ndjson` 適用**前**の状態だと、DR 後にメールが消える。

原因: Stalwart v0.16 のアカウント ID は LDAP directory 設定に基づいて生成される。  
settings.ndjson の `destroy Directory` + `create Directory` でアカウント ID マッピングがリセットされ、  
旧 DB のメールデータが新アカウント ID からアクセスできなくなる。

**解決策**: VolSync バックアップは必ず settings.ndjson 適用済み・LDAP 接続確認済みの状態で取ること。  
`stalwart-cli query Account` で accounts が存在することを確認してからバックアップを信頼する。

### Stalwart v0.16 管理 API の認証

v0.16 で `STALWART_ADMIN_SECRET` は廃止。API キー (Bearer token) 方式に変更。  
API キーを失った場合の正式な復旧手順:

```bash
# 1. STALWART_RECOVERY_ADMIN を環境変数として StatefulSet に追加
kubectl create secret generic stalwart-recovery -n prod \
  --from-literal=credentials="admin:$(openssl rand -hex 16)"

# statefulset.yaml に以下を追加してデプロイ:
# - name: STALWART_RECOVERY_ADMIN
#   valueFrom:
#     secretKeyRef:
#       name: stalwart-recovery
#       key: credentials

# 2. recovery admin で settings.ndjson を適用
stalwart-cli --url https://mail.aramakisai.com \
  --user admin --password "<recovery-password>" \
  apply --file /tmp/stalwart-settings.ndjson

# 3. 完了後、STALWART_RECOVERY_ADMIN を削除（セキュリティ上必須）
```

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
- 自動復旧スクリプト: `.github/scripts/recovery.sh`
- 復旧ワークフロー: `.github/workflows/dr-recovery.yml`
- CNPG 移行時の詳細知見: `.kiro/specs/single-node-migration/design.md` の「実装時の知見」セクション
