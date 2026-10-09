# 性能測定記録

タスク 1.2〜1.4・5.8・5.9（CPU limit 引き上げ後および 3 ノード・CMS 3 replicas 分散後の再測定）・5.10 の測定結果の要約。対象は CMS (Payload, `cms` Deployment) の `/api/globals/festival_meta?depth=1`。実行環境は `scripts/load-test/breakpoint.js` を `grafana/k6` Docker イメージで実行したもの。

スループット・ブレークポイント・目標レート・レイテンシ・CPU/メモリ使用量の実測値、およびそれらから逆算できる比率・外挿値は公開リポジトリに記載しない。本書には手順・構成・判定の結論のみを残す。リソース設定値 (CPU limit 等、マニフェストに既にあるもの) は記載する。

# タスク 1.2: オリジン直経路のブレークポイント実測（2026-09-21）

## 事前条件

- CNPG バックアップ: `directus-db` のログで daily barman backup が `Backup completed`、WAL archiving も継続稼働していることを確認した。
- 実施時間帯は利用の少ない時間帯。

## 経路の選定

### kubectl port-forward（不採用）

オリジン直（Cloudflare 非経由）の `kubectl port-forward` 経由で測定したところ、低いレート帯で port-forward プロセスが `lost connection to pod` / `connection reset by peer` で異常終了し、k6 側に `connection refused` が大量に出た。同時間帯の CMS Pod ログにアプリケーションエラーはなく、ノード CPU/メモリにも有意な変化がなかった。port-forward 自体が律速となり、CMS の限界には到達しなかった。

低レートの範囲ではエラー 0% で、CMS・DB・ノードとも問題なく処理できた。`abortOnFail` (`error_rate` / `http_req_failed` の `rate<0.1`) が想定どおり k6 を自動終了させることも確認した。

帯域については、レスポンスサイズ (約 9.4 kB/リクエスト) と到達レートの概算から、実行元の回線帯域が律速でないことを確認した。

### 到達経路の調査

port-forward を介さない、クラスタ変更なしの経路を調査した。

- **Tailscale サブネットルート**: `Self.PrimaryRoutes` は `None`、Pod/Service CIDR の広告なし（`terraform/tailscale.tf` にも `advertise-routes` 相当の設定なし）。使えない。
- **NodePort**: `terraform/firewall.tf` に「NodePort (30xxx) は不要」と明記。`cms` Service は `ClusterIP` のみ。該当なし。
- **hostNetwork/hostPort**: nginx-ingress controller が `hostNetwork: true` で 80/443 を bind しているが、autoconfig 専用で CMS 向け Ingress は存在しない。CMS への公開経路は Cloudflare Tunnel が ClusterIP へ直結する構成のため使えない。
- **ノード上からの ClusterIP 到達性**: ノード上から `curl http://10.43.111.239:80/api/...` が `200`。Cilium の kube-proxy 代替がホストネットワークからも ClusterIP を解決する。

採用した経路は SSH ローカルポートフォワード（`ssh -L <port>:<cms-ClusterIP>:80 root@prod-node-1`、k6 は `docker run --network=host` で実行）。クラスタ側の変更が不要で、負荷生成プロセスがノード CPU を奪わない。SSH の TCP フォワードは `kubectl port-forward` より頑健である。

## 結果と律速判定

SSH 経路ではレートを上げるにつれてレイテンシの裾が大きく悪化し、エラー率が閾値を超えて `abortOnFail` が発動した。エラー種別は `connection reset by peer`（SSH トンネルのローカル終端）。発動の前後で CMS Pod の CPU 使用量は CPU limit のほぼ上限に張り付いたまま、ノード全体・DB には余裕があった。

`gitops/manifests/prod/cms/deployment.yaml` の CMS コンテナには `limits.cpu: "500m"`（`limits.memory: "512Mi"` と併記）が設定されている。

- 律速要因は **CMS Deployment の CPU limit（500m）によるコンテナ単位の cgroup スロットリング**であり、ノード全体のリソース枯渇でも計測経路（SSH トンネル）の限界でもない。
- ノードの空き CPU には余裕があるため、`limits.cpu` を引き上げればより高いレートまで処理できる可能性が高い。
- `connection reset by peer` の発生源（CMS 側のソケット切断か、スロットリングによる SSH 側のタイムアウトか）はソケットレベルまでは切り分けていない。

