# DR ランブック — シングルノード コールドスタンバイ復旧

## 概要

検知は通知のみ、復旧は人が承認して実行する。

- `dr-trigger.yml` (5分毎 cron) がノード障害を判定し、Discord 通知と `dr-incident` Issue の起票/追記だけを行う。復旧ワークフローは自動起動しない。
- 復旧は `dr-recovery.yml` を人が `workflow_dispatch` で起動し、GitHub Environment `dr-recovery` の required reviewers が承認すると始まる。
- `recovery.sh` は冒頭で読み取り専用の生存確認ゲートを通し、ノードが生きている兆候が1つでもあれば何も変更せず停止する。
- 自動化の対象は **クラスター唯一のノード (prod-node-1) を喪失した単一ノード構成** のみ。他に Hetzner サーバーが残っている構成 (ノード追加期間中など) や prod-node-1 以外が対象の場合は、etcd 分断を避けるため停止し、手動手順に委ねる。

**目標復旧時間 (RTO)**: 30 分 (承認待ちを除く)
**目標復旧時点 (RPO)**: Authentik = 直前まで（CNPG WAL 連続アーカイブ）、Zitadel / Directus DB = 直前まで（同左）、Docker Mailserver = 最大 6 時間（VolSync スナップショット間隔）

---

## 検知 (dr-trigger)

```
.github/workflows/dr-trigger.yml (5分毎 cron + workflow_dispatch)
  → .github/scripts/dr-trigger.sh
       (a) Tailscale Devices API で hostname が prod-node-N のデバイス (実在ノード) の接続状態を集約
            全接続=正常 / 一部切断でクォーラム (過半数) 維持=NodeDegraded / 全断・クォーラム喪失・未検出=障害
       (b) idp / argocd / webmail への HTTPS 到達性 (タイムアウト+リトライ込み)

  判定:
    - (a) が障害、または (b) で2つ以上が同時に応答なし → NodeFailureSuspected
    - (b) で1つのみ応答なし、または (a) が NodeDegraded → 通知のみ (毎時1回に間引き)
  NodeFailureSuspected は1回の実行内で3回連続 (30秒間隔) したときだけ障害として扱う。
    1. open な `dr-incident` Issue が無ければ起票し Discord へ通知
    2. open な Issue があれば追記のみ (毎時1回の再通知)。重複起票しない
    3. 全シグナルが正常に戻ると Issue を自動クローズ
```

Tailscale は非 ephemeral のため、停止済みの旧デバイスが残ると実在ノード数に数えられクォーラム判定が厳しめ (誤検知側) に出る。再作成後は旧デバイスを削除しておく。

---

## 復旧の実行 (dr-recovery)

### 事前確認

1. `dr-incident` Issue と Discord の通知内容を確認し、ノードが本当に失われていることを確認する。
2. 侵入が疑われる場合は先に「侵入対応」に従いシークレットをローテーションする。

### 起動

```bash
gh workflow run dr-recovery.yml --repo aramakisai/aramakisai-infra -f target_node=prod-node-1
# 生存確認ゲートを上書きする場合 (ノードが生きていないことを人が確認した場合のみ)
#   -f force=true
# mailserver データを VolSync スナップショットからリストアする場合 (最大6時間前の状態になる)
#   -f restore_mail=true
```

Actions の実行画面で reviewer が **Review deployments** から承認すると、ジョブが始まる。

| 入力 | 既定 | 意味 |
|------|------|------|
| `target_node` | (必須) | 復旧対象。terraform `local.nodes` に定義済みで、inventory の cluster-init ホストであること |
| `force` | false | 生存確認ゲートの上書き。サーバーが稼働中の場合は Terraform・電源操作を行わず Ansible から再実行する |
| `restore_mail` | false | mailserver-data の VolSync リストア。`dr.aramakisai.com/restored-at` annotation が付いた PVC は再実行でも上書きしない |

### 処理の流れ

