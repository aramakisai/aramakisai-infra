# Research & Design Decisions - festival-peak-scaleout

## Summary

- **Feature**: `festival-peak-scaleout`
- **Discovery Scope**: Extension (既存の Terraform / Ansible 資産の条件付き拡張。新規サブシステムなし)
- **Key Findings**:
  - Ansible と Terraform は 3 ノード構成を**既に想定して書かれている**。`main.tf:4` と `inventory/tailscale.yml:15-16` の双方にノード追加を前提としたコメントがあり、join 分岐も実装済み。コード追加は最小で済む
  - DR 自動復旧機構が本 spec の最大の障害になる。`prod-node-1` の停止を etcd クォーラムの健全性と無関係に障害と判定し、TFC ワークスペース全体への無条件フル apply を auto-apply で実行する。恒常的な無効化スイッチは実装されていない
  - `hcloud_placement_group` が未定義のため、追加ノードが prod-node-1 と同一物理ホストに配置されうる。この場合 etcd を 3 台にしても単一障害点は解消しない
  - 外部公開経路は nginx-ingress を中心としない。Cloudflare Tunnel が各 ClusterIP へ直接転送し、公開 Web サイトは Cloudflare Workers で配信される。K3s へ到達する来場者トラフィックは CMS の API に限られる
  - CNPG は `instances` を増やすことで各インスタンスが自ノードの `local-path` PV を使う形で冗長化できる。共有ストレージを必要とせず、prod-node-1 の停止時にフェイルオーバーする

## Research Log

### Ansible と Terraform の 3 ノード対応状況

- **Context**: 要件 2 と 4 の実装コストを見積もる
- **Sources Consulted**: `ansible/playbooks/k3s-bootstrap.yml`、`ansible/inventory/tailscale.yml`、`ansible/roles/k3s-server/templates/config.yaml.j2`、`terraform/main.tf`
- **Findings**:
  - `terraform/main.tf:5-7` — `locals.nodes` は `{ "prod-node-1" = { private_ip = "10.0.1.1" } }` という map of object。値がオブジェクトであるため `server_type` フィールドの追加は既存構造の自然な拡張になる
  - `terraform/main.tf:4` — 「HA 復帰時は prod-node-2, prod-node-3 を追加する」というコメントが存在する
  - `terraform/main.tf:64-67` — ネットワーク接続は `hcloud_server` 内の `network` ブロックとしてインライン定義されており、`hcloud_server_network` の独立リソースは不要
  - `terraform/main.tf:80-89` — `lifecycle.ignore_changes` が `user_data` を対象に含むため、`tailscale_tailnet_key` の再生成が既存ノードの再作成を引き起こさない
  - `ansible/playbooks/k3s-bootstrap.yml:39-47` — Play 2 が `hosts: k3s_server_worker`、`serial: 1` で存在する。対応グループが未定義のため現状 no-op
  - `ansible/roles/k3s-server/templates/config.yaml.j2:44-49` — `k3s_cluster_init` が false の場合、`groups['k3s_server'][0]` の private IP を join 先として server モードで参加する
  - `ansible/inventory/tailscale.yml:20` — `k3s_version: v1.36.3+k3s1`
- **Implications**: 要件 2 の実装は `locals.nodes` への `server_type` 追加と inventory へのグループ追加が主体。新規ロールの実装は不要

### tls-san の分岐による到達性の制約

- **Context**: 3 ノード構成中に prod-node-1 が停止した際、kubectl が他ノードへ到達できるかを確認する
- **Sources Consulted**: `ansible/roles/k3s-server/templates/config.yaml.j2`
- **Findings**:
  - `config.yaml.j2:36-43` — `tls-san` は `{% if k3s_cluster_init %}` ブロックの内側にのみ存在し、`ansible_host` (Tailscale MagicDNS) と `k3s_private_ip` を設定する
  - 追加 server ノードは else 分岐へ入るため `tls-san` が渡らず、API 証明書の SAN に Tailscale 名が含まれない
  - `config.yaml.j2:46-48` — join 先は `groups['k3s_server'][0]` 固定。prod-node-1 停止中は新規ノードが join できない
- **Implications**: この状態では prod-node-1 障害時に kubeconfig の接続先を切り替えられず、要件 4 が意図する継続性が運用面で成立しない。`tls-san` を分岐の外へ出す必要がある