測定後は、オリジン直・Cloudflare 経由とも `200`、全 Pod `Running`、CMS/DB の RESTARTS 変化なし、OOMKilled なし、測定用プロセスは終了済みであることを確認した。

未確認: CPU limit 引き上げ後のブレークポイント（本タスクはクラスタ変更禁止のため未実施。タスク 5.9 で測定）、p99 レスポンスタイム（k6 既定サマリに含まれず未取得）。

# タスク 1.3: Cloudflare 経由経路の測定とエッジキャッシュ効果の確認（2026-09-21）

対象 API に対し、負荷印加の前後を含め計 9 回 `curl` でヘッダを確認し、全回で `cf-cache-status: DYNAMIC`（`HIT` なし）だった。**この API はエッジキャッシュされておらず、毎回オリジンへ到達する。** エッジキャッシュによるオリジン負荷の吸収は 0% である。

ブレークポイントに遠く及ばない低レート・短時間（40 秒のランプ）で Cloudflare 経由の負荷をかけたところ、403/429/5xx や WAF ブロックはなかった。CMS Pod の CPU はアイドルから明確に上昇し、リクエストがオリジンまで到達して実処理されていることを確認した。リクエストあたりの CPU 消費はオリジン直と近い水準で、Cloudflare 経由であることによる追加のオリジン負荷は観測されなかった。CDN 経由でもオリジン保護効果は期待できない。

未確認: Cloudflare の WAF/レート制限の具体的な閾値（今回の低レート測定では抵触しなかったという事実のみ）。

# タスク 1.4: ステートレス分散の要否判定

> 本節は design.md に合格ラインの算出式が定義される前の判断材料であり、最新の判定は「タスク 1.4 再実施」節を参照。

## CMS・ノードのリソース設定（マニフェスト・クラスタ上の設定値）

`gitops/manifests/prod/cms/deployment.yaml`（replicas: 1）:

| | CPU | Memory |
|---|---|---|
| requests | 250m | 256Mi |
| limits | 500m | 512Mi |

`prod-node-1` は Capacity cpu 4 / Allocatable cpu 3800m。全 Pod の requests 合計は 2195m (57%) で、requests ベースの残余は約 1605m。namespace `prod` の `LimitRange: prod-default-limits` がコンテナ単位の CPU limit 上限を `Max: 2`（2000m）に設定しており、これが CPU limit 引き上げの実質的な天井になる。

## 案の整理

- 律速要因がコンテナ単位の CPU limit である以上、limit を上げないままレプリカだけを増やしても、1 レプリカあたりの処理能力は変わらない。
- CPU limit の引き上げ（案 B）は単一ノードで完結し、ノード追加と無関係に先行できる。ただし可用性は改善しない。
- Payload (Node.js) は単一メインスレッドで JS を実行するため、1 vCPU を超える領域で CPU limit の引き上げがスループットに線形に反映される保証はない。外挿値は参考であり実測が必要。

| | 案A: 現状維持 | 案B: CPU limit 引き上げ（レプリカ1のまま） | 案C: ステートレス分散（レプリカ増） |
|---|---|---|---|
| 単一ノードで完結するか | ― | する。`resources.limits.cpu` の変更のみ（LimitRange 上限まで） | レプリカ増自体は可能だが、複数ノードへの分散が本 spec の意図 |
| 単一障害点の解消 | しない | しない（処理能力の話であり可用性の話ではない） | 複数ノードへ分散すれば Pod 単位は解消し得る |
| 変更に要する操作 | なし | Git コミット + ArgoCD sync（本番マニフェスト変更のためタスク 1 の範囲外） | 同左（`replicas` 変更 + 配置制約） |
| CPU limit 引き上げとの併用 | ― | ― | 併用可能。limit が据え置きなら合計処理能力は「現行 limit × レプリカ数」にとどまるため、先に limit を上げる方が低コスト |

この時点では算出式が spec に存在せず、要件 1.5 を字義通り適用できないため、タスク 5.8 は保留（案 B を先に試し、不足する場合に限り案 C）とした。データ層の冗長化（タスク 4・5.5）は可用性目的であり、この判定に依存しない。

