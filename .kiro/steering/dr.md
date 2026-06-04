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

### Stalwart v0.16 管理 API の認証 と PostSync Job 障害

#### 根本原因（繰り返し発生する既知の障害）

`stalwart-settings-apply` PostSync Job が HTTP 401 で失敗するパターンが 2 つある。

**パターン A: STALWART_API_KEY が DB に存在しない**

- Stalwart の API キーは Infisical に保存されているだけでは機能しない。Stalwart の DB に明示的に作成する必要がある
- DB がリセット・初期化されると API キーも消える（v0.16.6 へのアップグレード時に発生した）
- Job ログ: `error: authentication failed (HTTP 401)`

**パターン B: Authentication.directoryId = authentik-oidc のときは --user/--password も通らない**

- `settings-update.ndjson` で `Authentication.directoryId` が `authentik-oidc` に設定されると、HTTP API の認証もすべて OIDC バックエンドに回される
- OIDC バックエンドはユーザー名/パスワード形式を受け付けないため `--user admin --password` も 401 になる
- Stalwart ログ: `reason = "Unsupported credentials type for OIDC backend"`
- **つまり: API キーを失ったら管理者パスワードでも入れない。Recovery mode 必須**

#### 診断コマンド

```bash
# 1. Job の失敗確認
make kubectl ARGS="get application stalwart -n argocd -o json" | \
  jq '.status.operationState.syncResult.resources[] | select(.hookPhase=="Failed")'

# 2. Stalwart ログで認証エラーを確認
make kubectl ARGS="logs -n prod stalwart-0 --since=10m" | grep -i "auth\|401"

# 3. API キーの疎通テスト (debug job を使う)
# /tmp/stalwart-debug-job.yaml を参照
```

#### 復旧手順: Recovery mode で API キーを再作成する

```bash
# 1. Recovery admin 用パスワードを生成して Secret に登録
RECOVERY_PASS=$(openssl rand -hex 16)
make kubectl ARGS="create secret generic stalwart-recovery -n prod \
  --from-literal=credentials=admin:${RECOVERY_PASS} --dry-run=client -o yaml" | \
  make kubectl ARGS="apply -f -"

# 2. statefulset.yaml に STALWART_RECOVERY_ADMIN を追加してコミット → ArgoCD sync
# spec.template.spec.containers[0].env に追記:
#   - name: STALWART_RECOVERY_ADMIN
#     valueFrom:
#       secretKeyRef:
#         name: stalwart-recovery
#         key: credentials

# 3. port-forward して recovery admin で API キーを作成
make kubectl ARGS="port-forward -n prod pod/stalwart-0 8080:8080" &
stalwart-cli --url http://localhost:8080 \
  --user admin --password "$RECOVERY_PASS" \
  apply --stdin <<'EOF'
{"@type":"create","object":"ApiKey","value":{"api-key-job":{"description":"PostSync Job 用","roles":["superuser"],"secret":"<STALWART_API_KEY の値>"}}}
EOF

# 4. STALWART_RECOVERY_ADMIN を statefulset.yaml から削除してコミット → ArgoCD sync
# 5. stalwart-recovery Secret を削除
make kubectl ARGS="delete secret stalwart-recovery -n prod"
```

**注意**: `<STALWART_API_KEY の値>` は Infisical の `STALWART_API_KEY` の値をそのまま使う。  
API キーの値自体は変えない（Infisical に保存済みの値と一致させる必要がある）。

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
