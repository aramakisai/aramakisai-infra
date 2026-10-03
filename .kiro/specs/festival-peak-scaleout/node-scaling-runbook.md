# ノード増減手順書 (server 1 台 ⇄ 3 台)

K3s embedded etcd のメンバー増減と CNPG インスタンス増減の手順。各工程に検証環境を明記する。

## 検証環境の凡例

| 記号 | 環境 | 範囲 |
|------|------|------|
| L | ローカル Docker 検証環境 (`verify/`) | 本番と同一の `k3s-server` ロールを、systemd 入り privileged Debian 13 コンテナ 3 台 (`10.0.1.0/24`、`node-ip` を private IP に固定) へ適用。K3s v1.35.5+k3s1、Cilium 1.17.3、CNPG operator chart 0.21.6 / PostgreSQL 16.8 |
| H | Hetzner 実環境の最小構成 | タスク 3.5。未実施 |
| - | 未検証 | どの環境でも確認していない |

k3d は systemd を持たず Ansible ロール (apt / systemd 前提) を適用できないため使っていない。コンテナ上の embedded etcd は実 VM と同じ手順 (ロール適用による join、`kubectl delete node` による member 除去、`--cluster-reset`) で増減でき、再現性の問題は見つかっていない。
L が再現しないもの: Hetzner private network、Tailscale、cloud-init、placement group、実ディスク・実ネットワーク障害、CPU / メモリ資源の差。

### 検証環境の再現

```bash
cd .kiro/specs/festival-peak-scaleout/verify
docker build -t scaleout-verify-node .
./env.sh net && ./env.sh up 1
export K3S_TOKEN=<任意> ANSIBLE_CONFIG=../../../../ansible/ansible.cfg ANSIBLE_ROLES_PATH=../../../../ansible/roles
ansible-playbook -i inventory.yml join.yml --limit sv-node-1   # ノード追加は up 2 / --limit sv-node-2
./env.sh destroy
```

Cilium は Helm で別途入れる (`k8sServiceHost=10.0.1.1`)。コンテナ再起動後は `/sys/fs/bpf` の mount が消えるため `env.sh up` 内の mount を再実行する。etcd の状態確認には etcdctl を各ノードに置く。

## 前提と共通の確認コマンド

- 本番の `prod-node-1` が `k3s_cluster_init: true` で `k3s_server` グループの先頭を保つ。追加ノードは `k3s_server_worker` に置く (join 先が先頭要素に依存するため)。
- etcd メンバー確認 (各 server ノード上):
  `etcdctl --endpoints=https://127.0.0.1:2379 --cacert=/var/lib/rancher/k3s/server/tls/etcd/server-ca.crt --cert=.../client.crt --key=.../client.key member list`
- 作業前に必ず etcd snapshot を取得し、ノード外へ退避する (後述)。

## 拡張 (1 → 2 → 3)

| # | 工程 | 環境 | 観測結果 |
|---|------|------|----------|
| E1 | `k3s etcd-snapshot save --name pre-expand` を実行し、ファイルをノード外へコピー | L (snapshot 取得のみ。退避先はローカルディスク) / H・本番の退避先: - | 取得は 1 秒未満 |
| E2 | ノード 2 を作成し、`ansible-playbook ... --limit <node2>` で join | L | ロール適用 88 秒、Ready まで 105 秒。メンバー数 2 |
| E3 | メンバー 2 の区間の扱い | L | 2 メンバーのうち 1 台を停止すると、残る 1 台の API も応答しなくなった (単一ノード構成より耐障害性が低い)。停止したノードを戻すと API は 16 秒で復帰。この区間では他の作業を行わず、速やかに E4 へ進む |
| E4 | ノード 3 を同様に join | L | ロール適用 56 秒、Ready まで 73 秒。メンバー数 3、全ノード Ready |
| E5 | 3 台構成で 1 台停止して API が応答することを確認 | L | 応答した。停止ノードの復帰後に Ready |

### tls-san 変更の影響 (L で確認)

- 既存ノード (`prod-node-1` 相当) へ新テンプレートを適用すると、SAN の内容が同じでも `config.yaml` の描画結果が変わるため、ハンドラにより **k3s が再起動される** (Ansible 実行 27 秒)。サービス断はこの再起動の間の API 一時不応答。Pod は `KillMode=process` により稼働し続ける。
- API 証明書は再生成されない (シリアル・SAN とも不変)。SAN に新しい名前を足したときだけ再発行される。
- 一度証明書に入った SAN は、設定から外しても証明書に残る。
- 追加ノードは自身の `ansible_host` と private IP を SAN に持つ。
- したがって本番で `config.yaml.j2` の変更を `prod-node-1` に適用する時点で、再起動が 1 回発生する。実施はメンテナンス可能な時間帯に行う。

### 拡張時の CNPG (L で確認)

| # | 工程 | 環境 | 観測結果 |
|---|------|------|----------|
| C1 | `spec.instances` を 1 → 3 に変更 (GitOps ではコミット) | L | 3 インスタンスが Ready になるまで 43〜85 秒 (空に近い DB)。3 つとも別ノードに配置された。レプリケーションは async |
| C2 | Standby の配置確認 | L | 既定の `podAntiAffinityType: preferred` は強制ではない。ノード数が足りる場合は分散したが、ノード障害後の再作成時に同居しうる。同居を許さないなら `required` が必要 (L では未検証) |
| C3 | prod-node-1 停止を伴う placement group 追加 | H / - | **未検証** (Hetzner 固有) |