# タスク 1.4 再実施: 合格ラインの算出とステートレス分散の要否判定（2026-09-21）

design.md の「Performance & Scalability」節を前提に再実施した。

## 1. 1 ページビューあたりの CMS API コール数

対象は公開サイトのトップページ（`frontend/src/app/(site)/page.tsx` → `getHomePage()`）。`frontend/src/lib/home-page.ts` の `getHomePage()` は 1 回の SSR で CMS へ逐次 5 回のリクエストを発行する: `globals/festival_meta`・`sponsors`（`limit=0`）・`announcements`（`limit=10`）・`topics`（`limit=0`）・`globals/page_home`。

`curl -sI https://aramakisai.com/` を複数回実行し、全回で `x-nextjs-cache: MISS` を確認した（`HIT` は一度も観測されなかった）。OpenNext/Cloudflare Workers 側の ISR ページキャッシュが実質機能しておらず、毎回フル SSR が走るのが本番の実態である。CMS Pod の CPU 消費による逆算でも、コード上の 5 回と同じ桁であることを確認した。

**実測値: 1 ページビューあたり CMS API コール数 ≈ 5**（キャッシュが効いていない状態の値。HIT 状態の値は未測定で、0 に近づく想定）。合格ラインの算出にはこの値を用いる。

## 2. 来場者数・1 人あたり平均ページビュー数の取得元

入力値は作業時に取得し文書に残さない方針である。

- **来場者数**: オーナーからリポジトリ外の実績値として提供を受けた（2 開催日の合計値）。
- **1 人あたり平均ページビュー数**: オーナーから GA（`aramakisai.com` プロパティ）の「ページとスクリーン」エクスポートと、対象年 11 月中のアクティブユーザー数の提供を受けて算出した。
  - 分子はエクスポートの「表示回数」列の全行合計、分母は 11 月のアクティブユーザー数。行ごとの「アクティブ ユーザー」列は、重複計上になるため合算していない。
  - 分子と分母で集計期間が一致していないため、平均 PV は過大に算出される（安全側）。
  - 来場者数と GA アクティブユーザー数は母集団が異なる。design.md の算出式はこの差を埋める変換を定義していないため、独自の換算率は設けず、式の定義どおり適用した。この母集団差は未解決点として残す。

## 3. 合格ラインの算出

design.md の算出式へ、来場者数・平均 PV・1 ページビューあたり API コール数を代入して合格ラインを算出した。算出式・係数・算出結果は公開リポジトリに記載しない。平均 PV が過大評価側であるため、合格ラインも実態より高め（安全側）に出ている可能性が高い。

## 4. ブレークポイントとの比較

CPU limit 500m 時点のブレークポイントは、合格ラインを**大きく下回る**。平均 PV の見積りの粗さだけでは、この差を覆せない。

要件 1.5「ブレークポイントが合格ラインを上回る場合はステートレス分散を不要と判断する」を字義通り適用すると、条件を満たさないため、ステートレス分散は不要と判断できない。

## 5. 3 案の比較とタスク 5.8 の実施可否

- **案 A（現状維持）**: 合格ラインを大きく下回る。不採用。
- **案 B（CPU limit を LimitRange 上限の 2000m まで引き上げ、レプリカ 1 のまま）**: Node.js の単一スレッド特性により 1 vCPU 超で線形性が崩れる可能性があり、効果は未検証。単独では不足する可能性が高いが、コストが低く先行して実施する価値はある。
- **案 C（ステートレス分散、レプリカ増）**: limit を上げないままレプリカのみ増やしても非効率なため、案 B との併用が前提。

**タスク 5.8（ステートレス分散の実施）の実施可否: 実施する。** 案 B と案 C の併用を前提とし、効果検証には本番マニフェスト変更（Git コミット + ArgoCD sync）と再測定が必要（タスク 5.8/5.9）。

## 6. この判定がデータ層の冗長化に与える影響

**影響しない。** データ層の冗長化（タスク 4、5.5。cms-db・zitadel-db のインスタンス数を 3 に増やす作業）は可用性確保を目的とし、design.md が明記する通り測定結果に依存しない。