```
0. 進捗記録: open な dr-incident Issue (無ければ作成) に各段階と TFC run ID を追記
1. 生存確認ゲート (読み取り専用)。次のいずれかで停止 (force でのみ上書き)
     - Hetzner のサーバー状態が off / 不在以外 (running, starting など)、または取得失敗
     - Tailscale 上で対象ノードが接続中、または API 失敗
     - 公開エンドポイントのどれかが応答
     - kubectl get nodes が成功
2. 他に Hetzner サーバー (role=server) が残っている、または対象が cluster-init ホストでなければ停止
3. サーバー状態で分岐
     不在: plan 作成 (-target=対象ノードのみ, auto-apply 無効)
           → plan の変更が「対象サーバー作成 + Tailscale auth key 置換」だけか機械検査
              (placement group・DNS・RDNS・他ノードの変更が混ざれば run を discard して停止)
           → Tailscale の旧デバイス (名前一致 かつ offline) を ID 指定で削除 → apply
     停止: 電源投入 (Tailscale デバイスは消さない。消すと再接続できなくなる)
4. 対象ノードが Tailscale に接続するまで待機 (最大10分)
5. ansible-playbook k3s-bootstrap.yml を対象ノードに限定して実行
     (cluster-init は空の etcd から作り直す。etcd スナップショットは取得していない)
6. ArgoCD 全 Application の Healthy と、稼働中の全 CNPG クラスターの healthy を待機 (タイムアウトは失敗)
     instances=0 や hibernation 中のクラスターは対象外
7. infisical-auth / Deploy Key の空チェックと自己修復、mail-tls の自己修復
8. restore_mail=true かつ未リストアの PVC のみ: mailserver 停止 → VolSync リストア → 再起動
```

CNPG は Hetzner Object Storage から WAL リストアされる (Authentik / Directus は `bootstrap.recovery`、Zitadel も同様)。

### 途中で失敗した場合

Issue の進捗記録で失敗した段階と TFC run ID を確認する。原因を取り除いたら再度ワークフローを起動する。サーバーが既に作成されている場合、生存確認ゲートが Hetzner の running で停止するため、状態を確認した上で `force=true` で再実行する (各段階は再実行しても安全な作りで、Ansible は対象ノードのみ、メールのリストアは未リストアの PVC のみ)。

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
| mailserver のメールが最大 6 時間分消失 | VolSync スナップショット間隔 | 許容範囲内 |
| infisical-auth / Deploy Key が空 | ArgoCD sync タイミング問題 | **自動修復済み** (recovery.sh) |
| mailserver TLS (`mail-tls`) がない | cert-manager sync タイミング問題 | **自動修復済み** (recovery.sh) |
| `tailscale_tailnet_key.k3s_nodes` の置換が plan に出る | auth key は expiry 1時間の設計 | plan 検査で許可済み |

---

## 手動フォールバック

自動復旧が失敗した場合のみ実施する。
前提: `infisical login` 済み、`terraform login` 済み。ワークフローと同じ安全側の手順で行う。

### ステップ 1: 状態確認

```bash
infisical run --env=prod -- bash -c '
curl -sf -H "Authorization: Bearer $HCLOUD_TOKEN" "https://api.hetzner.cloud/v1/servers?name=prod-node-1" | jq ".servers[] | {name, status}"
'
```

サーバーが `off` なら Hetzner コンソールで電源投入するだけでよい (Tailscale デバイスは消さない)。

### ステップ 2: Tailscale 旧デバイス削除 (サーバーが不在のときのみ)

OAuth クライアント (devices:core 書込) でアクセストークンを取得し、対象名一致 かつ offline のデバイスを ID 指定で削除する。

```bash
infisical run --env=prod -- bash -c '
TOKEN=$(curl -sf -X POST https://api.tailscale.com/api/v2/oauth/token \
  -d "client_id=$TAILSCALE_OAUTH_CLIENT_ID" -d "client_secret=$TAILSCALE_OAUTH_CLIENT_SECRET" | jq -r .access_token)
curl -sf -H "Authorization: Bearer $TOKEN" \
  "https://api.tailscale.com/api/v2/tailnet/$TAILSCALE_TAILNET/devices" \
  | jq -r ".devices[] | select(.hostname | test(\"^prod-node-1(-[0-9]+)?$\")) | select(.connectedToControl != true) | .id"
'
# 出力された ID を確認し、デバイスごとに DELETE https://api.tailscale.com/api/v2/device/<id>
```

### ステップ 3: Terraform (対象ノードのみ)

```bash
cd terraform
infisical run --env=prod -- terraform plan -target='hcloud_server.nodes["prod-node-1"]'
# plan が「prod-node-1 の作成」と tailscale_tailnet_key.k3s_nodes の置換だけであることを確認してから
infisical run --env=prod -- terraform apply -target='hcloud_server.nodes["prod-node-1"]'
cd ..
```

`terraform destroy -target` は使わない (メール用 DNS レコード・RDNS を巻き込む)。

### ステップ 4: Ansible

```bash
infisical run --env=prod -- ansible-playbook \
  -i ansible/inventory/tailscale.yml --limit prod-node-1 \
  ansible/playbooks/k3s-bootstrap.yml
```

### ステップ 5: シークレット修復 (必要な場合)