## 縮退 (3 → 2 → 1)

### 事前: スナップショット退避 (L / 退避先は H・本番で別途確定)

```bash
k3s etcd-snapshot save --name pre-shrink
docker cp / scp などでノード外へ退避 (本番はオブジェクトストレージ等。退避先の確定は本手順の対象外)
```

### CNPG を先に戻す (L で確認)

1. 各クラスタの Primary の所在を確認する。
2. Primary が残すノード (`prod-node-1`) 上のインスタンスでなければ、そのクラスタを `spec.instances=3` のまま switchover で `prod-node-1` 上のインスタンスへ移す。
   - kubectl-cnpg プラグインが無い場合: `kubectl patch cluster <name> --subresource=status --type merge -p '{"status":{"targetPrimary":"<pod名>"}}'`
   - 重要: Primary が他ノードにある状態で `instances` を 1 に下げると、**Primary が残り Standby が消える**。local-path の PV はノードに紐づくため、残ったインスタンスの載るノードが削除対象だとデータを失う。L で実際に確認した。
3. `instances` を 1 に戻す。Standby が削除され、Primary のみが残ったことを確認する (切り替えから 40 秒以内に完了)。
4. 以降の手順に進む前に、残った Primary の Pod が `prod-node-1` 上にあることを確認する。

### ノード削除 (L で確認)

ノードは 1 台ずつ、次の順で処理する。**ノードを止める前に Node オブジェクトを削除する。**

| 段階 | 工程 | 環境 | 観測結果 |
|------|------|------|----------|
| S1 | `kubectl drain <node> --ignore-daemonsets --delete-emptydir-data` → `kubectl delete node <node>` | L | etcd メンバーは Node 削除の直後に自動で除去された (3 → 2) |
| S2 | ノード上で `systemctl stop k3s; k3s-uninstall.sh` (本番はサーバー削除) | L | 3 → 2 の全工程 18 秒。クォーラム維持、API 応答 |
| S3 | 2 台目で S1 と同じ手順 | L | 2 → 1 の全工程 24 秒。メンバー 1、`readyz` ok、DB の行数は不変、新規 Pod がスケジュール可能 |

3 → 2 ではクォーラムが 3 メンバー中 2 で維持される。2 → 1 では、Node 削除によりメンバーが先に外れてから 1 メンバーのクォーラムになるため、API は止まらなかった。

### 誤った順序 (L で再現)

2 メンバーの状態で、Node オブジェクトを削除せずに先に k3s を停止すると、残るノードの API が応答しなくなった (etcd クォーラム喪失)。ノード停止前の Node 削除を順守する。

復旧手順 (L で確認、ノードが戻せない場合):

```bash
# 残すノード (prod-node-1) 上
systemctl stop k3s
k3s server --cluster-reset
systemctl start k3s     # API は約 19 秒で復帰。メンバーは 1 になる
kubectl delete node <失われたノード>
```

停止したノードが戻せるなら、起動するだけでクォーラムは復帰する (E3 参照)。

### 縮退後 (一部 L / 一部 -)

| 工程 | 環境 |
|------|------|
| Terraform でサーバー削除、Tailscale デバイス削除、`dr-trigger` 再有効化、inventory からのエントリ除去 | H / **未検証** (3.5) |
| Pod のレプリカ数・配置制約を元に戻す (GitOps) | - (L ではアプリ未デプロイ) |

## CNPG の障害時挙動 (L で確認)

| 事象 | 結果 |
|------|------|
| switchover | 書き込み停止は約 5 秒。データ欠損なし |
| Primary Pod の強制削除 | 書き込み停止は約 7 秒。昇格後に全インスタンスが健全化するまで約 18 秒 |
| Primary を載せたノードの graceful 停止 | 書き込み停止は約 9 秒 |
| Primary を載せたノードの強制停止 (電源断相当、`docker kill`) | 書き込み停止は約 6 分。ノード NotReady 後の Pod 退避 (既定 300 秒) を待ってから昇格した。**本番で短縮したい場合は toleration の秒数調整が必要 (L では未検証)** |
| データ欠損 | 上記のすべてで、アプリが成功を確認した書き込みの欠損なし (async のまま) |

同期レプリケーション (`minSyncReplicas`) の要否: 検証した範囲では async でも欠損は出ていない。ただし Primary 障害の瞬間に未転送の WAL があれば失われうる理論上の余地は残る。同期にすると Standby 喪失時に書き込みが止まるリスクがあるため、採否は 7 章の方針に従い別途判断する (L の結果だけでは決められない)。

## 未検証の工程

- Hetzner 固有 (3.5): Tailscale デバイス削除と次回作成時の名前、`recovery.sh` の削除実装との一致、private IP 割当と cloud-init、placement group 追加に伴う prod-node-1 停止。
- 実クラスタのアプリ (cms-db / zitadel-db のサイズ・WAL アーカイブ・バックアップ) を伴う CNPG 増減。
- スナップショットの退避先 (オブジェクトストレージ等) と、退避したスナップショットからの復元。
- 強制停止時の CNPG failover 短縮。