## 未確認・未解決の点

- 平均 PV の分母に 11 月のみのアクティブユーザー数を使ったため、平均 PV・合格ラインは過大評価側に出ている。
- 来場者数と GA アクティブユーザー数の母集団差を埋める変換は未定義で、オーナー側の確認が必要。
- 2 日目が雨天だったことによるピーク推定への影響をどう織り込むかは、算出式の係数が吸収する設計か日別の追加調整が別途必要かが design.md から読み取れない。オーナー側の確認が必要。
- API コール数の実測は MISS 状態のみ。HIT 状態は未測定。
- 案 B・案 C の効果は未実測で、タスク 5.8/5.9 での本番実施・再測定による検証が必要。

# タスク 2.6: 対象を限定した差分確認

タスク 2.1・2.2 のコード変更 (`terraform/main.tf` の `locals.nodes`・`terraform/placement.tf`) に対して、対象を限定した `terraform plan` を実行した記録。

## 実行コマンド (1回目: 追加ノードのみ)

```
infisical run --env=prod -- terraform plan \
  -target='hcloud_server.nodes["prod-node-2"]' \
  -target='hcloud_server.nodes["prod-node-3"]' \
  -input=false -no-color
```

完走した。結果は以下の 4 リソースの新規作成のみで、変更・削除は 0 件 (`Plan: 4 to add, 0 to change, 0 to destroy`)。

- `hcloud_placement_group.k3s_nodes`（新規）
- `hcloud_server.nodes["prod-node-2"]`（新規）
- `hcloud_server.nodes["prod-node-3"]`（新規）
- `tailscale_tailnet_key.k3s_nodes`（新規追加ノードの `user_data` が参照するため依存関係として自動的に含まれた。`expiry = 3600` の設計により生じる既知の差分であり、tailscale.tf のコメントの通り他の変更と無関係）

この `-target` は `prod-node-2`・`prod-node-3` のみを指定しており、`hcloud_server.nodes["prod-node-1"]` は対象に含めていなかった。そのため `prod-node-1` に対する `placement_group_id` の追加差分（design.md が要求する「既存ノードの所属変更が in-place の更新として示される」ことの確認）は、この plan の出力範囲には含まれていなかった。

## 実行コマンド (2回目: prod-node-1 を含めた確認)

`prod-node-1` を含む対象無限定の plan は authentik リソースの import 失敗により完走しないが（design.md 397行目・398行目、project CLAUDE.md 記載の既知の制約）、`hcloud_server.nodes["prod-node-1"]` 単体を明示的に `-target` に含めた場合は authentik リソースへ到達しないため完走する。

```
infisical run --env=prod -- terraform plan \
  -target='hcloud_server.nodes["prod-node-1"]' \
  -target='hcloud_server.nodes["prod-node-2"]' \
  -target='hcloud_server.nodes["prod-node-3"]' \
  -input=false -no-color
```

完走した (`Plan: 4 to add, 1 to change, 0 to destroy`)。`prod-node-1` の差分は以下の通り、**in-place 更新のみであり、再作成 (`-/+` もしくは `# forces replacement`) は示されなかった**。

```
# hcloud_server.nodes["prod-node-1"] will be updated in-place
~ resource "hcloud_server" "nodes" {
      id                         = "133188863"
      name                       = "prod-node-1"
    ~ placement_group_id         = 0 -> (known after apply)
      # (21 unchanged attributes hidden)

      # (2 unchanged blocks hidden)
  }
```

変更される属性は `placement_group_id` の 1 件のみ (`0` → `(known after apply)`)。他の 21 属性・2 ブロックは無変更 (`unchanged` と明示)。新規作成対象 (4 件) は 1 回目と同一 (`hcloud_placement_group.k3s_nodes`・`prod-node-2`・`prod-node-3`・`tailscale_tailnet_key.k3s_nodes`)。

以上により、design.md の Validation 要件（「`placement_group_id` は provider の `resourceServerUpdate` が in-place で処理するため、サーバーの再作成を伴わない」）および task 2.2 の完了状態（「既存ノードの差分が所属先の変更のみであり、再作成が示されないこと」）を plan 出力で確認した。