```bash
infisical run --env=prod -- bash -c '
echo "$KUBECONFIG" > /tmp/kubeconfig-dr && chmod 600 /tmp/kubeconfig-dr

kubectl --kubeconfig=/tmp/kubeconfig-dr \
  create secret generic infisical-auth \
  --from-literal=clientId="$INFISICAL_CLIENT_ID" \
  --from-literal=clientSecret="$INFISICAL_CLIENT_SECRET" \
  -n argocd --dry-run=client -o yaml \
  | kubectl --kubeconfig=/tmp/kubeconfig-dr apply -f -

kubectl --kubeconfig=/tmp/kubeconfig-dr \
  annotate externalsecret --all -A \
  force-sync=$(date +%s) --overwrite
'
```

### ステップ 6: mailserver VolSync リストア (必要な場合のみ)

PVC に `dr.aramakisai.com/restored-at` annotation がある場合は既にリストア済みのため実施しない。

```bash
make kubectl ARGS="scale statefulset mailserver -n prod --replicas=0"
make kubectl ARGS="apply -f - <<'EOF'
apiVersion: volsync.backube/v1alpha1
kind: ReplicationDestination
metadata:
  name: mailserver-restore
  namespace: prod
spec:
  trigger:
    manual: dr-manual-$(date +%Y%m%dT%H%M%S)
  restic:
    repository: mailserver-restic-secret
    destinationPVC: mailserver-data
    copyMethod: Direct
    moverSecurityContext:
      runAsUser: 0
      runAsGroup: 0
      fsGroup: 0
EOF"
make kubectl ARGS="wait replicationdestination/mailserver-restore \
  -n prod --for=condition=Reconciled --timeout=30m"
make kubectl ARGS="annotate pvc mailserver-data -n prod dr.aramakisai.com/restored-at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
make kubectl ARGS="scale statefulset mailserver -n prod --replicas=1"
make kubectl ARGS="delete replicationdestination mailserver-restore -n prod"
```

### 複数ノード構成 (ノード追加期間中) の復旧

残存 etcd メンバーがいる場合、自動復旧は停止する。クォーラムが生きていれば、障害ノードを cluster から外して再作成し join する。クォーラムが失われている場合は残存メンバーで etcd を `--cluster-reset` してから join する。どちらも `k3s-bootstrap.yml` を使い、`k3s_cluster_init` が付いたホストを再実行しないこと (etcd が分断される)。

---

## 侵入対応 (intrusion-response.yml)

Falco が Discord にアラートを通知した場合、人間が侵害を判断して手動で実行するワークフロー。  
自動 dispatch は行わない（過検知リスク・シークレットローテーション前の再構築防止のため）。

### 実行手順

1. Falco の Discord 通知を確認し、侵害の可能性を判断する
2. GitHub Actions で `intrusion-response.yml` を手動 dispatch する

```bash
gh workflow run intrusion-response.yml \
  --repo aramakisai/aramakisai-infra \
  -f namespace=prod \
  -f pod_selector=""
```

または GitHub Actions UI から: Actions → Intrusion Response → Run workflow → namespace を入力

3. ワークフローが実行されると以下が自動で行われる:
   - **forensics**: Pod ログ・Events・NetworkPolicy を Artifacts として保存 (90日保持)
   - **isolate**: 対象 namespace の全 Pod に対して egress/ingress 全拒否 NetworkPolicy を適用
   - **notify**: Discord に「ローテーション必須シークレット一覧」と次のアクションを通知

4. Discord 通知を受け取ったら **すべてのシークレットをローテーション**する:
   - Infisical 管理コンソールで全シークレットを新しい値に更新
   - GitHub Actions Secrets の `INFISICAL_CLIENT_ID`/`INFISICAL_CLIENT_SECRET` もローテーション
   - ローテーション完了を確認する

5. ローテーション完了後に **`dr-recovery` を手動で起動し、reviewer の承認を得て** 再構築する:

```bash
gh workflow run dr-recovery.yml --repo aramakisai/aramakisai-infra -f target_node=prod-node-1
```

> ⚠️ **シークレットローテーション前に `dr-recovery` を実行しないこと**。  
> ローテーション前に再構築すると新ノードも即座に危険にさらされる。

---

## 計画的メンテナンス時の通知抑止

ホストOSの自動更新 (`ansible/roles/os-auto-update/` による毎日03:30の再起動)、K3s アップグレード (`.github/workflows/k3s-upgrade.yml`)、手動メンテナンスでノードが一時的に応答しなくなると、`dr-trigger.yml` が障害を通知する。復旧は自動起動しないため実害はなく、`dr-incident` Issue は正常化すると自動クローズされる。復旧ワークフローは冒頭の生存確認ゲートで、生きているノードに対しては何も変更せず停止する。

通知自体を止めたい長時間のメンテナンスでは、作業前後で `dr-trigger.yml` を無効化・再有効化する。

