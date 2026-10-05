# DR ランブック — シングルノード コールドスタンバイ復旧

## 概要

検知は通知のみ、復旧は人が承認して実行する。

- `dr-trigger.yml` (5分毎 cron) がノード障害を判定し、Discord 通知と `dr-incident` Issue の起票/追記だけを行う。復旧ワークフローは自動起動しない。
- 復旧は `dr-recovery.yml` を人が `workflow_dispatch` で起動し、GitHub Environment `dr-recovery` の required reviewers (team `infra`) が承認すると始まる。起動者本人の承認でもよい。
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
    - (b) で1つのみ応答なし、または (a) が NodeDegraded → 通知のみ (最終通知から1時間は再通知しない)
  NodeFailureSuspected は1回の実行内で3回連続 (30秒間隔) したときだけ障害として扱う。
    1. open な `dr-incident` Issue が無ければ起票し Discord へ通知
    2. open な Issue があれば追記のみ (最終通知から1時間後に再通知)。重複起票しない。
       Issue の検索に失敗した場合は起票せず Discord 通知のみ
    3. 全シグナルが3回連続 (15分) で正常だった場合に Issue を自動クローズ
  最終通知時刻と連続 Healthy 回数は Actions のキャッシュ (`.dr-trigger-state.json`) で実行間に引き継ぐ。
  キャッシュが失効しても初期状態に戻るだけで、検知は止まらない (再通知が早まる側)。
```

Tailscale は非 ephemeral のため旧デバイスが残る。lastSeen が2日以上前の切断デバイスは実在ノードに数えない。
ノードを外すスケールインでは、Terraform の `local.nodes` から外すのと同時に Tailscale のデバイスも削除する
(2日間は切断デバイスが数えられ、クォーラム判定が厳しめ = 誤検知側に出るため)。

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
```

Actions の実行画面で team `infra` のメンバー (起動者本人でもよい) が **Review deployments** から承認すると、ジョブが始まる。

| 入力 | 既定 | 意味 |
|------|------|------|
| `target_node` | (必須) | 復旧対象。terraform `local.nodes` に定義済みで、inventory の cluster-init ホストであること |
| `force` | false | 生存確認ゲートの上書き。サーバーが稼働中の場合は Terraform・電源操作を行わず Ansible から再実行する (冪等化済みの `k3s-bootstrap.yml` が前提) |

### 処理の流れ

```
0. 進捗記録: open な dr-incident Issue (無ければ作成) に各段階と TFC run ID を追記
1. 生存確認ゲート (読み取り専用)。次のいずれかで停止 (force でのみ上書き)
     - Hetzner のサーバー状態が off / 不在以外 (running, starting など)、または取得失敗
     - Hetzner 上に不在でも TFC state に対象サーバーの記録が無い、または state を取得できない (不整合は停止)
     - Tailscale 上で対象ノードが接続中、または API 失敗・想定外の応答
     - 公開エンドポイントのどれかが応答
     - kubectl get nodes が成功
2. 他に Hetzner サーバー (role=server) が残っている、対象が cluster-init ホストでない、
   Ansible を流す経路で HEAD が origin/main と一致しない・未コミット変更がある・冪等化済み playbook
   (ansible/playbooks/tasks/ensure_secret.yml) が無い、
   サーバー再作成の経路でメールの ReplicationSource が `spec.paused: true` でない、のいずれかなら破壊的操作の前に停止
   (checkout は承認後の main を使う)
3. サーバー状態で分岐
     不在: plan 作成 (-target=対象サーバーのみ, auto-apply 無効)
           → plan の変更が「対象サーバー作成 + Tailscale auth key 置換」だけか機械検査
              (placement group・DNS・RDNS・他ノードの変更が混ざれば run を discard して停止)
           → Tailscale の旧デバイス (名前一致 かつ offline) を ID 指定で削除 → apply
           → メール用 A/AAAA と rDNS (mail_prod_node_1, mail_prod_node_1_ipv4, mail_ipv4, mail_ipv6)
              だけを target にした2回目の run。この4アドレスの create/update/置換以外が混ざれば discard して停止
     停止: 電源投入のみ (Tailscale デバイスは消さない。消すと再接続できなくなる)。IP は変わらない
4. 対象ノードが Tailscale に接続するまで待機 (最大10分)
     停止からの復帰: Ansible は流さず、ノードが k3s で Ready に戻るまで待機 (最大10分)
5. 不在・force の経路のみ: ansible-playbook k3s-bootstrap.yml を対象ノードに限定して実行 (最大40分。SSH は
     CI 専用デプロイ鍵 `CI_SSH_PRIVATE_KEY` を 0600 の一時ファイルに書き出して使い、終了時に削除する)
     (cluster-init は空の etcd から作り直す。etcd スナップショットは取得していない)
     完了後に Infisical の共有 kubeconfig を読み取って取得し直す (取得失敗は停止)
6. infisical-auth / Deploy Key の空チェックと自己修復 (CI 用 identity の値から作成)
7. ArgoCD の Application が Healthy になるまで待機 (replicas=0 のワークロードだけを持つ凍結中アプリは除外、最大20分、
   待機中に mail-tls の自己修復も試行) と、稼働中の全 CNPG クラスターの healthy 待機 (最大15分)。
   instances=0 や hibernation 中のクラスターは対象外。タイムアウトは失敗
```