# タスク 4.3: 対象外のワークロードを据え置く判断の記録

`gitops/manifests/prod/` 配下を走査し、CNPG `Cluster`（DB クラスタ）と `StatefulSet` を全数列挙した。稼働状態は各ワークロードの Deployment/StatefulSet/Helm values の `replicas` 指定で確認した（クラスタへの問い合わせは行っていない）。

## DB クラスタ (CNPG Cluster) 全数

| クラスタ名 | マニフェスト | instances (本タスク後) | 所属ワークロードの稼働状態 | 分類 |
|---|---|---|---|---|
| directus-db (cms) | `gitops/manifests/prod/cms/db-cluster.yaml` | 3（タスク 4.1 で変更） | 稼働中（`cms/deployment.yaml` replicas: 1） | 対象 |
| zitadel-db | `gitops/manifests/prod/zitadel/db-cluster.yaml` | 3（タスク 4.2 で変更） | 稼働中（`zitadel/statefulset.yaml` replicas: 1） | 対象 |
| authentik-db | `gitops/manifests/prod/authentik/db-cluster.yaml` | 1（変更なし） | 停止中（`gitops/helm-values/prod/authentik.yaml` に `replicas: 0` が 2 箇所、`redis.yaml`・`ldap-outpost.yaml` も `replicas: 0`） | 対象外 |
| vaultwarden-db | `gitops/manifests/prod/vaultwarden/db-cluster.yaml` | 1（変更なし） | 停止中（`vaultwarden/deployment.yaml` replicas: 0、「凍結中」とコメントあり） | 対象外 |
| room-presence-db | `gitops/manifests/prod/room-presence/db-cluster.yaml` | 1（変更なし） | 停止中（`gitops/apps/prod/room-presence.yaml` の Helm `valuesObject.replicaCount: 0`） | 対象外 |

## StatefulSet (CNPG 以外) 全数

| ワークロード | マニフェスト | replicas | 分類 |
|---|---|---|---|
| mailserver (DMS) | `gitops/manifests/prod/mailserver/statefulset.yaml` | 1 | 対象外 |
| zitadel（アプリ本体） | `gitops/manifests/prod/zitadel/statefulset.yaml` | 1 | 対象外 |

## 対象外リストと根拠

- **authentik-db**: authentik 本体が停止中のため冗長化しても効果がなく、ディスクを消費するのみ
- **vaultwarden-db**: Vaultwarden 本体が停止中のため、authentik-db と同じ理由で対象外
- **room-presence-db**: room-presence 本体が停止中のため、authentik-db と同じ理由で対象外
- **mailserver (StatefulSet)**: ポート 25 の bind と逆引き（RDNS）設定により特定ノードから移動できない。配置制約を変更しない
- **zitadel (StatefulSet、アプリ本体)**: 本タスク（4. データ層冗長化）の対象は稼働中の CNPG データ層クラスタ（cms-db・zitadel-db）に限る（design.md データ層冗長化節）。zitadel-db 自体は対象（タスク 4.2）だが、Zitadel アプリ本体の冗長化は別の意思決定を要するため本タスクの対象外とする
- **nginx-ingress**（`gitops/apps/prod/nginx-ingress.yaml`、Helm 管理でローカルマニフェストなし）: ステートフルワークロードではないが、design.md が「外部公開経路の構成を変更しない」対象として明記しているため参考記録する。来場者トラフィックを担わない autoconfig 専用の経路であり、配置を変更しても効果がない

対象外としたワークロード（authentik・vaultwarden・room-presence 本体とその DB）はいずれも本タスクで再開していない。

---

# タスク 5.9: CMS CPU limit 引き上げ後の再測定（2026-09-21）

案 B（`gitops/manifests/prod/cms/deployment.yaml` の `limits.cpu` を `500m` → `2000m`）が PR #252 でマージ・ArgoCD sync 済みとなったため、タスク 1.2 と同じ手法（SSH 直接経路、`breakpoint.js`）で再測定した。

## 事前条件