### DR 自動復旧機構の 3 ノード構成での挙動

- **Context**: 要件 6 が 3 ノード構成での DR 挙動の定義を求める
- **Sources Consulted**: `.github/scripts/dr-trigger.sh`、`.github/scripts/recovery.sh`、`.github/workflows/dr-trigger.yml`、`terraform/providers.tf`
- **Findings**:
  - `dr-trigger.sh:19` — 監視対象は `TARGET_HOSTNAME="${DR_TRIGGER_TARGET_HOSTNAME:-prod-node-1}"`。`dr-trigger.yml:34-51` に上書きする環境変数がないため、実運用では `prod-node-1` 決め打ちで動作する
  - `dr-trigger.sh:38-50` — 判定は (a) Tailscale 上で対象ホストがオフライン、または (b) 固定 3 エンドポイントのうち 2 つ以上が応答なし、のいずれかで `NodeFailureSuspected` となる
  - `dr-trigger.sh:104` — Tailscale 判定は `select(.hostname == $h)` でホスト名に依存する。etcd クォーラムの健全性は判定に一切含まれない
  - `recovery.sh:172-229` — 復旧は Terraform Cloud の `/runs` API に対する plain run の作成であり、`is-destroy=false`、`auto-apply=true`。`-target` は使用されていない
  - `terraform/providers.tf:45-50` — TFC ワークスペースは `aramakisai-infra` の単一構成で、hcloud・tailscale・cloudflare・authentik の全リソースが同一 state に含まれる
  - `recovery.sh:144-160` — Tailscale デバイス削除も `hostname == "prod-node-1"` 決め打ちで、環境変数による上書き機構がない
  - `recovery.sh:270-309` — Ansible は `ansible/inventory/tailscale.yml` を使用する。inventory が 3 ノードへ更新されていれば、その内容に従って実行される
  - 恒常的な無効化手段は存在しない。実装されているのは猶予期間中の Issue コメントによる中止 (`dr-trigger.sh:58-67, 239-242`) と Issue クローズ (`:225-237`) のみで、いずれもインシデント発生後の個別対応である
- **Implications**:
  - prod-node-1 が停止すると、残り 2 台で etcd クォーラムが維持されサービスが継続していても DR が発火する。3 ノード化の効果を自動復旧機構自身が打ち消す
  - 発火した場合の apply は TFC ワークスペース全体が対象であり、authentik の到達不能により run 自体が失敗する可能性がある
  - 以上より、スケールアウト期間中は DR を停止する以外に安全な選択肢がない

### Terraform の apply 対象範囲

- **Context**: 要件 2.7 / 2.8 が `-target` による限定を求める
- **Sources Consulted**: `terraform/main.tf`、`terraform/network.tf`、`terraform/tailscale.tf`、`terraform/firewall.tf`、`terraform/dns.tf`
- **Findings**:
  - `hcloud_server.nodes` の依存は `hcloud_network_subnet.nodes`、`hcloud_firewall.k3s_nodes`、`hcloud_ssh_key.default` / `.ci`、`tailscale_tailnet_key.k3s_nodes`。Terraform の `-target` はこれらを自動的に含む
  - `terraform/network.tf` — `hcloud_network.main` が `10.0.0.0/16`、`hcloud_network_subnet.nodes` が `10.0.1.0/24`。prod-node-1 が `.1` を使用しており、アドレスの枯渇懸念はない
  - `terraform/tailscale.tf:1-9` — auth key は `reusable = true` で、コメントに 3 ノードを 1 つのキーで扱う旨が明記されている
  - `terraform/dns.tf:88-115` — `hcloud_server.nodes["prod-node-1"]` を明示参照しており、ノード追加による変更は生じない
  - `hcloud_placement_group` はリポジトリ内に定義が存在しない
  - `terraform/firewall.tf` のルールはノード非依存であり、メール関連ポートが追加ノードにも開放される
- **Implications**:
  - `-target` は `hcloud_server.nodes["prod-node-2"]` と `hcloud_server.nodes["prod-node-3"]` の 2 つで足りる
  - placement group の不在は本 spec の目的に直接抵触する。Hetzner の spread 型 placement group を導入しない限り、3 台が同一物理ホストに配置される可能性を排除できない
  - メール関連ポートの開放は mailserver が prod-node-1 固定である以上実害は限定的だが、一時ノードに不要な開放面が生じる事実は記録しておく

