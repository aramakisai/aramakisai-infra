# DR ランブック — シングルノード コールドスタンバイ復旧

## 概要

prod-node-1 (CX33) が障害になった場合、Grafana Cloud Alerting + GitHub Actions の  
自動復旧ワークフローが **人手なしで 30 分以内に復旧を完了する**。

人が行うのは **復旧後の確認のみ**。  
自動復旧が失敗した場合のみ、「手動フォールバック」セクションを参照する。

**目標復旧時間 (RTO)**: 30 分  
**目標復旧時点 (RPO)**: Authentik = 直前まで（CNPG WAL 連続アーカイブ）、Stalwart = 最大 2 時間、Directus = 直前まで（CNPG WAL 連続アーカイブ）

---

## 自動復旧フロー

```
Grafana Cloud Synthetic Monitoring
  → idp.aramakisai.com が 3 分間応答なし
  → Contact Point (Webhook) → POST https://api.github.com/repos/aramakisai/aramakisai-infra/dispatches
                               {"event_type": "dr-recovery"}

GitHub Actions (.github/workflows/dr-recovery.yml)
  1. Tailscale から prod-node-1 デバイスを削除
  2. Terraform Cloud API で CX33 prod-node-1 を再作成 (≈5分)
  3. Tailscale への prod-node-1 登録をポーリング (最大10分)
  4. ansible-playbook k3s-bootstrap.yml を実行 (≈10分)
       → K3s / Cilium / cloudflared / ArgoCD / infisical-auth / Deploy Key / App of Apps
  5. ArgoCD sync + CNPG healthy を待機 (最大20分)
       → ESO が Infisical から全シークレットを取得
       → CNPG が B2 から WAL リストア (Authentik, Directus)
  6. Stalwart を停止 → VolSync で B2 から stalwart-data をリストア → 再起動
```

---

## 復旧後の確認 (人手)

自動復旧完了後、以下を確認する。

### シークレット注入確認

過去に infisical-auth が空になり ESO 全停止した事例あり（2026-06-02 インシデント）。

```bash
# infisical-auth が空でないこと
make kubectl ARGS="get secret infisical-auth -n argocd \
  -o jsonpath='{.data.clientId}'" | base64 -d && echo

# ArgoCD Deploy Key が空でないこと
make kubectl ARGS="get secret aramakisai-infra-repo -n argocd \
  -o jsonpath='{.data.sshPrivateKey}'" | base64 -d | wc -c
```

いずれかが空の場合 → 「手動フォールバック: シークレット修復」を実施。

### サービス疎通確認

```bash
make kubectl ARGS="get applications -n argocd"
# → 全 Application が Synced / Healthy

make kubectl ARGS="top nodes"
# → メモリ使用率 60% 以下が目標

dig mail.aramakisai.com AAAA
# → 新ノードの IPv6 アドレスが返ること
```

| URL | 確認内容 |
|-----|---------|
| https://idp.aramakisai.com | ログイン成功 |
| https://api.aramakisai.com/admin | 画面表示（最新データ復旧済み）|
| https://webmail.aramakisai.com | 画面表示 |
| `https://argocd.aramakisai.com` | 管理画面表示 |

### 既知の想定内事象

| 事象 | 理由 | 対処 |
|------|------|------|
| Stalwart のメールが最大 2 時間分消失 | VolSync スナップショット間隔 | 許容範囲内 |
| Stalwart TLS (`mail-tls`) がない | ArgoCD sync のタイミング次第 | 下記を手動適用 |

```bash
# mail-tls が存在しない場合のみ実行
make kubectl ARGS="apply \
  -f gitops/manifests/prod/stalwart/certificate.yaml \
  -f gitops/manifests/prod/stalwart/external-secret.yaml \
  -f gitops/manifests/prod/stalwart/restic-external-secret.yaml"
```

---

## 手動フォールバック

自動復旧が失敗した場合のみ実施する。  
前提: `infisical login` 済み、`terraform login` 済み。

### ステップ 1: Tailscale デバイス削除