- 実機 Pod の `resources` が `limits.cpu: 2`、`limits.memory: 512Mi`、`requests.cpu: 250m`、`requests.memory: 256Mi` であることを確認した（`replicas: 1` は据え置き）。
- CNPG バックアップ・WAL archiving が正常で、測定前の CMS Pod は `Running`・`RESTARTS 0`・アイドル水準だった。
- 経路はタスク 1.2 と同じ SSH ローカルポートフォワード。Cloudflare 経由は対象 API が素通りするため高レートでは使用しない。

## 結果

ランプ時間を変えて 2 回測定した（測定 5・6）。いずれもエラー率が閾値を超えて `abortOnFail` が発動し、2 回とも近い値に収束したため、単一のランプ設定に依存した偶然ではないと判断した。エラーの主因は `connection reset by peer`（SSH トンネルのローカル終端）、次点が k6 側 10 秒の `request timeout`。トンネル自体のクラッシュ（`connection refused`）はなかった。

測定 5 では監視スクリプトの不備（`kubectl top pod` に複数 Pod 名を同時指定できない）で CMS Pod 単体の値が欠落したため、個別呼び出しに修正した測定 6 で補った。

## 律速箇所の判定

- CMS Pod の CPU は `limits.cpu: 2000m` に対し余裕を残しており、旧測定のような cgroup スロットリングの兆候は観測されなかった。ノード全体・`directus-db-1` にも余裕があり、メモリも制限値に対して余裕があった（OOMKilled なし）。
- CPU に余裕を残したまま接続断・タイムアウトが増えたことから、**CPU limit によるスロットリングではなく、CMS（Payload/Node.js）アプリケーション側の処理能力が新たな律速になっていると判断する。** タスク 1.4 で留保していた「Node.js の単一スレッド特性により 1 vCPU 超で線形性が崩れる可能性」に整合する。
- CPU が上限まで使われなかった直接原因（イベントループ、DB コネクションプール、keep-alive、トンネル側のソケット処理等）はプロファイリングを行っておらず未特定。

## 変更前（500m）との比較

| | 変更前（500m） | 変更後（2000m） |
|---|---|---|
| ブレークポイント・実測スループット | 低い | 向上 |
| CMS Pod CPU | limit にほぼ張り付き | limit 未到達 |
| 律速要因 | CPU limit による cgroup スロットリング | CPU limit 未到達。アプリケーション（Node.js）側の処理能力上限と推定 |

## 合格ラインとの比較

実測スループットは合格ラインに**届いていない**。外挿していた期待レンジの下限にも届かなかった。外挿は「CPU limit が天井になる」前提で作っていたが、実際には CPU limit に到達する前にアプリケーション側の別要因で頭打ちになったため、その前提がこの構成では成立しなかったと考えられる。

タスク 1.4 の判定（案 B 単独では合格ラインに届かない可能性が高く、案 C との併用が必要）は、今回の実測により**裏付けられた**。案 B 単独の実測効果は外挿の楽観値よりかなり小さく、案 C の必要性はタスク 1.4 時点より強まった。

## 測定後の復帰確認

オリジン直・Cloudflare 経由とも `200`、全 Pod `Running`、CMS `RESTARTS 0`、Events に OOMKilled 等なし。CMS Pod のメモリはアイドル水準へ復帰。SSH トンネルプロセスは終了済み。

## 未確認・未解決の点

- CPU 使用率が上限まで到達しなかった直接原因は未特定。Node.js プロセス内部のプロファイリング（`--prof`、clinic.js 等）が必要。
- 測定 5・6 は同一 SSH トンネル経路で、トンネル自体のスループット上限が律速として混入している可能性を完全には排除できていない（タスク 5.10 で切り分け）。
- 案 C 実施後の合算スループットは未測定。
- p99 レスポンスタイムは k6 既定サマリに含まれず未取得。

# タスク 5.10: SSH トンネル律速疑義の切り分け（ノード上直接実行、2026-09-21）

タスク 5.9 のエラーが SSH ポートフォワードのローカル終端のシグネチャであり、サーバー側に余裕があったことから、「SSH トンネル自体が天井になっていたのではないか」という疑義が生じた。SSH を経路から外し、`prod-node-1` 上で k6 を直接実行して CMS の ClusterIP (`10.43.111.239:80`) を叩く再測定を行った。

## 実行方法

