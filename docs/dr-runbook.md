# DR ランブック — シングルノード コールドスタンバイ復旧

## 概要

prod-node-1 (CX33) が障害になった場合、クラスター外部で完結する複合検出ワークフロー
(`dr-trigger.yml`) + GitHub Actions の自動復旧ワークフローが
**人手なしで 30 分以内に復旧を完了する**。

人が行うのは **復旧後の確認、または猶予期間中の誤検知時の中止操作のみ**。  
自動復旧が失敗した場合のみ、「手動フォールバック」セクションを参照する。

**目標復旧時間 (RTO)**: 30 分  
**目標復旧時点 (RPO)**: Authentik = 直前まで（CNPG WAL 連続アーカイブ）、Stalwart = 最大 2 時間、Directus DB = 直前まで（CNPG WAL 連続アーカイブ）、Directus アセット = 直前まで（Hetzner OS への直接保存のため復元待機不要、コンテナ起動と同時に接続）

---

## 自動復旧フロー

旧構成は Grafana Cloud Synthetic Monitoring (idp 単体の応答なしのみを判定条件) を起点としていたが、
Grafana Cloud 解約に伴い `dr-trigger.yml` (クラスター外部・GitHub Actions 完結) に引き継いだ。
**idp 単体障害ではノード再作成を起こさない複合検出ロジック**と、
**誤検知時に運用者が止められる「通知 + 猶予期間オプトアウト」方式**を採用している。

```
.github/workflows/dr-trigger.yml (5分毎 cron + workflow_dispatch)
  → .github/scripts/dr-trigger.sh が複合検出を実施
       (a) Tailscale Devices API で prod-node-1 の connectedToControl を確認
       (b) idp / argocd / webmail への HTTPS 到達性を確認 (タイムアウト+リトライ込み)

  判定:
    - (a) がオフライン、または (b) で2つ以上が同時に応答なし
        → ノード障害 (NodeFailureSuspected)
    - (b) で1つのみ応答なし、(a) はオンライン
        → 単体サービス障害 (SingleEndpointDown) — Discord 通知のみ、ノード再作成はしない

  ノード障害と判定した場合:
    1. Discord へ即座に障害検知通知を送信
    2. ラベル `dr-incident` の GitHub Issue を作成 (= 猶予期間の開始、既定10分)
    3. 猶予期間中に OWNER/MEMBER/COLLABORATOR 権限を持つ運用者が
       Issue へ `abort`/`中止` を含むコメントを付ける、または Issue をクローズすると中止
    4. 猶予期間が経過しても中止操作がない場合、無人でも自動で
       repository_dispatch (event_type: dr-recovery) を発火し Issue をクローズ

GitHub Actions (.github/workflows/dr-recovery.yml) ← repository_dispatch (dr-recovery) で起動
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

5分毎の cron tick で再評価するため、検知から dispatch 発火までの実時間は最大 15 分程度になる
(猶予期間 10 分 + cron 間隔 5 分)。`dr-recovery.yml` 自体の入力契約・内部ロジックは変更していない。

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
infisical run -- terraform apply -auto-approve
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
検出ログ: https://github.com/aramakisai/aramakisai-infra/actions/workflows/dr-trigger.yml
復旧ログ: https://github.com/aramakisai/aramakisai-infra/actions/workflows/dr-recovery.yml

# dr-trigger.yml の手動トリガー (テスト・強制実行)
gh workflow run dr-trigger.yml --repo aramakisai/aramakisai-infra

# dr-recovery.yml への直接 dispatch (dr-trigger.yml をバイパスして強制実行する場合のみ)
gh api repos/aramakisai/aramakisai-infra/dispatches -f event_type=dr-recovery

# 同時実行制御: dr-trigger は concurrency グループ "dr-trigger"、
# dr-recovery は "dr-recovery" で保護済み (cancel-in-progress: false)
```

### dr-trigger.yml の運用

```
ノード障害疑いの猶予期間中: https://github.com/aramakisai/aramakisai-infra/issues?q=label:dr-incident
  → 誤検知の場合は OWNER/MEMBER/COLLABORATOR 権限を持つアカウントで
    `abort` または `中止` を含むコメントを付ける (Issue を直接クローズしても中止扱いになる)
  → 権限を持たないアカウント (author_association が NONE 等) のコメントは無視される

猶予期間 (既定10分) の変更:
  .github/scripts/dr-trigger.sh の DR_TRIGGER_GRACE_MINUTES (デフォルト値) を変更してコミットする
  (workflow_dispatch 実行時のみ環境変数で一時的に上書きすることも可能)

監視対象エンドポイントの変更:
  .github/scripts/dr-trigger.sh の ENDPOINTS 配列を編集する
```

### GitHub Actions Secrets (要設定)

| Secret 名 | 内容 | 使用ワークフロー |
|-----------|------|------------------|
| `TAILSCALE_API_KEY` | Tailscale API キー (Infisical の既存キーをミラー) | dr-trigger.yml |
| `TAILSCALE_TAILNET` | Tailscale tailnet 名 (Infisical の既存キーをミラー) | dr-trigger.yml |
| `DISCORD_OPS_WEBHOOK_URL` | 運用通知用 Discord Webhook (Infisical の既存キーをミラー) | dr-trigger.yml |
| `INFISICAL_CLIENT_ID` | Infisical Machine Identity Client ID | dr-recovery.yml |
| `INFISICAL_CLIENT_SECRET` | Infisical Machine Identity Client Secret | dr-recovery.yml |
| `INFISICAL_PROJECT_ID` | Infisical プロジェクト ID | dr-recovery.yml |
| `TS_OAUTH_CLIENT_ID` | Tailscale OAuth Client ID (tag:ci 用) | dr-recovery.yml |
| `TS_OAUTH_SECRET` | Tailscale OAuth Client Secret (tag:ci 用) | dr-recovery.yml |

**dr-trigger.yml の `GITHUB_TOKEN`**: 新規 PAT は不要。`repository_dispatch` 発火には
`contents: write`、Issue 操作には `issues: write` をワークフロー内の `permissions` で明示している
(`GITHUB_TOKEN` は同一リポジトリへの `repository_dispatch`/`workflow_dispatch` に関して
再帰防止ルールの例外として使用できる)。

**Tailscale 前提 (dr-recovery.yml)**: Tailscale ACL に `tag:ci` タグを定義し、
上記 OAuth Client がそのタグでデバイスを登録できること。

**60日非活動による無効化に関する注意**: GitHub Actions の scheduled workflow は
リポジトリに60日間コミット等の活動がないと自動的に無効化される。本リポジトリは継続的に
開発中のため現状リスクは低いが、定期的に Actions タブで `dr-trigger.yml` が有効なままか
目視確認することを推奨する (自動化対象外)。