```bash
infisical run -- bash -c '
for HOST in prod-node-1; do
  ID=$(curl -sf \
    -H "Authorization: Bearer $TAILSCALE_API_KEY" \
    "https://api.tailscale.com/api/v2/tailnet/$TAILSCALE_TAILNET/devices" \
    | jq -r --arg h "$HOST" '"'"'.devices[] | select(.hostname == $h) | .id'"'"')
  [ -n "$ID" ] && curl -sf -X DELETE \
    -H "Authorization: Bearer $TAILSCALE_API_KEY" \
    "https://api.tailscale.com/api/v2/device/$ID" && echo "Deleted: $HOST"
done
'
```

### ステップ 2: Terraform

```bash
cd terraform
infisical run -- terraform apply -var-file="../secrets.tfvars" -auto-approve
cd ..
```

### ステップ 3: Ansible

```bash
infisical run -- ansible-playbook \
  -i ansible/inventory/tailscale.yml \
  ansible/playbooks/k3s-bootstrap.yml
```

### ステップ 4: シークレット修復 (必要な場合)

```bash
infisical run -- bash -c '
echo "$KUBECONFIG" > /tmp/kubeconfig-dr && chmod 600 /tmp/kubeconfig-dr

# infisical-auth 修復
kubectl --kubeconfig=/tmp/kubeconfig-dr \
  create secret generic infisical-auth \
  --from-literal=clientId="$INFISICAL_CLIENT_ID" \
  --from-literal=clientSecret="$INFISICAL_CLIENT_SECRET" \
  -n argocd --dry-run=client -o yaml \
  | kubectl --kubeconfig=/tmp/kubeconfig-dr apply -f -

# ESO 強制再同期
kubectl --kubeconfig=/tmp/kubeconfig-dr \
  annotate externalsecret --all -n prod \
  force-sync=$(date +%s) --overwrite
'
```

### ステップ 5: Stalwart VolSync リストア (手動)

```bash
make kubectl ARGS="scale statefulset stalwart -n prod --replicas=0"
make kubectl ARGS="apply -f gitops/manifests/prod/stalwart/replication-destination.yaml"
make kubectl ARGS="wait replicationdestination/stalwart-restore \
  -n prod --for=condition=Reconciled --timeout=30m"
make kubectl ARGS="scale statefulset stalwart -n prod --replicas=1"
make kubectl ARGS="delete -f gitops/manifests/prod/stalwart/replication-destination.yaml"
```

---

## GitHub Actions 復旧ワークフローの管理

```
復旧ログ: https://github.com/aramakisai/aramakisai-infra/actions/workflows/dr-recovery.yml

# 手動トリガー (テスト・強制実行)
curl -X POST \
  -H "Authorization: Bearer <GITHUB_PAT>" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  https://api.github.com/repos/aramakisai/aramakisai-infra/dispatches \
  -d '{"event_type":"dr-recovery"}'

# 同時実行制御: concurrency グループ "dr-recovery" で保護済み
# 前回のジョブが実行中の場合、新規トリガーはキューに入る (cancel-in-progress: false)
```

### Grafana Cloud Contact Point 設定

| 項目 | 値 |
|------|-----|
| URL | `https://api.github.com/repos/aramakisai/aramakisai-infra/dispatches` |
| HTTP Method | POST |
| Header: Authorization | `Bearer <GITHUB_PAT>` |
| Header: Accept | `application/vnd.github+json` |
| Header: X-GitHub-Api-Version | `2022-11-28` |
| Body | `{"event_type":"dr-recovery"}` |

**GITHUB_PAT**: Fine-Grained PAT (Actions: write 権限のみ) を Grafana Cloud の Contact Point に設定する。

### GitHub Actions Secrets (要設定)

| Secret 名 | 内容 |
|-----------|------|
| `INFISICAL_CLIENT_ID` | Infisical Machine Identity Client ID |
| `INFISICAL_CLIENT_SECRET` | Infisical Machine Identity Client Secret |
| `INFISICAL_PROJECT_ID` | Infisical プロジェクト ID |
| `TS_OAUTH_CLIENT_ID` | Tailscale OAuth Client ID (tag:ci 用) |
| `TS_OAUTH_SECRET` | Tailscale OAuth Client Secret (tag:ci 用) |

**Tailscale 前提**: Tailscale ACL に `tag:ci` タグを定義し、上記 OAuth Client がそのタグでデバイスを登録できること。