- k6 バイナリ: GitHub Releases から、ローカルの Docker イメージ `grafana/k6:latest` と同一バージョン（v2.2.0）を `curl` で取得し `/tmp` に展開した（パッケージマネージャは不使用）。
- シナリオ: `breakpoint.js` を `scp` でノードへ転送し、変更なしで使用。
- 実行例（ノード上）:
  ```
  BASE_URL=http://10.43.111.239 TARGET_PATH='/api/globals/festival_meta?depth=1' \
  START_RATE=<開始レート> MAX_RATE=<上限レート> RAMP_DURATION=2m PRE_ALLOCATED_VUS=200 MAX_VUS=1500 \
  ./k6-bin run breakpoint.js
  ```
  タスク 5.9 測定 6 と同一パラメータ。SSH はコマンド投入のみに使用し、負荷トラフィックは経由していない。
- 監視: ローカル PC から `make kubectl` で CMS Pod・`directus-db-1`・ノードの `top` をポーリングし、ノード上では `k6-bin` プロセス自体の CPU も別途サンプリングした。

## 結果

エラー率が閾値を超えて `abortOnFail` が発動した。エラー種別は SSH 経由と質的に異なり、`connection refused`（サーバー側 TCP accept 拒否）と `request timeout` が同数で、**`connection reset by peer` は 0 件**だった（SSH トンネルのシグネチャが消滅）。実測スループットは SSH 経由よりわずかに上昇しただけで、数倍規模の改善はなかった。CMS Pod の CPU は SSH 経由時とほぼ同水準で、`limits.cpu: 2000m` には到達しなかった。

## 律速箇所の再判定

**タスク 5.9 の「アプリ（Node.js/Payload）側の処理能力が律速」という判定は訂正を要しない。** SSH トンネルは律速の主因ではなかった。

1. SSH を外しても実測スループットはわずかしか改善せず、トンネルが天井だったなら生じるはずの大幅な改善が見られなかった。
2. CMS Pod の CPU は経路を変えてもほぼ同水準で、CPU 律速でないことも変わらない。
3. SSH という仲介プロセスのない直結経路で `connection refused` が発生したことは、**CMS（Node.js）プロセス自身が新規接続の accept を追いつかせられていない**ことを直接示すサーバー側の兆候であり、SSH アーティファクトでは説明できない。

## 測定値への影響（k6 のノード上実行による CPU 競合）

k6 を CMS と同一ノード上で実行したため、k6 自身がノード CPU の一部（1 コア未満）を消費し、CMS・DB と奪い合っている。この分だけ測定値は保守側（低め）にぶれている可能性がある。ただし CMS Pod は cgroup で分離されており、ノード全体の CPU が上限に近づいた形跡もないため、判定を覆す規模とは考えにくい。

## 測定後の復帰確認

ノード直（ClusterIP）・Cloudflare 経由とも `200`、全 Pod `Running`、CMS `RESTARTS 0`。ノード `/tmp` に配置した k6 バイナリ・シナリオ・生成物は削除済みで、`k6-bin` プロセスの残存なし。

## 未確認・未解決の点

- `connection refused` が Node.js の listen backlog 由来か、OS の `somaxconn`／ephemeral port 枯渇由来か、アプリケーションが明示的に接続を絞っているのかは、プロセス内部・カーネルパラメータの調査をしておらず未特定。
- k6 とノード CPU の競合を完全に排除した測定（別ノードまたはクラスタ外から ClusterIP へ到達させる等）は未実施。
- CPU 使用率が上限まで到達しなかった直接原因は、タスク 5.9 と同じく未特定。

# タスク 5.8: CMS のステートレス分散 (2026-10-09)

CMS Deployment（`gitops/manifests/prod/cms/`）を 3 ノードへ分散した。PR #346 で Git にコミットし、ArgoCD の sync で反映した。クラスタへの直接操作は行っていない。

## 変更内容

