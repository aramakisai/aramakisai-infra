# DR ランブック — シングルノード コールドスタンバイ復旧

## 概要

prod-node-1 (CX33) が障害になった場合に、Raspberry Pi の自動復旧サービスまたは手動で  
新しいノードを作成し、K3s + ArgoCD を再起動するまでの手順。

**目標復旧時間**: 30 分以内  
**データ損失許容**: CNPG は WAL 連続アーカイブのため直前まで復旧可。Stalwart は最大 2 時間分。

---

## 自動復旧フロー (Raspberry Pi)

Grafana Cloud が `idp.aramakisai.com` の死活監視で 3 分間応答なしを検知すると、  
Contact Point (Webhook) 経由で Pi の `POST /recover` を呼び出し、自動的に以下が実行される。

```
1. Tailscale から prod-node-1 デバイスを削除
2. Terraform Cloud API で CX33 prod-node-1 を再作成
3. Tailscale への prod-node-1 登録を最大 10 分ポーリング
4. ansible-playbook k3s-bootstrap.yml を実行
5. ArgoCD App of Apps が自動 sync → 全サービス起動
```

自動復旧後、以下の手動確認が必要（「移行後確認」セクション参照）。

---

## 手動復旧手順

### 前提条件

```bash
# Infisical にログイン済みであること
infisical login

# terraform ログイン済みであること
terraform login
```

すべてのシークレットは Infisical `prod` 環境で管理される。`.env` は参照しない。

---

### ステップ 1: Tailscale デバイス削除

`ephemeral: false` のため、Hetzner でノードが削除されても Tailscale デバイスが残存する。  
新ノードが `prod-node-1-1` として登録されるのを防ぐため、**terraform apply より前に**削除する。

```bash
infisical run -- bash -c '
for HOST in prod-node-1; do
  ID=$(curl -sf \
    -H "Authorization: Bearer $TAILSCALE_API_KEY" \
    "https://api.tailscale.com/api/v2/tailnet/$TAILSCALE_TAILNET/devices" \
    | jq -r --arg h "$HOST" '"'"'.devices[] | select(.hostname == $h) | .id'"'"')
  [ -n "$ID" ] && curl -sf -X DELETE \
    -H "Authorization: Bearer $TAILSCALE_API_KEY" \
    "https://api.tailscale.com/api/v2/device/$ID" && echo "Deleted: $HOST ($ID)"
done
'
```

---

### ステップ 2: Terraform でノード再作成

```bash
cd terraform
infisical run -- terraform apply -var-file="../secrets.tfvars" -auto-approve
```

**確認**: Hetzner Cloud コンソールで CX33 `prod-node-1` が作成され、  
Tailscale 管理コンソールで `prod-node-1`（サフィックスなし）として登録されていること。

---

### ステップ 3: Ansible でシングルノード K3s ブートストラップ

```bash
cd ..
infisical run -- ansible-playbook \
  -i ansible/inventory/tailscale.yml \
  ansible/playbooks/k3s-bootstrap.yml
```

**確認**:

```bash
make kubectl ARGS="get nodes"
# → prod-node-1   Ready   control-plane,etcd,master
```

---

### ステップ 4: ArgoCD sync・シークレット注入の確認

```bash
# infisical-auth Secret が空でないこと（空だと ESO 全停止）
make kubectl ARGS="get secret infisical-auth -n argocd \
  -o jsonpath='{.data.clientId}'" | base64 -d && echo

# ArgoCD GitHub Deploy Key が空でないこと
make kubectl ARGS="get secret aramakisai-infra-repo -n argocd \
  -o jsonpath='{.data.sshPrivateKey}'" | base64 -d | wc -c

# 全 Application が Synced/Healthy になるまで待機
make kubectl ARGS="get applications -n argocd"
```

いずれかが空の場合:

```bash
# infisical-auth を手動 patch
infisical run -- bash -c '
kubectl --kubeconfig=/tmp/kubeconfig-aramakisai \
  create secret generic infisical-auth \
  --from-literal=clientId="$INFISICAL_CLIENT_ID" \
  --from-literal=clientSecret="$INFISICAL_CLIENT_SECRET" \
  -n argocd --dry-run=client -o yaml | kubectl apply -f -
'
# ESO を強制再同期
make kubectl ARGS="annotate externalsecret --all -n prod \
  force-sync=$(date +%s) --overwrite"
```

---

### ステップ 5: CNPG DB 確認

CNPG は `bootstrap.recovery` + `skipEmptyWalArchiveCheck: enabled` により  
B2 の最新 WAL から自動リストアされる。通常は人手不要。

```bash
# Authentik DB の状態確認
make kubectl ARGS="get cluster authentik-db -n prod"
# → STATUS: Cluster in healthy state

# Directus DB の状態確認
make kubectl ARGS="get cluster directus-db -n prod"
# → STATUS: Cluster in healthy state
```

⚠️ **注意**: Directus は WAL アーカイブなし（initdb 起動）のため、  
DR 後は空の DB で起動する。コンテンツの再投入が必要。

---

### ステップ 6: Stalwart メールデータを VolSync でリストア

PVC は新ノードで空の状態で作成される。B2 restic から復元する。

```bash
# Stalwart を停止
make kubectl ARGS="scale statefulset stalwart -n prod --replicas=0"

# ReplicationDestination を適用してリストア開始
make kubectl ARGS="apply -f gitops/manifests/prod/stalwart/replication-destination.yaml"

# リストア完了を待機（最大 30 分）
make kubectl ARGS="wait replicationdestination/stalwart-restore \
  -n prod --for=condition=Reconciled --timeout=30m"

# Stalwart を再起動
make kubectl ARGS="scale statefulset stalwart -n prod --replicas=1"

# リストア用リソースを削除
make kubectl ARGS="delete -f gitops/manifests/prod/stalwart/replication-destination.yaml"
```

---

### ステップ 7: cert-manager Certificate の確認

Stalwart の TLS 証明書 (`mail-tls`) が存在しない場合は手動適用する。

```bash
make kubectl ARGS="get certificate mail-tls -n prod"

# 存在しない場合
make kubectl ARGS="apply \
  -f gitops/manifests/prod/stalwart/certificate.yaml \
  -f gitops/manifests/prod/stalwart/external-secret.yaml \
  -f gitops/manifests/prod/stalwart/restic-external-secret.yaml"
```

---

## 移行後確認チェックリスト

```bash
# ノードリソース確認（メモリ使用率 60% 以下が目標）
make kubectl ARGS="top nodes"

# 全 Application 確認
make kubectl ARGS="get applications -n argocd"

# DNS 確認（新ノードの IPv6 が返ること）
dig mail.aramakisai.com AAAA

# CNPG バックアップ確認
make kubectl ARGS="get scheduledbackup -n prod"
```

サービス疎通確認:
- Authentik: `https://idp.aramakisai.com` → ログイン
- Directus: `https://api.aramakisai.com/admin` → 管理画面
- Roundcube: `https://webmail.aramakisai.com` → 画面表示
- ArgoCD: `https://argocd.aramakisai.com` → 管理画面

---

## 既知の制約・注意事項

| 項目 | 内容 |
|------|------|
| Directus WAL | 旧クラスターで WAL アーカイブが未設定のため DR 後は空 DB 起動 |
| Stalwart | VolSync は 2 時間ごとのスナップショットのため最大 2 時間分のメールが消失する可能性あり |
| Stalwart TLS | cert-manager が `mail-tls` を自動発行するが、ArgoCD の sync タイミングにより手動適用が必要な場合あり |
| CNPG イメージ | `postgresql:16.8` を使用。`skipEmptyWalArchiveCheck: enabled` でアーカイブチェックをスキップ |
| Tailscale デバイス | `ephemeral: false` のため terraform apply 前に手動削除が必須 |