### etcd スナップショットの設定状況

- **Context**: 要件 3.5 / 4.6 / 5.6 がスナップショットを前提にしている
- **Sources Consulted**: `ansible/roles/k3s-server/` 配下一式
- **Findings**:
  - `etcd-snapshot-schedule-cron`、`etcd-snapshot-retention`、`etcd-s3` 系の設定は一切存在しない。ロールに `defaults/` ディレクトリ自体がない
  - スナップショットは K3s の既定動作 (ローカルディスクへの定期取得) に委ねられており、外部ストレージへの退避設定はない
- **Implications**:
  - スナップショットはノードのローカルディスク上にのみ存在する。prod-node-1 が失われた場合、そのノードのスナップショットも同時に失われる
  - 要件 5.6 の「スナップショットからの復旧」を成立させるには、縮退前に取得したスナップショットをノード外へ退避する手順が必要

### 負荷テストツールの選定

- **Context**: 要件 1 がブレークポイント探索を求める。リポジトリに既存の負荷テスト資産はない
- **Sources Consulted**: Grafana k6 公式ドキュメント (Breakpoint testing / Test for performance)
- **Findings**:
  - ブレークポイント探索には `ramping-arrival-rate` executor が公式に推奨される。plateau や ramp-down を設けず負荷を上げ続ける構成を取る
  - `thresholds` に `abortOnFail: true` を設定すると、閾値超過時点でテストが自動停止する
  - 到達負荷ではなく到達レートを保証する executor であるため、システムが遅くなっても負荷の印加量が落ちない
- **Implications**:
  - 本番クラスタへ負荷を印加する要件 1.6 において `abortOnFail` は必須。閾値超過後も負荷を掛け続けることを防ぐ
  - Docker で実行できるため、クラスタへの常駐コンポーネント追加は不要。要件 1.7 の制約と整合する

### 増減手順の検証環境の候補

- **Context**: 要件 3 がノード増減双方の手順を本番以外で確立することを求める
- **Sources Consulted**: `.github/scripts/dr-k3d-setup.sh`、`.github/scripts/dr-kvm-create.sh`、`.github/scripts/dr-local-test.sh`
- **Findings**:
  - k3d ベースの検証基盤が既に存在し、ArgoCD・Secret・namespace の準備を自動化している
  - KVM ベースの検証基盤も存在し、Debian 13 の VM を作成して prod-node-1 相当の環境を構築する
  - `recovery.sh` のステップ 1 に Tailscale デバイス削除が実装済みで、削除処理の参照実装になる
- **Implications**: 検証環境をゼロから構築する必要はない。ただし k3d は Hetzner private network・Tailscale・cloud-init を再現しないため、何をどこで検証するかの切り分けが必要

### 外部公開経路の実態

- **Context**: 負荷分散の対象を決めるため、外部トラフィックがどの経路で K3s へ到達するかを確認する
- **Sources Consulted**: `terraform/tunnel.tf`、`terraform/dns.tf`、`gitops/apps/prod/nginx-ingress.yaml`、`gitops/manifests/prod/autoconfig/ingress.yaml`
- **Findings**:
  - `terraform/tunnel.tf:16-68` — Cloudflare Tunnel の ingress ルールは webmail・argocd・idp・stg・cms・presence・vault のすべてを `*.svc.cluster.local` の ClusterIP Service へ直接転送する
  - `terraform/dns.tf:26-29` — 公開 Web サイトの apex は `cloudflare_workers_domain` により Cloudflare Workers で配信される。K3s 上では動作しない
  - `ingressClassName: nginx` を用いる Ingress は autoconfig の 1 件のみ
  - `gitops/apps/prod/nginx-ingress.yaml:26-28` — nginx-ingress は `nodeSelector` で prod-node-1 に固定され `hostNetwork: true` でポート 443 と 80 を bind する
- **Implications**:
  - 開催期間中に K3s へ到達する来場者トラフィックは CMS の API に限られる。負荷テストの対象もこれに絞る
  - nginx-ingress の配置変更は負荷分散の手段として効果を持たない。本設計では扱わない

