# Gap Analysis - festival-peak-scaleout

> **訂正注記**: 本文書は初期調査時点の記録であり、以下 2 点の誤りを含む。結論は `research.md` および `design.md` を参照すること。
> 1. 「`local-path` によりステートフルワークロードが分散できない」— 誤り。CNPG は `instances` を増やすことで各インスタンスが自ノードの PV を使う形で冗長化でき、共有ストレージを要しない
> 2. 「外部公開経路が nginx-ingress を中心とする」— 誤り。Cloudflare Tunnel は各 ClusterIP へ直接転送し、公開 Web サイトは Cloudflare Workers で配信される
>
> また調査時点でワークロードの稼働状態を確認しておらず、停止中の authentik・vaultwarden・room-presence を稼働中として扱っている箇所がある。

## Summary

- **Discovery Scope**: Extension (既存の Terraform / Ansible 資産を条件付きで拡張する。新規サブシステムの構築は伴わない)
- **Key Findings**:
  - Ansible 側は 3 ノード構成を**既に想定して作られている**。`k3s-bootstrap.yml` の Play 2 が `k3s_server_worker` グループを `serial: 1` で待機しており、`config.yaml.j2` も追加 server の join 分岐を実装済み。inventory にグループが無いため現状は no-op になっている
  - 一方で `tls-san` が `cluster_init` の分岐内にしか無く、追加ノードの API 証明書に Tailscale 名と private IP が含まれない。kubeconfig の接続先を追加ノードへ切り替える経路が塞がっている
  - 縮退検証に使える基盤 (`dr-k3d-setup.sh` / `dr-kvm-create.sh` / `dr-local-test.sh`) が既に存在する。検証用に Hetzner リソースを新規調達しなくても手順確立が可能
  - 負荷テストの資産はリポジトリに存在しない。要件 1 は完全な新規追加になる
  - 要件 7 (ワークロード分散) の手本は `cloudflared` の `topologySpreadConstraints` のみ。ストレージが `local-path` である制約は本 spec では解消しない
  - `terraform plan` は対象を限定しない限り失敗する。authentik provider の向き先 `idp.aramakisai.com` が Zitadel へ切り替わっており、残存する authentik リソースが API へ到達できない。要件 2 の差分確認は `-target` 前提になる
  - authentik の Pod は `replicas: 0` へスケールダウン済みで、その理由として **prod-node-1 のメモリ逼迫解消**が明記されている。メモリ制約は推測ではなく既に観測・対処された事実である

## Research Log

### Ansible の追加ノード対応状況

- **Context**: 要件 2 と 4 が追加 server ノードの join を要求する。実装コストの見積もりが必要
- **Findings**:
  - `ansible/playbooks/k3s-bootstrap.yml:39-47` — Play 2 が `hosts: k3s_server_worker`、`serial: 1` でローリング実行する形で存在する
  - `ansible/inventory/tailscale.yml:13-23` — `k3s_server` に prod-node-1 のみ。`:15-16` のコメントが `k3s_server_worker` の追加を想定している
  - `ansible/roles/k3s-server/templates/config.yaml.j2:44-49` — `k3s_cluster_init` が false の場合 `server: "https://{{ hostvars[groups['k3s_server'][0]]['k3s_private_ip'] }}:6443"` を設定し、追加 server として join する
  - Play 0 (`:19-26`) の swap / os-auto-update は `hosts: all` でノード名非依存
  - Cilium / cloudflared / ArgoCD の各 Play は `run_once: true` のため多重実行されない
  - `ansible/roles/` に `k3s-agent` は存在しない (`structure.md:37` の記述と実装が不一致)
- **Implications**: 追加実装はほぼ不要。inventory へのグループ追加が主作業になる。agent ロールの新規実装は本 spec では不要

### tls-san の分岐による制約

- **Context**: 3 ノード構成中に prod-node-1 が停止した場合の kubectl 到達性を確認する必要がある
- **Findings**:
  - `config.yaml.j2:36-43` — `tls-san` は `{% if k3s_cluster_init %}` ブロック内にのみ存在する。設定値は `ansible_host` (Tailscale MagicDNS) と `k3s_private_ip`
  - 追加 server ノードには `tls-san` が渡らないため、API 証明書の SAN に Tailscale 名が含まれない
  - `server:` の join 先は `groups['k3s_server'][0]` すなわち prod-node-1 固定。prod-node-1 が停止している間は新規ノードが join できない