```bash
gh workflow disable dr-trigger.yml --repo aramakisai/aramakisai-infra
gh workflow enable dr-trigger.yml --repo aramakisai/aramakisai-infra
```

---

## GitHub Actions ワークフローの管理

```
検出ログ: https://github.com/aramakisai/aramakisai-infra/actions/workflows/dr-trigger.yml
復旧ログ: https://github.com/aramakisai/aramakisai-infra/actions/workflows/dr-recovery.yml
障害 Issue: https://github.com/aramakisai/aramakisai-infra/issues?q=label:dr-incident

# dr-trigger.yml の手動実行 (動作確認)
gh workflow run dr-trigger.yml --repo aramakisai/aramakisai-infra

# 同時実行制御: dr-trigger は concurrency グループ "dr-trigger"、
# dr-recovery は "dr-recovery" で保護 (cancel-in-progress: false)
```

監視対象エンドポイントの変更は `.github/scripts/dr-trigger.sh` の `ENDPOINTS` 配列を編集する。連続判定回数・間隔は `DR_TRIGGER_PROBES` / `DR_TRIGGER_PROBE_INTERVAL` (既定 3 回・30 秒)。

### GitHub Environment `dr-recovery` (リポジトリ設定、手動作成)

リポジトリ設定は Terraform 管理外 (`terraform/` に github provider は無い) のため、管理者が手動で作成する。

1. Settings → Environments → New environment → 名前 `dr-recovery`
2. **Required reviewers** に承認者 (1名以上) を追加する
3. 必要に応じて **Prevent self-review** を有効化する
4. Deployment branches は `main` のみに制限する

Environment が無いまま実行すると GitHub が自動作成して承認なしで実行してしまうため、先に作成しておくこと。

### Tailscale OAuth クライアント (TAILSCALE_OAUTH_CLIENT_ID / SECRET)

Terraform provider・dr-trigger・recovery.sh は同じキー名の OAuth クライアントを使う。復旧でデバイスを削除するため、書込権限を持つクライアント1つに統一する。

| スコープ | 用途 |
|----------|------|
| `devices:core` (Read + Write) | デバイス一覧 (dr-trigger / recovery)、旧デバイス削除 (recovery) |
| `auth_keys` (Read + Write) | Terraform `tailscale_tailnet_key` の発行 (タグ `tag:k3s-node` を指定) |

ACL を Terraform で管理する場合は `policy_file` スコープも必要。Admin console の Settings → OAuth clients で作成し、値を Infisical (`prod`) の同名キーへ投入する。`tailscale_oauth_client` による Terraform 管理は、provider 自身の認証に使うクライアントを自身で作る鶏卵問題があり、発行されたシークレットが state に残るため採用していない (最初の1つは手動作成が必須)。

### GitHub Actions Secrets (要設定)

その他の認証情報は Infisical から注入する。ワークフローが直接参照する GitHub Secrets は次のとおり。

| Secret 名 | 内容 | 使用ワークフロー |
|-----------|------|------------------|
| `INFISICAL_CLIENT_ID` | Infisical Machine Identity Client ID | dr-trigger.yml / dr-recovery.yml |
| `INFISICAL_CLIENT_SECRET` | Infisical Machine Identity Client Secret | dr-trigger.yml / dr-recovery.yml |
| `INFISICAL_PROJECT_ID` | Infisical プロジェクト ID | dr-trigger.yml / dr-recovery.yml |
| `TS_OAUTH_CLIENT_ID` | Tailscale OAuth Client ID (tag:ci 用、ランナーの tailnet 参加) | dr-recovery.yml |
| `TS_OAUTH_SECRET` | Tailscale OAuth Client Secret (tag:ci 用) | dr-recovery.yml |

Infisical (`prod`) から注入する主なキー: `HCLOUD_TOKEN` (Hetzner サーバー状態の確認・電源投入)、`TAILSCALE_OAUTH_CLIENT_ID/SECRET`、`TAILSCALE_TAILNET`、`TFC_API_TOKEN`、`TFC_WORKSPACE_ID`、`DISCORD_OPS_WEBHOOK_URL`、`K3S_TOKEN` ほか Ansible 用。

`GITHUB_TOKEN` は Issue 操作のため `issues: write` を各ワークフローの `permissions` で付与している。新規 PAT は不要。

**Tailscale 前提 (dr-recovery.yml)**: Tailscale ACL に `tag:ci` タグを定義し、`TS_OAUTH_*` のクライアントがそのタグでデバイスを登録できること。

**60日非活動による無効化に関する注意**: GitHub Actions の scheduled workflow はリポジトリに60日間コミット等の活動がないと自動的に無効化される。定期的に Actions タブで `dr-trigger.yml` が有効なままか目視確認することを推奨する。