### 稼働中ワークロードの全数確認

- **Context**: 冗長化の対象を決めるため、各ワークロードが実際に稼働しているかを確認する
- **Sources Consulted**: `gitops/manifests/prod/` 配下の全 Deployment / StatefulSet、`gitops/apps/prod/room-presence.yaml`、各 `db-cluster.yaml`
- **Findings**:
  - 稼働中 — cms、zitadel、mailserver、roundcube、autoconfig が `replicas: 1`、cloudflared が `replicas: 2`
  - 停止中 — authentik の ldap-outpost と redis が `replicas: 0`、vaultwarden と vaultwarden-rbac-sync が `replicas: 0`
  - `gitops/apps/prod/room-presence.yaml:13-17` — room-presence は 2026-07-12 から凍結。`replicaCount: 0`、`migration.enabled: false`、`midnightReset.suspend: true`、DB は hibernation。再開手順がコメントに記載されている
  - CNPG は 5 クラスタとも `instances: 1`。うち稼働中のワークロードに属するのは cms-db と zitadel-db の 2 つ
  - ストレージは cms-db が 10Gi、他が 5Gi
- **Implications**:
  - 冗長化の対象は cms-db と zitadel-db に限る。停止中のワークロードに属する DB を冗長化してもディスクを消費するだけで効果がない
  - 増加分はノードあたり 20Gi 程度であり、追加ノードのディスクに対して余裕がある

### placement group の制約と既存サーバーの扱い