- **Implications**:
  - prod-node-1 障害時、etcd クォーラムは 2/3 で生存し API server も動くが、Tailscale 経由の kubeconfig を追加ノードへ向けると TLS 検証に失敗する
  - 要件 4 が意図する「単一ノード障害でクラスタが停止しない」を運用面で成立させるには `tls-san` を分岐の外へ出す必要がある
  - これは既存ノードの設定変更を伴うため、要件 2 の「prod-node-1 に差分を出さない」と衝突しうる。設計フェーズで扱う

### 縮退検証に使える既存資産

- **Context**: 要件 3 が本番以外での縮退手順確立を求める
- **Findings**:
  - `.github/scripts/dr-k3d-setup.sh` — k3d クラスタに ArgoCD / infisical-auth / Deploy Key / prod namespace を用意する。`recovery.sh` のローカルテスト前提条件を整える用途
  - `.github/scripts/dr-kvm-create.sh` — Debian 13 の KVM VM を作成し prod-node-1 相当の環境を作る。libvirt が前提
  - `.github/scripts/dr-local-test.sh` / `dr-k3d-fullstack.sh` — ローカル DR テストの実行系
  - `.github/scripts/recovery.sh` — ステップ 1 に Tailscale デバイス削除が実装済み (`dr.md:153` 参照)
- **Implications**: 検証環境をゼロから作る必要はない。ただし k3d はコンテナベースで Hetzner private network と Tailscale を再現しないため、何をどこで検証するかの切り分けが要る

### etcd スナップショットの設定状況

- **Context**: 要件 3.5 / 4.6 / 5.6 がスナップショットの取得と復旧を前提にしている
- **Findings**:
  - `config.yaml.j2` に `etcd-snapshot-*` 系の設定は存在しない。K3s の既定動作に依存している
  - S3 等へのスナップショット外部保存の設定も存在しない
  - CNPG のバックアップは別途 S3 へ継続アーカイブされている (`backup` spec)
- **Implications**: スナップショットの保存先・世代数・取得タイミングが未確認。**Research Needed**

### 負荷テスト資産

- **Context**: 要件 1 の実装コスト見積もり
- **Findings**: k6 / vegeta / locust 等のツール設定・シナリオ・CI 定義はリポジトリに存在しない
- **Implications**: 完全な新規追加。ただし要件 1.7 が新規監視基盤の導入を禁じているため、計測側は Netdata と `kubectl top` に限定される

### DR 自動復旧機構

- **Context**: 要件 6 が 3 ノード構成での DR 挙動の定義を求める
- **Findings**:
  - `dr.md:8-24` — `dr-trigger.yml` が 5 分毎 cron でクラスタ外から複合検出 (Tailscale オフライン、または idp/argocd/webmail のうち 2 つ以上が同時応答なし) を行い、Discord 通知と猶予期間を経て `dr-recovery.yml` を発火する
  - 判定ロジックは `.github/scripts/dr-trigger.sh` にあり、ユニットテスト `scripts/test-dr-trigger-logic.sh` が存在する
  - 検出条件が prod-node-1 のみを対象としているかは未確認
- **Implications**: 3 ノード構成では単一ノード障害でもサービスが継続するため誤検出は起きにくいが、`recovery.sh` が prod-node-1 の再作成を行う前提で書かれている場合、3 ノード構成に対して破壊的に作用する可能性がある。**Research Needed**

### Terraform の apply 可能性