実行中の run は apply の前に異常終了しても、未 apply の TFC run を discard してワークスペースのロックを残さない。
ワークフローのジョブ上限は各段階の最大待機の合計 (TFC run 2 回分を含む145分) より長い165分。

メールデータのリストアは自動化していない。ノード再作成後の `mailserver-data` は空の PVC で始まる。手順は「手動フォールバック」を参照。

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
| 再作成後の mailserver が空の PVC で起動する | メールのリストアは自動化していない | 「手動フォールバック」のメールリストアを人が判断して実施 |
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

### ステップ 6: mailserver のリストア (人が判断して実施、コミットだけで完結)

自動化していない。稼働中の PVC には書き込まない。**新名の PVC** にリストアしてから StatefulSet の参照を切り替える。
クラスタへの直接操作 (scale / patch / Application の sync 停止) は不要で、すべて Git のコミットと ArgoCD の sync で進める。
`mailserver` Application は `automated.selfHeal` のため、`kubectl scale` や稼働中 PVC への直接リストアは使わない。

雛形: `docs/templates/mailserver-restore.yaml` (PVC + ReplicationDestination。ArgoCD の対象パス外)。
mailserver の PVC 名は `gitops/manifests/prod/mailserver/` の3か所 (`pvc.yaml` の名前、`statefulset.yaml` の `claimName`、
`replication-source.yaml` の `sourcePVC`) で参照されている。以降、現在の名前を `<旧>`、新しい名前を
`mailserver-data-<YYYYMMDD>` (`<新>`) と書く。

**事前 (DR の新クラスターでは必須): ReplicationSource を paused にしておく**

新クラスターでは GitOps が空の PVC を作り、`mailserver-backup` (ReplicationSource) が同じ restic リポジトリへ
最初のバックアップを取る。VolSync 0.9.1 のソース (`controllers/statemachine/machine.go`) では、一度も同期していない
ReplicationSource は schedule を待たず **作成直後に同期を開始する**。これにより (a) 最新のスナップショットが空になり、
(b) 保持ポリシー (hourly 12 / daily 7、prune 7日) で障害前のスナップショットが時間とともに消える。

- `spec.paused: true` の ReplicationSource は backup Job の parallelism が 0 になり、スナップショットを作らず prune もしない。
- GitOps 上で「DR のときだけ paused」にする仕組みは作れないため、手順で担保する。**`dr-recovery` を起動する前に**
  `replication-source.yaml` へ `spec.paused: true` を入れたコミットを main に入れる。`recovery.sh` は新規作成の経路でこれを検査し、
  無ければ破壊的操作の前に停止する。
- paused の間は mailserver のバックアップが取られず、バックアップ監視 (Healthchecks.io) は猶予後にアラートする (想定内)。
- 切替が済んだら手順6で `paused: true` を外す。外した直後に初回の同期が (schedule を待たず) 始まり、リストア済みのデータをバックアップする。

**通常時 (クラスターが健在な部分リストア) も `restoreAsOf` を指定する**。最新のスナップショットに破損した状態が含まれうるため、
戻したい時点より前で最新のものを選ぶ。

**手順 (各行が1コミット、sync 後に確認してから次へ)**