| | 変更前 | 変更後 |
|---|---|---|
| `replicas` | 1 | 3（各ノード 1 Pod） |
| `strategy` | `Recreate` | `RollingUpdate`（`maxUnavailable: 0`、`maxSurge: 1`） |
| `topologySpreadConstraints` | なし | `topologyKey: kubernetes.io/hostname`、`maxSkew: 1`、`whenUnsatisfiable: ScheduleAnyway`、`matchLabelKeys: [pod-template-hash]` |
| PodDisruptionBudget | なし | `pdb.yaml`（`maxUnavailable: 1`、`app: cms` を選択） |
| resources | 変更なし | 変更なし |

- `ScheduleAnyway` のため、ノード障害時は残りのノードへ寄って起動できる。
- `matchLabelKeys` により、rollout 中も新リビジョンの Pod がノードへ均等に置かれる。
- HPA は設けていない。
- ステートレス性の根拠は、アップロードが S3、認証が JWT と Cookie、DB スキーマ適用が PreSync Job（`cms-migrate`）で `push: false`、Payload のジョブキューが DB 上の `processing` フラグで排他されることによる。

# タスク 5.9 再測定: 3 ノード・CMS 3 replicas 分散後 (2026-10-09)

タスク 5.8 の反映後、タスク 5.10 と同じ手法で `/api/globals/festival_meta?depth=1` のブレークポイントを再測定した。

## 事前条件

- CMS 3 Pod が `Running`、各ノード 1 Pod、`RESTARTS 0`。CMS の `limits` は `cpu: 2`、`memory: 512Mi`。
- `directus-db` は 3/3 Ready、`ContinuousArchiving=True`、`LastBackupSucceeded=True`、直近の daily backup は completed。`directus-db` の `limits` は `cpu: 500m`、`memory: 512Mi`。

## 実行方法

- `prod-node-1` 上で k6（v2.2.0、GitHub Releases から `/tmp` へ取得）を直接実行し、CMS の ClusterIP を叩いた。
- シナリオは `breakpoint.js` を無変更で使用し、`--summary-trend-stats` で p99 も取得した。
- ランプ時間と開始レートを変えて 2 回測定した。

## 結果

2 回とも `abortOnFail` が発動し、2 回の値は近くに収束した。ランプ条件に依存せず、システム側の上限によるものと判断する。

エラーは k6 側 10 秒の `request timeout` のみで、`connection refused` は 0 件だった。タスク 5.10 で見られた accept 側の詰まりは消えている。p95・p99 はタイムアウト値に張り付いた。

## 律速箇所の判定

- 3 Pod 間の負荷はほぼ均等だった。各 CMS Pod の CPU は `limits.cpu: 2000m` に届かないまま頭打ちになった（Node.js メインスレッドの飽和の可能性）。
- `directus-db` の primary Pod は CPU が `limits.cpu: 500m` に張り付いた。replica 2 台はほぼアイドルで、読み取りも primary に集中している。
- ノードの CPU・メモリには余裕があり、OOMKilled はなかった。k6 プロセス自体の CPU 消費は小さかった。

律速はアプリ単体から、共有の DB primary（CPU limit）と CMS 各 Pod の単一スレッド飽和へ移ったと判断する。

## 変更前（タスク 5.10、replicas 1）との比較

| | 変更前（replicas 1） | 変更後（replicas 3） |
|---|---|---|
| ブレークポイント | 低い | 上回る（改善幅は replicas 数に比例せず小幅） |
| エラー種別 | `connection refused` と `request timeout` | `request timeout` のみ |
| 律速要因 | CMS（Node.js）の新規接続 accept | DB primary の CPU limit と CMS 各 Pod の単一スレッド飽和 |

## 合格ラインとの比較

実測スループットは合格ラインに**届いていない**。

## 測定後の復帰確認

ClusterIP・Cloudflare 経由（`https://cms.aramakisai.com/...`）とも `200`、全 Pod `Running`、CMS `RESTARTS 0`、OOMKilled なし、CNPG は healthy。ノード上の k6 一式は削除済みで、プロセスの残存なし。

## 未確認・未解決の点

- DB primary の CPU limit と CMS の単一スレッド飽和のどちらが主因かは未特定。切り分けには変更を伴う測定が必要で、未実施。
- `depth=1` 1 リクエストあたりの DB クエリ数とキャッシュの有無は未確認。
- k6 と CMS Pod が同一ノードに同居することによる影響は未確認。
- 測定中の実トラフィックの混在は未確認。