- **Context**: 要件 2 が `terraform plan` の差分確認を受け入れ基準にしている
- **Findings**:
  - `terraform/providers.tf:25-28,71-75` — `provider "authentik"` は条件分岐なしで宣言されている。enable フラグの類は実装されていない
  - `terraform/authentik_main.tf` の data source 6 件、`terraform/authentik_imports.tf` の `import` ブロック 11 件を含め、authentik リソースは無条件で残存する
  - `terraform/variables.tf:110` — `authentik_url` の既定値は `https://idp.aramakisai.com`
  - `terraform/tunnel.tf:34-41` — 同ホストの cloudflared backend は Zitadel (`zitadel.zitadel.svc.cluster.local`) を指す。authentik provider は Zitadel を叩くことになる
  - `idp-migration-zitadel` の実測記録では、対象を限定しない `terraform plan` が API の 502 により失敗し、`zitadel_*.tf` / `access.tf` へ `-target` を指定した plan のみ成功している
  - 同 spec は `phase: completed` だが、tasks.md 本文に「旧 authentik 資産の一括撤去」が未採番・未着手の積み残しとして複数箇所に明記されている
  - アプリケーション側は `gitops/helm-values/prod/authentik.yaml` で `server.replicas: 0` / `worker.replicas: 0`。ArgoCD Application と CNPG DB は削除されていない
- **Implications**:
  - 要件 2.7 / 2.8 の `-target` 限定は必須の前提であり、任意の回避策ではない
  - authentik 資産の撤去は本 spec のスコープ外だが、未着手である限り `terraform plan` は常に限定実行を強いられる。要件 2.2 の「prod-node-1 に差分を出さない」確認も限定した範囲内でしか成立しない
  - 撤去タスクの扱いは別 spec として起票するのが妥当

## Requirement-to-Asset Map

| 要件 | 既存資産 | ギャップ | 種別 |
|---|---|---|---|
| 1 ベースライン測定 | Netdata、`make kubectl` | 負荷テストツール・シナリオ一式 | Missing |
| 2 インフラコード対応 | `main.tf` の `for_each`、firewall/Tailscale key の全ノード適用 | `server_type` のノード別指定、inventory グループ | Missing (軽微) |
| 2 (同上) | `-target` による限定実行の先例 (idp-migration-zitadel) | 対象無限定の `terraform plan` は authentik の残存により失敗する | Constraint |
| 3 縮退検証 | `dr-k3d-setup.sh`、`dr-kvm-create.sh`、`recovery.sh` の Tailscale 削除 | 縮退手順そのもの、検証対象の切り分け | Missing |
| 3 (同上) | — | etcd スナップショットの保存先・世代 | Unknown |
| 4 本番投入 | `k3s-bootstrap.yml` Play 2、`config.yaml.j2` の join 分岐 | `tls-san` が追加ノードへ渡らない | Constraint |
| 4 (同上) | — | join 先が prod-node-1 固定 | Constraint |
| 5 縮退実行 | `recovery.sh` の Tailscale デバイス削除ロジック | 段階的な etcd メンバー削除手順 | Missing |
| 6 DR 整合 | `dr-trigger.sh`、`test-dr-trigger-logic.sh` | 3 ノード構成での挙動が未定義 | Unknown |
| 7 ワークロード配置 | `cloudflared` の `topologySpreadConstraints` | ストレージが `local-path` でステートフルは分散不可 | Constraint |
| 8 ドキュメント同期 | `structure.md` の自律同期ルール | `product.md:9`、`structure.md:37`、`tech.md` のノード記述 | Missing |

## Implementation Approach Options

分岐点は要件 3 の検証環境をどこに置くかに集約される。他の要件は既存パターンの拡張で素直に収まる。

### Option A: 既存の k3d 基盤を拡張して縮退手順を検証する

`dr-k3d-setup.sh` の系統を拡張し、k3d の multi-server クラスタで 3 → 1 の etcd メンバー削除を検証する。

- **対象**: `.github/scripts/` に縮退検証スクリプトを追加
- **Trade-offs**:
  - ✅ 追加コストゼロ。ローカルで反復できるため試行回数を稼げる
  - ✅ 既存の DR テスト資産と同じ場所・同じ流儀に収まる
  - ❌ k3d はコンテナベースで Hetzner private network・Tailscale・cloud-init を再現しない
  - ❌ `node-ip` を private IP に固定する本番設定が再現されないため、etcd ピアリング周りの差異が残る

### Option B: Hetzner に使い捨てノードを調達して検証する

本番と同一の Terraform / Ansible 経路で検証用クラスタを立てる。