- **Context**: `hcloud_placement_group` が未定義であり、3 台が同一物理ホストへ配置されれば etcd 3 台化の効果が失われる
- **Sources Consulted**: [Hetzner Docs — Placement Groups Overview](https://docs.hetzner.com/cloud/placement-groups/overview/)、[同 FAQ](https://docs.hetzner.com/cloud/placement-groups/faq/)、terraform-provider-hcloud の `internal/server/resource.go`
- **Findings**:
  - type は `spread` の 1 種類のみ。同一グループの仮想サーバーはすべて異なる物理サーバー上で動作する
  - 保証されるのはホスト単位の分散まで。ホスト間の物理的距離は制御されず、ロケーション全体の障害に対しては保証されない
  - 上限は 1 グループ 10 台、1 サーバーは 1 グループのみ、1 プロジェクト 50 グループ。料金は無料
  - 対象は Cloud Server のみで Volume や Load Balancer には適用できない
  - FAQ の "What about existing servers?" に「既存サーバーの追加は可能だが、追加するにはオフラインである必要がある」と明記されている
  - `resourceServerUpdate` に `if d.HasChange("placement_group_id") { setPlacementGroup(...) }` の分岐が存在し、`placement_group_id` は ForceNew ではない。サーバーの再作成は発生しない
  - 空きホストが枯渇した場合の作成失敗時の挙動とエラーコードは、overview と FAQ のいずれにも記載がなく確認できていない
- **Implications**:
  - 追加ノードは新規作成時に指定するため停止を伴わない
  - prod-node-1 への適用は停止を伴うが、再作成ではない。3 ノードかつ CNPG 冗長化後であれば、停止中も etcd はメンバー 2 でクォーラムを維持し DB はフェイルオーバーする
  - 順序を工夫することで、実質的な停止を mailserver のみに限定できる

## Architecture Pattern Evaluation

縮退手順の検証環境について 3 案を比較した。

| Option | Description | Strengths | Risks / Limitations | Notes |
|--------|-------------|-----------|---------------------|-------|
| A: k3d のみ | 既存の k3d 基盤を拡張し multi-server クラスタで etcd 増減を検証 | 追加コストなし、反復が容易、既存資産と同じ流儀 | private network・Tailscale・cloud-init を再現しない。`node-ip` 固定の本番設定と差異が残る | 手順の骨格確立には十分 |
| B: Hetzner のみ | 本番と同一経路で検証用クラスタを構築 | 本番と同条件。Hetzner 固有の落とし穴を実地で確認できる | TFC state の分離が必要。authentik による plan 失敗と切り分けが困難。prod 前提リソースを巻き込む恐れ | 単独採用は準備コストが高い |
| C: ハイブリッド | 手順の核を A で確立し、Hetzner 固有工程のみ B の最小構成で一度通す | 反復部分を安く、差異の大きい部分のみ実環境で押さえられる | 2 環境にまたがるため検証範囲の切り分けを誤ると空白が残る | 採用 |

## Design Decisions

### Decision: 縮退手順の検証をハイブリッド構成で行う

- **Context**: 要件 3 が本番以外での手順確立を求める一方、k3d は Hetzner 固有要素を再現しない
- **Alternatives Considered**:
  1. k3d のみ — 安価だが Tailscale デバイス残存や private IP 割当を検証できない
  2. Hetzner のみ — 忠実だが TFC state 分離の準備コストが高く、authentik 起因の失敗と切り分けにくい
- **Selected Approach**: etcd メンバーの増減という反復を要する部分を k3d で確立し、Tailscale デバイス削除・private IP 割当・cloud-init の 3 点のみを Hetzner の最小構成で一度確認する
- **Rationale**: 試行回数が必要な領域と本番差異が大きい領域が分離できるため、双方の弱点を補える
- **Trade-offs**: 手順書が 2 つの環境にまたがるため、検証済み範囲の記述を明示する必要がある
- **Follow-up**: k3d の multi-server クラスタが embedded etcd の増減を本番同等に再現するかを、検証の最初の工程で確認する

### Decision: スケールアウト期間中は DR 自動復旧を停止する

- **Context**: `dr-trigger.sh` は prod-node-1 の停止を etcd クォーラムと無関係に障害と判定し、`recovery.sh` は TFC ワークスペース全体へ auto-apply でフル apply を行う
- **Alternatives Considered**:
  1. `dr-trigger.sh` を 3 ノード対応に改修する — 判定ロジックと `recovery.sh` の双方に手を入れる必要があり、変更規模がイベント直前の作業として過大
  2. `DR_TRIGGER_TARGET_HOSTNAME` を存在しないホスト名へ向ける — 停止は達成できるが意図が読めず、戻し忘れのリスクが高い
  3. ワークフローを無効化する — 停止範囲が明確で、復帰操作も単純
- **Selected Approach**: スケールアウト期間中は `dr-trigger.yml` の発火を Git 管理下のフラグまたは条件分岐により停止し、縮退完了後に戻す。GitHub の UI 操作のみで完結する手段は Git に痕跡が残らず再有効化の失念を履歴から検知できないため採らない
- **Rationale**: 3 ノード構成では単一ノード障害でサービスが継続するため自動復旧の必要性が下がる。一方で誤発火した場合の影響が全ワークスペースへの無条件 apply と極めて大きく、停止による損失より誤発火による損失が上回る
- **Trade-offs**: 期間中は自動復旧が働かないため、2 台以上が同時に停止した場合は手動対応になる。この期間は人が張り付いているイベント期間と重なるため許容する
- **Follow-up**: 無効化と再有効化を作業チェックリストの必須項目として扱う。戻し忘れを防ぐ仕組みを設計で扱う

### Decision: tls-san を全 server ノードへ適用する

- **Context**: 追加ノードに `tls-san` が渡らないため、prod-node-1 障害時に kubeconfig を他ノードへ向けられない
- **Alternatives Considered**:
  1. 現状維持 — 3 ノード化しても運用上の到達性が確保されず、要件 4 の意図が達成されない
  2. 追加ノード専用の設定を別途持たせる — 分岐が増え、ノード種別ごとに設定が分かれる
  3. `tls-san` を分岐の外へ出す — 全 server ノードが同じ SAN 構成を持つ
- **Selected Approach**: `tls-san` を `k3s_cluster_init` の分岐外へ移動し、全 server ノードに適用する
- **Rationale**: SAN の追加は証明書に名前を足すだけで、既存の接続経路を変更しない。ノード種別による設定差異をなくす方が構成として単純
- **Trade-offs**: prod-node-1 の設定ファイルに差分が生じるため、要件 2.2 の「差分を出さない」対象が Terraform に限られることを明示する必要がある
- **Follow-up**: 既存ノードへの適用が証明書の再生成や K3s の再起動を伴うかを、k3d 環境で事前に確認する

### Decision: placement group を導入し、prod-node-1 は冗長化後に停止して追加する

- **Context**: `hcloud_placement_group` が未定義であり、prod-node-1 と追加ノードが同一物理ホストに載れば、そのホストの障害で 2 台が同時に失われ etcd クォーラムを喪失する
- **Alternatives Considered**:
  1. 導入しない — 同居の可能性を排除できない。Hetzner はホストの割り当てを公開しないため、他に確認手段がない
  2. 追加ノードのみを所属させる — 追加 2 台は互いに分散するが、prod-node-1 との同居リスクが残る
  3. 全ノードを所属させ、prod-node-1 はスケールアウトの最初に停止して追加する — 全サービスの停止を伴う
  4. 全ノードを所属させ、prod-node-1 はデータ層の冗長化完了後に停止して追加する
- **Selected Approach**: 4 を採る。追加ノードは新規作成時に placement group を指定し、prod-node-1 は 3 ノード構成かつ CNPG 冗長化が完了した後に停止して追加する
- **Rationale**: 既存サーバーの追加にはオフラインであることが要求されるが、冗長化が完了していれば停止中も etcd はメンバー 2 でクォーラムを維持し、データベース接続は Standby が引き継ぐ。順序の工夫だけで実質的な停止を mailserver に限定できる
- **Trade-offs**: prod-node-1 の停止中は etcd がメンバー 2 となり、この区間の耐障害性は単一ノード構成より低い。停止時間を最小に保つ。また mailserver はこの区間で停止する
- **Follow-up**: 停止と追加の所要時間を検証環境で測り、作業計画に織り込む。追加に失敗した場合は prod-node-1 を起動して従前の状態へ戻し、追加ノードのみが所属する状態を記録する

### Decision: 負荷テストは k6 を Docker で実行し abortOnFail を設定する

- **Context**: 要件 1 が本番クラスタへのブレークポイント探索を求める。要件 1.7 が新規監視基盤の導入を禁じる。測定対象は CMS の API であり、公開 Web サイトは Workers で配信されるため含まない
- **Alternatives Considered**:
  1. `hey` や `oha` などの単純なツール — 段階的なレート上昇と複数リクエストのシナリオ表現が難しい
  2. k6 Operator によるクラスタ内実行 — 常駐コンポーネントが増え、要件 1.7 の趣旨に反する
  3. k6 を Docker で外部から実行 — 常駐物なし、シナリオ記述が可能
- **Selected Approach**: k6 を Docker で実行し、`ramping-arrival-rate` executor と `abortOnFail: true` を設定した `thresholds` を用いる
- **Rationale**: 本番クラスタへ負荷を印加する以上、閾値超過時の自動停止は安全装置として不可欠である
- **Trade-offs**: テスト実行元のネットワーク帯域が測定上限を規定する可能性がある
- **Follow-up**: 測定値が実行元の回線に律速していないかを、オリジン直測定時に確認する

### Decision: 縮退前に etcd スナップショットをノード外へ退避する

- **Context**: スナップショットの明示設定がなく、K3s の既定でローカルディスクにのみ保存される
- **Alternatives Considered**:
  1. 既定動作に委ねる — ノード喪失時にスナップショットも失われ、要件 5.6 が成立しない
  2. S3 への定期退避を設定する — 恒久的な改善だが本 spec のスコープを超える
  3. 作業前に手動で取得し手元へ退避する — 作業手順に閉じる
- **Selected Approach**: スケールアウト前と縮退前に手動でスナップショットを取得し、ノード外へ退避する手順を作業チェックリストに含める
- **Rationale**: 本 spec が必要とするのは特定の作業時点における復旧手段であり、継続的なバックアップ体制の構築は別の課題である
- **Trade-offs**: 手動操作であるため実行漏れが起こりうる。チェックリストの必須項目として扱う
- **Follow-up**: S3 への定期退避は別 spec として起票を検討する

### Decision: データ層の冗長化対象を稼働中の DB クラスタに限定する

- **Context**: CNPG は 5 クラスタ存在するが、うち 3 つは停止中のワークロードに属する
- **Alternatives Considered**:
  1. 全 5 クラスタを冗長化する — 停止中のワークロードには効果がなく、ディスクを消費するのみ
  2. cms-db のみ冗長化する — 来場者経路は守られるが、zitadel の停止で当日のスタッフ操作が止まる
  3. cms-db と zitadel-db を冗長化する
- **Selected Approach**: cms-db と zitadel-db の 2 クラスタを `instances: 3` とする。authentik-db・vaultwarden-db・room-presence-db は据え置く
- **Rationale**: 来場者トラフィックが到達するのは CMS の API のみだが、zitadel が停止すると CMS 管理画面と ArgoCD へログインできず、当日の情報更新や障害対応が行えない。両者を組にして初めて運用が継続する
- **Trade-offs**: 停止中ワークロードの DB は prod-node-1 の停止とともに失われるが、稼働していないため実害がない
- **Follow-up**: 冗長化の前に各ノードのディスク空き容量を確認する

### Decision: 追加ノードに taint を設定しない

- **Context**: 追加ノードに taint がないため、Pod の再起動や再スケジュールにより意図しないワークロードが移動しうる
- **Alternatives Considered**:
  1. `NoSchedule` taint を付与し、配置対象にのみ toleration を与える — 配置を厳密に制御できるが、データ層冗長化により CNPG インスタンスを追加ノードへ置く必要があり、toleration の付与対象が広がる
  2. taint を設定しない — 制御は緩いが変更点が少ない
- **Selected Approach**: taint を設定しない。etcd への影響は `config.yaml.j2` の `system-reserved` と `kube-reserved` による予約で抑える
- **Rationale**: 追加ノードは 16GB であり、意図しない Pod が載っても etcd を圧迫する余地が大きい。厳密な制御のために toleration を広く配る方が、設定の追加と縮退時の除去という往復を増やす
- **Trade-offs**: 「測定結果を見てから配置を決める」という原則が、スケジューラの自然な分散によって部分的に先取りされる。この逸脱を受容する
- **Follow-up**: 3 ノード構成の稼働中に、意図しないワークロードが追加ノードへ移動していないかを確認する

## Risks & Mitigations

- **DR 自動復旧の誤発火によるワークスペース全体への apply** — スケールアウト期間中はワークフローを無効化する。無効化と復帰を作業チェックリストの必須項目にする
- **prod-node-1 の placement group 追加に伴う停止** — 再作成は発生しないが停止は必要。データ層の冗長化完了後に実施し、停止中も etcd メンバー 2 でクォーラムを維持する。mailserver はこの区間で停止する。失敗時は起動して従前の状態へ戻す
- **縮退操作によるクォーラム喪失** — 要件 3 の検証を経ていない手順では実行しない。作業前のスナップショット退避を必須とする
- **`terraform plan` が authentik により失敗し差分確認が限定的になる** — `-target` で対象を限定する。限定範囲外の差分が確認できない事実を作業記録に残す
- **Tailscale デバイスの削除漏れ** — 次回のノード作成時に別名で登録され Ansible が接続できなくなる。縮退チェックリストの必須項目にする
- **負荷テストが本番サービスに影響する** — 利用の少ない時間帯に実施し、`abortOnFail` で自動停止させる。事前に CNPG バックアップの正常性を確認する
- **CNPG フェイルオーバー時の書き込み損失** — 同期レプリケーションを設定しない限り、Primary 障害時に直近の書き込みが失われる可能性がある。設定の要否は検証環境で挙動を確認したうえで判断する
- **縮退時の Primary の所在** — Primary が削除対象ノード上にあるまま縮退すると、昇格先のない状態でデータを保持するインスタンスが失われる。switchover を縮退手順の必須工程とする
- **一時ノードへのメール関連ポート開放** — firewall ルールがノード非依存のため追加ノードにも開く。mailserver が prod-node-1 固定であるため実害は限定的だが、一時ノードの稼働期間が短いことを前提とする

## References

- [k6 Breakpoint testing](https://grafana.com/docs/k6/latest/testing-guides/test-types/breakpoint-testing) — `ramping-arrival-rate` によるブレークポイント探索の公式パターン
- [k6 Test for performance](https://grafana.com/docs/k6/latest/examples/get-started-with-k6/test-for-performance) — `thresholds` と `abortOnFail` の設定方法
- `.kiro/specs/single-node-migration/` — 現構成の成立経緯とコールドスタンバイ復旧の設計
- `.kiro/specs/observability-v2/` — DR 検知経路の GitHub Actions 完結型への移行
- `.kiro/specs/festival-peak-scaleout/gap-analysis.md` — 要件と既存資産の対応表
