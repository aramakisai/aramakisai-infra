# ノード増減ランブック — 1 ノード ⇄ 3 ノードの一時拡張と縮退

## 概要

`prod-node-1` 単独の構成に `prod-node-2` / `prod-node-3` を追加して 3 ノード構成にし (スケールアウト)、イベント後に 1 ノード構成へ戻す (縮退) 手順。設計は `.kiro/specs/festival-peak-scaleout/design.md` の「スケールアウトの実行順序」「縮退の実行順序」。

- 3 ノード構成が存在するのはイベント前後の限られた期間だけ。開催日より前に完了し、動作確認の猶予を確保する。
- 各工程に「どの環境で確認したか」を明記する。凡例は次節。確認できていない工程は末尾の「未検証の領域」に一覧する。
- Terraform / Ansible / `make kubectl` の基本コマンドは [CLAUDE.md](../CLAUDE.md) を参照。`gitops/` 配下はすべて PR → マージ → ArgoCD 同期で反映し、`kubectl patch/apply` で直接変更しない。

## 検証状況の凡例

| 記号 | 意味 |
|------|------|
| **k3d** | k3d の使い捨てクラスタ (K3s は inventory の `k3s_version` と同一、CNPG は本番と同系) で確認 |
| **Hetzner** | Hetzner の検証専用プロジェクトで、本番と同じ `k3s-server`・`swap` ロールと cloud-init により実機 3 台を構築して確認 ([検証ハーネス](#検証ハーネス-scaletest)) |
| **未検証** | どちらでも確認していない。末尾の一覧に理由と本番での扱いを書く |

検証環境が再現する範囲:

| 項目 | k3d | Hetzner |
|------|-----|---------|
| etcd メンバーの増減・クォーラム挙動 | 再現する | 再現する |
| CNPG の増減・switchover・昇格 | 再現する | 入れていない (本番のバックアップ先・Secret への接続を避けるため) |
| `node-ip` の private IP 固定・private network | しない | 再現する |
| Tailscale 登録・デバイス残存・同名再作成 | しない | 再現する |
| cloud-init・固定 private IP の attach | しない | 再現する |
| CNI | flannel (本番は Cilium) | Cilium (本番と同じ chart・values) |
| `tls-san` の渡し方 | CLI 引数 | config.yaml (本番と同じ) |

## 守るべき順序制約

> **1. 3 台 → 2 台 → 1 台の削除は、対象を稼働中のまま `kubectl delete node` する。** 2 台 → 1 台で先に対象を止めると、停止の瞬間にクォーラムを失い `kubectl delete node` も `etcdctl member remove` も通らなくなる。削除した直後に対象を停止する。(k3d で失敗順序と正順序の双方を確認、Hetzner で正順序を確認)
>
> **2. CNPG は、主系を残すノード (`prod-node-1`) へ switchover し、全インスタンスが Ready に戻ってから `instances: 1` にする。** CNPG は縮退時に主系を削除しない。主系が他ノードにあるまま `instances: 1` にすると主系がそのノードに残り、ノード削除でデータを失う。(k3d)
>
> **3. `tls-san` の変更は K3s の再起動が必要。** config.yaml を書き換えただけでは serving cert は再生成されない。新規 server の追加だけなら既存ノードへの再適用は不要 (各ノードが自分の値を持つ)。(k3d)
>
> **4. Tailscale の旧デバイスは、サーバー削除後に削除してから同名ノードを再作成する。** 削除せずに再作成すると新デバイスが別名 (`<node>-1`) で登録され、MagicDNS が旧 (offline) デバイスを指して Ansible が接続できなくなる。(Hetzner)
>
> **5. 拡張の途中でメンバーが 2 となる区間は、1 台の停止でクォーラムを失う。** この区間を短く保ち、他の作業を行わない。停止したノードを起動すれば約 15 秒で復帰する。(k3d)
>
> **6. prod-node-1 の placement group 追加 (A-6) は、データ層の冗長化 (A-5) の完了後に行う。** 順序を逆にすると同じ操作が全サービスの停止になる。
>
> **7. 作業の前に etcd スナップショットを取得し、ノード外へ退避する。** スナップショットは既定でノードのローカルにしか残らず、ノードを失うと同時に失われる。

## 事前確認

- `make kube-login` 済みで `make kubectl ARGS="get nodes"` が通ること。tailnet に接続していること。
- CNPG のバックアップが正常であること (`kubectl get backups.postgresql.cnpg.io -A` で直近が `completed`)。
- 各ノードのディスクが CNPG インスタンスの増分を収容できること。CNPG のストレージは `local-path` で、各インスタンスはスケジュールされたノードのローカルディスクに置かれる。
- 作業はイベント前の余裕のある時間に行う。拡張中は mailserver が A-6 の区間だけ停止する (`prod-node-1` 固定のため)。

---

## A. スケールアウト (1 → 3 ノード)

### A-1. 作業前の復旧手段を確保する

etcd のスナップショットを取得し、ノード外へ退避して読み出しを確認する。スナップショットにはクラスタの Secret が含まれるため、権限を絞り、リポジトリ外の場所に置き、作業完了後に削除する。

```bash
ssh root@prod-node-1 'k3s etcd-snapshot save --name pre-scaleout'
ssh root@prod-node-1 'ls -1 /var/lib/rancher/k3s/server/db/snapshots/ | grep pre-scaleout'   # ファイル名を控える
umask 077
scp root@prod-node-1:/var/lib/rancher/k3s/server/db/snapshots/<上で控えたファイル名> <リポジトリ外の退避先>/
sha256sum <退避先>/<ファイル名>
ssh root@prod-node-1 'sha256sum /var/lib/rancher/k3s/server/db/snapshots/<ファイル名>'   # 一致を確認
```

- 検証: 取得 (約 1 秒、十数 MB)、ノード外へのコピー、sha256 の一致まで **k3d**。Hetzner 実機のスナップショット取得は **未検証** だが、手順は k3d と同じ。
- スナップショットからの復元 (`--cluster-reset-restore-path`) は **未検証** (末尾の一覧を参照)。

### A-2. 自動復旧の扱いを確認する

`dr-trigger.yml` は通知のみで復旧を自動起動しない。復旧は `dr-recovery.yml` を人が起動し承認した場合だけ動き、複数ノード構成では生存確認ゲートが停止する ([docs/dr-runbook.md](dr-runbook.md))。

- 作業中の過渡状態で `dr-trigger` が障害を通知し、`dr-incident` Issue が起票されることがある。実害はなく、正常化すると自動クローズされる。
- 作業中は `dr-recovery.yml` を起動しない。作業記録に「作業期間中の `dr-trigger` 通知は誤報として扱い、`dr-recovery` を起動しない」ことを残す。
- 3 ノード構成で `prod-node-1` を失った場合の自動復旧は停止し、手動手順 ([複数ノード構成の復旧](dr-runbook.md#複数ノード構成-ノード追加期間中-の復旧)) になる。

検証: ワークフロー・スクリプトの読解のみ (**未検証**: 3 ノード構成での実障害)。

### A-3. 追加ノードを作成する

対象を限定した plan で追加ノードの新規作成だけが示されることを確認してから apply する。対象無限定の plan は到達不能なプロバイダにより完了しないため、`-target` を使う。

```bash
cd terraform
infisical run --env=prod -- terraform plan \
  -target='hcloud_server.nodes["prod-node-2"]' -target='hcloud_server.nodes["prod-node-3"]'
# 追加ノード 2 台の作成と placement group の作成、tailscale_tailnet_key.k3s_nodes の置換だけであること。
# prod-node-1 の再作成が示されたら apply しない
infisical run --env=prod -- terraform apply \
  -target='hcloud_server.nodes["prod-node-2"]' -target='hcloud_server.nodes["prod-node-3"]'
cd ..
```

`tailscale_tailnet_key.k3s_nodes` の `must be replaced` は設計どおりの既知の差分 (有効期限 1 時間)。

作成後、`prod-node-2` / `prod-node-3` が Tailscale に接続し、互いに異なる物理ホストに配置されていることを確認する。

```bash
tailscale status | grep prod-node
```

- 検証: cloud-init による Tailscale 登録 (約 1 分以内)・想定名 (`<node>`)・`tag` 付与・private IP の固定 attach (起動中でも成功) は **Hetzner**。サーバー作成から API 上 running までは数秒。
- placement group による物理ホスト分散、`terraform apply -target` の実行は **未検証** (検証環境は本番の Terraform を使わない)。

併せて `gitops/manifests/prod/ops-dashboard/collector/dashboard.toml` の `[[servers]]` に追加ノードを加える PR を出す ([docs/ops-dashboard-runbook.md](ops-dashboard-runbook.md))。

### A-4. 追加ノードをクラスタへ参加させる

実機の作成後に、追加ノードを `ansible/inventory/tailscale.yml` の `k3s_server_worker` へ戻す PR を出してマージする。`k3s_server` グループ (`prod-node-1` のみ) は変更しない。join 先の解決がこのグループの先頭要素に依存する。実機のないホストを inventory に残すと `any_errors_fatal` の Play が unreachable で全台分中断し、`k3s-bootstrap` と `k3s-upgrade` が `prod-node-1` を含めて実行できなくなる。

```yaml
k3s_server_worker:
  hosts:
    prod-node-2: {ansible_host: prod-node-2, k3s_role: server, k3s_private_ip: 10.0.1.2}
    prod-node-3: {ansible_host: prod-node-3, k3s_role: server, k3s_private_ip: 10.0.1.3}
```

マージ後 (制御ノードの HEAD が `origin/main` と一致している必要がある)、対象を限定して差分を確認してから実行する。

```bash
# 追加ノードだけが changed になり、prod-node-1 に変更が出ないこと
infisical run -- ansible-playbook -i ansible/inventory/tailscale.yml ansible/playbooks/k3s-bootstrap.yml \
  --limit prod-node-2,prod-node-3 --check --diff
infisical run -- ansible-playbook -i ansible/inventory/tailscale.yml ansible/playbooks/k3s-bootstrap.yml \
  --limit prod-node-2,prod-node-3
```

`k3s-server` ロールは 1 台ずつ join し、各ノードで etcd の readyz と Node Ready を待つ。参加後に全ノードの Ready とメンバー数 3 を確認する。

```bash
make kubectl ARGS="get nodes -o wide"          # INTERNAL-IP が 10.0.1.1 / 10.0.1.2 / 10.0.1.3 で全て Ready
make kubectl ARGS="get --raw=/readyz/etcd"
```

etcd のメンバー数は etcdctl で確認する (K3s は etcdctl を同梱しない)。

```bash
V=v3.6.15
curl -fsSL -o /tmp/etcd.tgz https://github.com/etcd-io/etcd/releases/download/$V/etcd-$V-linux-amd64.tar.gz
curl -fsSL https://github.com/etcd-io/etcd/releases/download/$V/SHA256SUMS | grep "etcd-$V-linux-amd64.tar.gz" | (cd /tmp && sha256sum -c -)
tar -xzf /tmp/etcd.tgz -C /tmp --strip-components=1 etcd-$V-linux-amd64/etcdctl
scp /tmp/etcdctl root@prod-node-1:/tmp/etcdctl
ssh root@prod-node-1 'D=/var/lib/rancher/k3s/server/tls/etcd
  /tmp/etcdctl --endpoints=https://127.0.0.1:2379 --cacert=$D/server-ca.crt --cert=$D/client.crt --key=$D/client.key member list -w simple'
```

期待: メンバー 3、peer URL が `10.0.1.x`。

- 検証: 1 → 2 → 3 の join、メンバー数、`node-ip` の private IP 固定、`server: https://<private IP>:6443` での join、Cilium 稼働は **Hetzner**。各段約 35 秒 (k3d)、Hetzner では 1 台目の cluster-init と Cilium で約 5 分、2・3 台目の join を含めて全体で約 12 分 (K3s・パッケージの取得を含む) で failed 0。
- 拡張中のメンバー 2 区間が 1 台停止でクォーラムを失うこと、停止ノードの起動で約 15 秒で復帰することは **k3d**。メンバー 3 では 1 台停止しても API は継続する (**k3d**)。
- `--limit` を付けた事前検査の動作、本番 inventory での `--check --diff` は **未検証**。

### A-5. データ層を冗長化する

CMS (`directus-db`、namespace `prod`) と認証基盤 (`zitadel-db`、namespace `zitadel`) の `db-cluster.yaml` を `instances: 3` にし、別ノードへ配置する制約を加える PR をマージして ArgoCD で同期する。

```bash
make kubectl ARGS="get cluster -A"
make kubectl ARGS="get pods -n prod -l cnpg.io/cluster=directus-db -o wide"
make kubectl ARGS="get pods -n zitadel -l cnpg.io/cluster=zitadel-db -o wide"
```

期待: 各クラスタで 3 インスタンスが別ノードに Ready (`readyInstances: 3`)。待機系は非同期レプリケーションで、昇格時に直近のコミットを失う可能性がある。同期レプリケーション (`minSyncReplicas: 1`) は Standby の停止中に書き込みが詰まるため、A-6 のようにノードを停止する運用では採用しない。

- 検証: 1 → 3 の増加 (約 2 分で 3 Ready)、既定設定でも別ノードへ分散すること、主系 Pod 削除時の昇格 (約 5 秒、全 Ready まで約 30 秒)、switchover (約 3 秒、全 Ready まで約 12 秒) は **k3d**。
- ノード障害 (ノード停止) 時の昇格所要時間は **未検証** (k3d の値は Pod の graceful 削除。ノード障害ではより長い)。
- 本番の Hetzner 上での CNPG・バックアップ (barman) は **未検証**。

### A-6. prod-node-1 を placement group へ追加する

**prod-node-1 が停止する。mailserver はこの区間のみ停止する。** A-5 の完了後に行う。3 ノード・CNPG 冗長化済みであれば、停止中も etcd はメンバー 2 でクォーラムを維持し、データベース接続は残存ノードの待機系が引き継ぐ。停止時間を最小にするため、手順を先に確認してから始める。

```bash
cd terraform
# prod-node-1 が in-place の更新 (placement_group_id のみ) で、再作成が示されないこと
infisical run --env=prod -- terraform plan -target='hcloud_server.nodes["prod-node-1"]'
```

1. `plan` が再作成を示した場合は中止する。
2. Hetzner Cloud Console で `prod-node-1` をシャットダウンする (Hetzner は既存サーバーの placement group 追加にオフラインを要求する)。
3. `infisical run --env=prod -- terraform apply -target='hcloud_server.nodes["prod-node-1"]'` で追加する。
4. サーバーを起動し、`make kubectl ARGS="get nodes"` で全ノードが Ready に戻ることを確認する。
5. 失敗した場合は `prod-node-1` を起動して従前の状態へ戻し、追加ノードのみが所属する状態を記録する。

**未検証** (既存サーバーの placement group 追加と、それに伴う停止・復帰は検証環境で行っていない。検証では全台を最初から同一構成で作成した)。prod-node-1 の停止中に残り 2 台でクォーラムが維持されることは、メンバー 3 で 1 台停止しても API が継続する挙動 (**k3d**) に基づく。

### A-7. 投入後の全サービスを確認する

- ArgoCD・CMS・認証基盤・メールサーバーが正常応答すること。
- mailserver が `prod-node-1` 上で動作し続けていること。
- 意図しないワークロードが追加ノードへ移動していないこと (配置制約を設けていないため、再スケジュールで移動しうる)。

```bash
make kubectl ARGS="get pods -A -o wide"
make kubectl ARGS="get applications -n argocd"
```

既存サービスに影響が出た場合は B の手順で 1 ノード構成へ戻る。

検証: **未検証** (本番のアプリケーション構成は検証環境に入れていない)。

### A-8. ステートレスワークロードを分散する (条件付き)

負荷試験の判定でステートレス分散が必要と確定した場合に限る。`gitops/manifests/prod/cms/deployment.yaml` の `replicas` と分散配置の制約を PR で変更し、変更前の値を PR 本文に残す。直接 `kubectl scale` しない。

検証: k3d で Deployment の複数ノードへの分散 (6 レプリカが 3 ノードへ分散) を確認。本番のアプリケーションでの分散と、DB 接続数・Cloudflare 経由の挙動は **未検証**。

### A-9. 冗長化後の性能を再測定する

[scripts/load-test/README.md](../scripts/load-test/README.md) のシナリオを再実行し、3 ノード構成でのブレークポイントを記録する。測定値は公開リポジトリに書かない。

---

## B. 縮退 (3 → 1 ノード)

### B-1. 縮退前の復旧手段を確保する

A-1 と同じ手順でスナップショット名を `pre-scalein` として取得・退避し、sha256 の一致を確認する。検証状況は A-1 と同じ。

### B-2. ワークロードの配置を変更前へ戻す

A-8 で変更した `replicas` と配置制約を変更前の値へ戻す PR をマージする。削除対象ノード上で稼働するものを事前に減らす。検証: k3d で Deployment のレプリカ数を元に戻す操作を確認 (**k3d**)。

### B-3. データ層を単一構成へ戻す

各クラスタで主系の所在を確認し、`prod-node-1` 以外にある場合は先に移動する。

```bash
make kubectl ARGS="get cluster directus-db -n prod -o jsonpath={.status.currentPrimary}"
make kubectl ARGS="get pods -n prod -l cnpg.io/cluster=directus-db -o wide"   # prod-node-1 上のインスタンス名を確認
```

主系が `prod-node-1` 上にない場合、`prod-node-1` 上のインスタンスへ switchover する (`kubectl cnpg promote <cluster> <instance>` と同じ操作)。

```bash
make kubectl ARGS="patch cluster directus-db -n prod --subresource=status --type merge -p '{\"status\":{\"targetPrimary\":\"<prod-node-1 上のインスタンス名>\"}}'"
```

`status.currentPrimary` が移り、`readyInstances` が 3 に戻るまで待つ (約 12 秒)。その後 `db-cluster.yaml` を `instances: 1` に戻す PR をマージし、待機系の削除完了を確認する。`zitadel-db` (namespace `zitadel`) も同様に行う。

```bash
make kubectl ARGS="get pods -n prod -l cnpg.io/cluster=directus-db -o wide"   # prod-node-1 上の 1 本だけが残ること
```

- 検証: 主系の移動 (約 3〜4 秒)、`instances: 1` への縮小 (約 5〜24 秒)、主系を残すノードへ移した場合にデータが保持されること、移さなかった場合に主系が他ノードに残ること、縮退後の最終状態で CNPG が healthy でデータが保持されることは **k3d**。
- `status.targetPrimary` の patch は CNPG の switchover 操作であり、マニフェストの drift を生まない。`instances` の変更は必ず Git で行う。

### B-4. ノードを 1 台ずつ削除する

`prod-node-3` → `prod-node-2` の順に、1 台ずつ「drain → 稼働中のまま `kubectl delete node` → 直後に対象を停止」を行い、各段階でメンバー数とクォーラムを確認する。**2 → 1 は最も慎重に扱う** (守るべき順序制約 1)。

3 → 2 (`prod-node-3`):

```bash
make kubectl ARGS="drain prod-node-3 --ignore-daemonsets --delete-emptydir-data --timeout=180s"
make kubectl ARGS="delete node prod-node-3"           # prod-node-3 は稼働中のまま
ssh root@prod-node-3 'systemctl stop k3s'              # 直後に停止する
make kubectl ARGS="get nodes"
make kubectl ARGS="get --raw=/readyz/etcd"
# etcdctl member list でメンバー 2 を確認 (A-4 と同じ手順)
```

2 → 1 (`prod-node-2`):

```bash
make kubectl ARGS="drain prod-node-2 --ignore-daemonsets --delete-emptydir-data --timeout=180s"
make kubectl ARGS="delete node prod-node-2"           # prod-node-2 は稼働中のまま。ここでクォーラムが再構成される
ssh root@prod-node-2 'systemctl stop k3s'              # 直後に停止する。削除後に K3s を動かしたままにしない
make kubectl ARGS="get nodes"
make kubectl ARGS="get --raw=/readyz/etcd"
make kubectl ARGS="create deployment post-shrink --image=registry.k8s.io/pause:3.9"
make kubectl ARGS="rollout status deployment/post-shrink --timeout=120s"
make kubectl ARGS="delete deployment post-shrink"
```

期待: メンバー 1、API が応答し続ける、新規 Pod がスケジュールされる。クラスタが停止した場合は退避済みのスナップショットから復旧する。

- 検証: drain (約 5〜31 秒)、3 → 2 と 2 → 1 の双方で `kubectl delete node` から約 7 秒以内にメンバーが自動除去されること (手動の `member remove` は不要)、2 → 1 でクォーラム喪失がないこと、縮退後の 1 ノードで API 応答と新規 Deployment の配置・rollout が成功することは **Hetzner** と **k3d**。
- 3 → 2 は「drain → 停止 → delete node」の順でも成功した (**k3d**)。2 → 1 は先に停止すると失敗する (**k3d**)。
- 削除後の停止手段は Hetzner ではサーバーの削除 (API) で確認した。`systemctl stop k3s` による停止は **未検証** だが、メンバーは削除時点で除去済みのため結果は同じと考えられる。
- スナップショットからの復旧 (`--cluster-reset-restore-path`) は **未検証**。

### B-5. サーバーと接続デバイスを削除する

1. `terraform/main.tf` の `locals.nodes` から `prod-node-2` / `prod-node-3` を外す PR をマージする。`ansible/inventory/tailscale.yml` の `k3s_server_worker` も `hosts: {}` に戻す (同じ PR でよい)。残すと `any_errors_fatal` の Play が unreachable で全台分中断する。`dashboard.toml` の `[[servers]]` からも外す。
2. 対象を限定して適用し、追加サーバーを削除する。

    ```bash
    cd terraform
    infisical run --env=prod -- terraform plan -target='hcloud_server.nodes["prod-node-2"]' -target='hcloud_server.nodes["prod-node-3"]'
    # 2 台の削除だけであること
    infisical run --env=prod -- terraform apply -target='hcloud_server.nodes["prod-node-2"]' -target='hcloud_server.nodes["prod-node-3"]'
    cd ..
    ```

    `terraform destroy -target` は使わない。

3. Hetzner Console で課金が停止したこと (サーバーが存在しない) を確認する。
4. **Tailscale の旧デバイスを削除する。** 残すと次回の作成時に別名で登録され、構成管理が接続できなくなる。対象名に一致し offline のデバイスだけを ID 指定で削除する ([docs/dr-runbook.md](dr-runbook.md) の手動フォールバック ステップ 2 と同じ手順。対象の正規表現を `^prod-node-(2|3)(-[0-9]+)?$` に変える)。

    ```bash
    infisical run --env=prod -- bash -c '
    TOKEN=$(curl -sf -X POST https://api.tailscale.com/api/v2/oauth/token \
      -d "client_id=$TAILSCALE_OAUTH_CLIENT_ID" -d "client_secret=$TAILSCALE_OAUTH_CLIENT_SECRET" | jq -r .access_token)
    curl -sf -H "Authorization: Bearer $TOKEN" \
      "https://api.tailscale.com/api/v2/tailnet/$TAILSCALE_TAILNET/devices" \
      | jq -r ".devices[] | select(.hostname | test(\"^prod-node-(2|3)(-[0-9]+)?$\")) | select(.connectedToControl != true) | [.id, .hostname] | @tsv"
    '
    # 出力された ID が prod-node-2 / prod-node-3 のものだけであることを目視し、デバイスごとに
    # DELETE https://api.tailscale.com/api/v2/device/<id>
    ```

    削除後に `tailscale status | grep prod-node` で `prod-node-1` だけが残ることを確認する。2 日間は切断デバイスが実在ノードとして数えられ、`dr-trigger` のクォーラム判定が厳しめに出る。

- 検証: サーバー削除後もデバイスが残存すること (約 80 秒で offline)、同名再作成で新デバイスの `.name` が `<node>-1` になり `.hostname` は同名のままであること、`recovery.sh` と同じ条件 (`.hostname` が `^<node>(-[0-9]+)?$` に一致し offline) で旧デバイスを削除でき、削除後 0 件、再作成で想定名に戻ること (約 1 分) は **Hetzner**。
- 本番の Terraform での削除 (`-target` による destroy を含む plan)、本番の Tailscale デバイスの削除は **未検証**。

### B-6. 自動復旧の扱いを戻す

`dr-trigger` は常時通知のみのため、再有効化の操作はない。`dr-incident` Issue が残っていれば、正常化していることを確認して閉じる。縮退後の構成が移行前と等価であることを `locals.nodes` / inventory の内容で確認する (単一ノードの自動復旧が再び対象になる)。

---

## 注意: recovery.sh のデバイス判定

複数ノード構成の期間中は自動復旧が停止するが、単一ノードの `recovery.sh` には次の誤判定のリスクがある (今回は修正していない)。

- 同名ノードを旧デバイスの削除なしに再作成すると、新デバイスは `.name` (MagicDNS 名) だけが `<node>-1` になり、`.hostname` は同名のままである。
- `ts_node_registered` は `.hostname` の完全一致と接続中のみを見るため、旧デバイスが残っていても「登録済み」と判定する。一方、Ansible が解決する MagicDNS 名は旧 (offline) デバイスを指し、接続に失敗する。
- 手動で作成するときは、旧デバイスの削除後に残存 0 件を確認してから作成する。

---

## 検証ハーネス (scaletest)

`scripts/scaletest/scaletest.sh` が Hetzner の検証専用プロジェクトに使い捨ての 3 ノード構成を作り、本番と同じ `k3s-server`・`swap` ロールと cloud-init で構築する。

### 前提 (初回のみ)

- Hetzner: 検証専用プロジェクト `scaletest` と、そのプロジェクトの Read & Write API トークン、SSH 鍵 `scaletest-key` (ansible が root で SSH するため)。
- Tailscale: 検証専用の OAuth クライアント (scope は `devices:core` と `auth_keys` の write、タグは `tag:scaletest` のみ)。`tag:scaletest` の ACL は `terraform/tailscale-acl.hujson.tftpl` が正本で、人の端末からは到達でき、`tag:scaletest` 同士と本番ノードへは到達できない。このため K3s の join・etcd ピア・Cilium は private network (`10.250.0.0/16`) だけで行う。
- Infisical の `staging` 環境の `/scaletest` フォルダに次のキーを置く (値は書かない): `SCALETEST_HCLOUD_TOKEN`、`SCALETEST_TS_CLIENT_ID`、`SCALETEST_TS_CLIENT_SECRET`、`SCALETEST_TAILNET`、`K3S_TOKEN`。
- 手元に `ansible` (PyYAML を含む)・`jq`・`curl`・`ssh`・tailnet 接続が必要。

プロジェクト・SSH 鍵・OAuth クライアントは再検証のために残す運用とする。サーバー・network・firewall・primary IP・Tailscale デバイスは毎回 `down` で削除する。

### 使い方

必ず `staging` の `/scaletest` で、`.infisical.json` のあるリポジトリ (worktree を含む) の中から実行する。外では接続先プロジェクトを解決できず環境変数が注入されない。**`--env=prod` では実行しない** (スクリプトは本番用の変数 `HCLOUD_TOKEN`・`TAILSCALE_OAUTH_*`・`TF_VAR_*` 等が環境にあると停止する)。

```bash
S="infisical run --env=staging --path=/scaletest -- scripts/scaletest/scaletest.sh"
$S up                                  # network / firewall / scaletest-1..3 を (未作成のものだけ) 作成し Tailscale 登録を待つ (約 1〜2 分)
$S isolation                           # 本番ノード・他の検証ノードへ tailnet で到達できないこと
$S bootstrap scaletest-1               # 1 台目 cluster-init + Cilium (約 5 分)
$S bootstrap                           # 2・3 台目の join (約 7 分)
$S kubectl get nodes -o wide
$S members                             # etcd メンバー一覧
```

縮退 (B-4 と同じ順序):

```bash
$S kubectl drain scaletest-3 --ignore-daemonsets --delete-emptydir-data --timeout=180s
$S kubectl delete node scaletest-3     # scaletest-3 は稼働中のまま
$S delete-server 3
$S members                             # メンバー 2。同様に scaletest-2 を除去してメンバー 1
```

Tailscale デバイスの残存と同名再作成 (B-5 の根拠):

```bash
$S devices                             # サーバー削除後もデバイスが残る (約 80 秒で offline)
$S up 3                                # 旧デバイスを残したまま再作成すると .name が scaletest-3-1 になる
$S devices
$S delete-server 3
$S purge-devices 3                     # offline の scaletest-3 を削除
$S up 3                                # 約 1 分で想定名 scaletest-3 に戻る
```

後片付け (必ず実行する。サーバーは時間課金):

```bash
$S down                                # サーバー・firewall・network・Tailscale デバイスを削除し、0 件を確認
$S verify-clean                        # 単独でも確認できる
```

### 動作の要点

- 作業ディレクトリは `mktemp -d` で作り、終了時に削除する (`SCALETEST_WORKDIR` で指定可、ホーム配下は不可)。Tailscale の auth key と描画済みの cloud-init はファイルに書かずメモリ上だけで扱う。秘密は環境変数でだけ受け取り、表示しない。
- Cilium・Helm のバージョンと values、`k3s_version` は本番の `k3s-bootstrap.yml` と `inventory/tailscale.yml` から読み取って渡し、Cilium の適用は本番と同じ共通タスクを使うため、二重管理にならない。
- 検証用 playbook は `ansible/playbooks/scaletest-bootstrap.yml`、inventory は `ansible/inventory/scaletest.yml`。本番の `k3s-bootstrap.yml` は使わない。`os-auto-update`・cloudflared・ArgoCD・bootstrap Secret を含めることで、検証クラスタが本番の Discord・Tunnel・Infisical・バックアップ先へ接続するのを避けるため。
- 本番混入の防止: 本番用変数の検出、`KUBECONFIG` の検出 (`kubectl` サブコマンドはノード上の k3s を SSH 経由で使い、手元の kubeconfig・context は参照しない)、inventory と実行対象が `scaletest-N` だけであることの確認 (playbook 側でも検査)、Hetzner プロジェクトに `scaletest-N` 以外のサーバーがないことの確認、Tailscale デバイスの削除対象を `scaletest-N` に限定する。
- すべてのコマンドは再実行できる: `up` は存在するサーバーをスキップし (作成対象があるときだけ Tailscale auth key を発行する)、`bootstrap` は Cilium を現行 release との差分があるときだけ適用し (判定は本番と共通の `ansible/playbooks/tasks/cilium.yml`)、`down` は primary IP が消えるまで待って 0 件を確認する。
- ansible の出力はファイルへ書き、標準入力を閉じて実行する (長い playbook で端末入力待ちに入るのを避ける)。`ANSIBLE_CONFIG` には空の設定を指定する (リポジトリ直下・`ansible/` の `ansible.cfg` が本番 inventory を既定にしているため)。
- etcd のメンバー確認は etcdctl を使う (etcd の公式リリースを SHA256 で照合してノードの `/tmp` へ置く)。`tailscale ping` は `--c=N` 形式で IP を指定する。
- 実績: cx23 (2 vCPU / 4GB) 3 台で、準備から後片付けまで通しで約 40 分、費用は時間課金で数円程度。

---

## 未検証の領域

| 領域 | 理由 | 本番での扱い |
|------|------|--------------|
| etcd スナップショットからの復元 (`--cluster-reset-restore-path`) | 取得・退避・整合確認のみ実施。復元は破壊的で検証環境でも未実施 | 縮退で 2 → 1 が失敗してクラスタが停止した場合の最終手段。実施前に [docs/dr-runbook.md](dr-runbook.md) の複数ノード構成の復旧と併せて手順を確認し、復元自体を初めて実行する前提で時間を確保する |
| ノード障害時の CNPG 昇格時間 | k3d の値は Pod の graceful 削除。ノード停止での値は未計測 | 待機系が別ノードにあることで継続できる前提。昇格までの断は graceful 削除より長いと見込む |
| Hetzner Volume での PVC | 本番の CNPG は `local-path` で Volume を使わない。検証でも使っていない | 対象外。Volume を使う構成に変える場合は別途検証する |
| 既存サーバー (prod-node-1) の placement group 追加と停止・復帰 | 検証は全台を最初から同一構成で作成した | A-6。plan で in-place 更新のみであることを確認し、失敗時の戻し手順 (起動して従前へ) を用意して実施する |
| 本番の Terraform (`-target` の plan / apply / 削除) | 検証環境は本番の state・TFC に触れない | plan の差分を A-3・A-6・B-5 の期待どおりか目視で確認する |
| 本番 inventory での `--limit` 付き `k3s-bootstrap.yml`（`--check --diff` を含む） | 検証は専用 playbook を使う | A-4 のとおり `--check --diff` で既存ノードに変更が出ないことを確認してから実行する |
| `systemctl stop k3s` による削除直後の停止 | Hetzner ではサーバー削除、k3d ではコンテナ停止で確認 | B-4。`kubectl delete node` の直後に実行する |
| CNPG・アプリケーション・バックアップ (barman) を載せた 3 ノード構成 | 本番のバックアップ先・Secret への接続を避けるため検証クラスタには入れていない | A-5・A-7 のとおり各段階で状態を確認する |
| Cilium が node-2/3 へ展開される際の挙動の本番差 | 検証は同じ chart・values だが、ArgoCD 管理のアプリとの共存は未確認 | A-4 で全ノード Ready と Cilium Pod の稼働を確認する |