- **対象**: Terraform の一時的な構成、または手動プロビジョニング + 既存 Ansible ロール
- **Trade-offs**:
  - ✅ private network・Tailscale・cloud-init を含め本番と同条件
  - ✅ Tailscale デバイス残存など Hetzner 固有の落とし穴を実地で踏める
  - ❌ 既存の prod ワークスペースと state を分離する必要がある。`terraform plan` の既知の問題と絡むと切り分けが難しい
  - ❌ DNS・Cloudflare・Infisical など prod 前提のリソースを巻き込まない配慮が要る

### Option C: ハイブリッド (推奨候補)

etcd メンバーの増減という手順の核を Option A で反復確立し、Hetzner 固有の工程 (Tailscale デバイス削除、private IP 割当、cloud-init) は Option B の最小構成で一度だけ通して確認する。

- **Trade-offs**:
  - ✅ 試行回数の多い部分を安く、本番差異の大きい部分だけを実環境で押さえられる
  - ✅ Hetzner 側の滞在時間が短くコストが小さい
  - ❌ 2 つの環境を跨ぐため手順書の構成に注意が要る
  - ❌ 検証範囲の切り分けを誤ると、どちらでも確認されない領域が残る

## Effort & Risk

| 要件 | Effort | Risk | 根拠 |
|---|---|---|---|
| 1 ベースライン測定 | M | Low | 新規追加だが独立性が高く、失敗しても本番構成に影響しない |
| 2 インフラコード対応 | S | Medium | 変更自体は軽微だが、`terraform plan` の既知問題により差分確認そのものが阻害されうる |
| 3 縮退検証 | M | Medium | 既存基盤を流用できるが、検証範囲の切り分け判断が結果の信頼性を左右する |
| 4 本番投入 | S | Medium | Ansible 側が対応済み。ただし `tls-san` の扱いが未解決 |
| 5 縮退実行 | S | High | etcd クォーラムを扱う不可逆操作。要件 3 の成否に全面的に依存する |
| 6 DR 整合 | S | Medium | 調査は軽いが、見落とすと自動復旧が 3 ノード構成を破壊しうる |
| 7 ワークロード配置 | M | Low | 条件付き実施。手本があり、変更は GitOps 経由で巻き戻せる |
| 8 ドキュメント同期 | S | Low | 対象箇所は特定済み |

## Research Needed

1. **etcd スナップショットの既定動作** — 保存先・世代数・スケジュール。外部保存の要否を含め、要件 3.5 / 5.6 の前提が成立するか
2. **`dr-trigger.sh` と `recovery.sh` の 3 ノード構成での挙動** — 検出条件が prod-node-1 に限定されているか、`recovery.sh` がノード再作成時に他ノードの存在を考慮するか
3. **`-target` に含めるべきリソースの範囲** — ノード追加に関わる `hcloud_server` / `hcloud_firewall` / `tailscale_tailnet_key` 等を漏れなく指定できるか。限定範囲外に副作用が出ないか
4. **`tls-san` を全ノードへ適用した場合の既存ノードへの影響** — 設定変更が prod-node-1 の証明書再生成や再起動を伴うか
5. **k3d の multi-server クラスタが embedded etcd の増減を本番同等に再現するか** — Option A の前提
6. ~~**想定ピーク負荷の算定根拠**~~ — 解消済み。`design.md` の Performance & Scalability に算出式と係数を定義した。入力値のみ作業時に取得する

## Recommendations for Design Phase

- **推奨アプローチ**: Option C (ハイブリッド)。反復の必要な etcd 手順をローカルで固め、Hetzner 固有工程のみ実環境で一度通す
- **設計で決めるべき事項**:
  - `tls-san` を `cluster_init` 分岐の外へ出すか、追加ノード専用の設定を持たせるか。既存ノードへの影響と要件 2.2 との整合
  - 縮退時の etcd メンバー削除を `kubectl delete node` に委ねるか、明示的な手順を踏むか
  - 要件 6 の DR 無効化を、ワークフローの停止・条件分岐・環境変数のいずれで実現するか
  - 要件 1 の負荷テストを一時スクリプトとして扱うか、リポジトリに定着させるか
- **先行して着手できる項目**: 要件 1 (測定) と Research Needed の 1〜3 は他要件に依存しない。要件 2 以降の設計判断に必要な入力でもあるため優先度が高い