0. (DR のみ) 上記の `paused: true` のコミットを、`dr-recovery` の起動前に main へ入れる。
1. **PVC と RD を追加**: 雛形の PVC (`<新>`、annotation `volume.kubernetes.io/selected-node: prod-node-1` を維持) と
   ReplicationDestination (`destinationPVC: <新>`、`trigger.manual` に過去に使っていない一意の値、`restic.restoreAsOf` に
   **障害発生時刻**) を `gitops/manifests/prod/mailserver/` に追加してコミットする。StatefulSet は変更しないため
   sync-wave の指定は不要。`selected-node` は mailserver の `nodeSelector` (`prod-node-1`) と一致させ、mover が別ノードに載って
   PV が固定されるのを防ぐ。RD に `Prune=false` は付けない (手順3で prune により削除するため)。稼働中の mailserver と `<旧>` には触れない。
2. **完了を確認**: RD の `status.lastManualSync` が `spec.trigger.manual` と一致し、`status.latestMoverStatus.result` が
   `Successful` になるまで待つ (`make kubectl ARGS="get replicationdestination mailserver-restore -n prod -o yaml"`、読み取りのみ)。
   復元されたスナップショットが `restoreAsOf` 以前のものか、mover Pod (名前に `volsync-dst-mailserver-restore` を含む) のログで確認する。
   Failed の場合は原因 (restic リポジトリの Secret、`privileged-movers` annotation) を直して `trigger.manual` を新しい値に変えてコミットする。
3. **RD を削除するコミット**: リストア完了後、StatefulSet を切り替える前に RD を削除する。理由: RD が残っている間に
   RD が再作成される (手動削除と selfHeal、Replace 同期など) と `status.lastManualSync` が失われ、同じ `trigger.manual` でも
   未実行とみなされて再リストアが走る。切替後に残すと、その再リストアが稼働中の PVC を上書きする。切替前なら
   `<新>` は未使用なので影響しない。RD の削除は mover Job とキャッシュなど RD 自身の子リソースを消すだけで、
   `destinationPVC` の PVC は RD の所有物ではないため残る (restic restore は上書きのみで、削除では何も実行されない)。
4. **参照を切り替えるコミット**: `statefulset.yaml` の `claimName` と `replication-source.yaml` の `sourcePVC` を `<新>` にする
   (DR では `paused: true` は維持)。`claimName` は Pod テンプレートの値で更新可能 (volumeClaimTemplates ではない)。
   Pod が再作成され、`<新>` のデータで起動する。`<旧>` の PVC マニフェストは残す (prune されて消えないように)。
5. **確認**: mailserver が Ready で、`doveadm user` と webmail でメールが見えること。
6. (DR のみ) **`paused: true` を外すコミット**: 5 を確認してから外す。初回の同期がすぐ走り、次の ReplicationSource の実行成功を確認する。

**ロールバック**: 手順4のコミットを revert して `claimName` / `sourcePVC` を `<旧>` に戻す。`<旧>` は削除していないため、
切替前の状態で起動する。切替後に `<新>` が受信したメールは `<旧>` には無い。

**旧 PVC の扱い**: 切替後も `<旧>` を残し、問題が無いと確認できてから (目安は1週間) 別コミットでマニフェストから外す。
外すと prune で PVC が削除され、local-path の reclaimPolicy が Delete のため **データも消える (戻せない)**。
削除前に人が確認する。切替の間に `<旧>` が受けたメールは `<新>` に入らないため、必要なら削除前に `<旧>` を別の Pod にマウントして
`rsync -a` (`--delete` は付けない) で `<新>` にマージする。

**DR (新クラスタ) の場合**: GitOps が現在の名前の PVC を空で作り、mailserver は空の PVC で起動する。復旧後に上記の手順1〜5を
新しい名前で行う (空の PVC への切替までの間に受けたメールは `<旧>` 側に残り、削除しなければ失われない)。

**名前の扱い**: PVC は改名できないため、リストアのたびに名前が変わる。`pvc.yaml` には現在使う PVC (切替後は `<新>`) だけを定義し、
`<旧>` は上記の保持期間が終わったら外す。名前は日付付きのまま運用し、固定名 `mailserver-data` には戻さない。

**検証済み範囲** (ローカル検証、本番では未実行):
- `copyMethod: Direct` + `destinationPVC: <通常の PVC>` のリストアが成功し、sha256 一致、uid/gid 5000、mode 700 が保持された。
- VolSync 0.9.1 + local-path では volume populator (PVC の `dataSourceRef` → RD) は使えない。`Direct` は latestImage が PVC 種別で
  populator 非対応、`Snapshot` は local-path が非 CSI で snapshot 不可。どちらもエラーにならず空の PVC が bind される。
- 本番の mailserver バックアップは、namespace `prod` の `volsync.backube/privileged-movers: "true"` により稼働している。

**未検証範囲**:
- 本番クラスタでの手順全体 (コミットから sync、切替まで)。
- RD 削除が PVC に影響しないこと、削除で再リストアが走らないことは、VolSync の所有関係と挙動の説明に基づく (実測していない)。
- `ReplicationSource` の `sourcePVC` 変更が既存の RS に適用でき、バックアップが連続すること。
- 新 PVC に `volume.kubernetes.io/selected-node` を事前に付けると local-path が consumer 無しで指定ノードに PV を作ること (mover Pod と mailserver Pod が同じノードに載ること)。
- `restoreAsOf` の指定が実際にそれ以前のスナップショットを選ぶこと (VolSync 0.9.1 の API 仕様に基づく、実測していない)。
- `paused: true` の ReplicationSource が backup Job を作っても実行せず、`paused` を外すと初回同期が始まること (ソースの読解に基づく、実測していない)。
- `<旧>` と `<新>` の同時存在時のディスク容量。

---

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
2. **Required reviewers** に team `infra` を設定する。承認者の追加・削除は team のメンバー管理で手動で行う
3. **Prevent self-review は無効**にする。起動者本人が承認できる (別メンバーの承認は必須としない)
4. **Allow administrators to bypass** は無効にする
5. Deployment branches は `main` のみに制限する

Environment が無いまま実行すると GitHub が承認なしの Environment を自動作成してしまう。ワークフローは冒頭の検査で止まるが、先に作成しておくこと。

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
| `INFISICAL_CLIENT_ID` | Infisical Machine Identity Client ID (CI 用、読取) | dr-trigger.yml / dr-recovery.yml |
| `INFISICAL_CLIENT_SECRET` | Infisical Machine Identity Client Secret (CI 用、読取) | dr-trigger.yml / dr-recovery.yml |
| `INFISICAL_PROJECT_ID` | Infisical プロジェクト ID | dr-trigger.yml / dr-recovery.yml |
| `TS_OAUTH_CLIENT_ID` | Tailscale OAuth Client ID (tag:ci 用、ランナーの tailnet 参加) | dr-recovery.yml |
| `TS_OAUTH_SECRET` | Tailscale OAuth Client Secret (tag:ci 用) | dr-recovery.yml |

Infisical (`prod`) から注入する主なキー: `INFISICAL_CLIENT_ID/SECRET`、`HCLOUD_TOKEN` (Hetzner サーバー状態の確認・電源投入)、`TAILSCALE_OAUTH_CLIENT_ID/SECRET`、`TAILSCALE_TAILNET`、`TFC_API_TOKEN`、`TFC_WORKSPACE_ID`、`DISCORD_OPS_WEBHOOK_URL`、`K3S_TOKEN` ほか Ansible 用。
`TFC_API_TOKEN` は `owners` team の team token (有効期限なし。期限切れで障害時に DR が黙って止まるのを避けるため。無料プランでは team を `owners` 1つしか作れない。organization token は run を作れず、user token は個人に紐づくため不可)。org 管理者と同等の権限を持ち DR に必要な範囲より広いが、intrusion-response のローテーション対象に含まれる。

`GITHUB_TOKEN` は Issue 操作のため `issues: write` を、dr-recovery.yml では Environment の保護ルール検査のため `actions: read` を `permissions` で付与している。新規 PAT は不要。
dr-recovery.yml は冒頭で Environment `dr-recovery` の required reviewers を検査し、未設定なら失敗する。`main` 以外の ref では起動しない。

**Tailscale 前提 (dr-recovery.yml)**: Tailscale ACL に `tag:ci` タグを定義し、`TS_OAUTH_*` のクライアントがそのタグでデバイスを登録できること。

**60日非活動による無効化に関する注意**: GitHub Actions の scheduled workflow はリポジトリに60日間コミット等の活動がないと自動的に無効化される。定期的に Actions タブで `dr-trigger.yml` が有効なままか目視確認することを推奨する。
