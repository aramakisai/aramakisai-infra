# Requirements Document

## Introduction

2026年11月14日〜15日の荒牧祭開催期間に向けて K3s クラスタを一時的に 3 ノード構成へスケールアウトし、イベント終了後にシングルノード構成へ戻す。

現構成は `single-node-migration` spec によりコスト削減を目的として 3×CX23 の HA クラスタから 1×CX33 (2vCPU/8GB) のシングルノードへ移行済みである。単一ノードであるため etcd クォーラムの概念が存在せず、ノード障害時は `dr-trigger.yml` による検知とノード再作成 (コールドスタンバイ) に依存する。

イベント当日はアクセスが集中する一方、現構成が想定負荷に耐えるかを判断する実測値が存在しない。また復旧に要する時間の間サービスが停止する。

開催期間中に K3s へ到達する来場者向けトラフィックは CMS の API に限られる。公開 Web サイトの apex は Cloudflare Workers で配信され、Cloudflare Tunnel の ingress ルールは各サービスの ClusterIP へ直接転送されるため、nginx-ingress は autoconfig 専用の経路として動作する。

本 spec は二つの軸を持つ。第一に、稼働中のデータ層を複数ノードへ冗長化し、prod-node-1 の障害時にもサービスを継続させる。第二に、負荷テストの結果が必要性を示した場合に限り、ステートレスなワークロードを分散する。後者は測定を先行させ、実測の裏付けがないまま構成を作り込まない。

## Boundary Context

- **In scope**: 負荷テストによるボトルネック特定、server ノードの一時追加による etcd クォーラム確保、稼働中の CNPG クラスタの一時的な冗長化、縮退手順の事前検証と実行、イベント期間中の DR 自動復旧機構との整合
- **Out of scope**: 共有ストレージ (Hetzner CSI 等) への移行、恒久的な HA 化、mailserver の冗長化 (ポート 25 の bind と RDNS により prod-node-1 に固定される)、停止中ワークロード (authentik・vaultwarden・room-presence) の再開および冗長化
- **Adjacent expectations**:
  - `single-node-migration` (completed) が確立したシングルノード構成を、イベント期間に限り一時的に逸脱する。終了後は同 spec の構成へ復帰する
  - `observability-v2` が提供する `dr-trigger.yml` / `dr-recovery.yml` は prod-node-1 単独構成を前提としており、3 ノード構成中の動作は未定義である
  - `ha-improvement` (cancelled) が要件 1 で定めた CNPG のレプリカ増加と別ノード配置は、本 spec が開催期間に限って再導入する。同 spec の PodDisruptionBudget 追加は対象外とする

## Requirements

### Requirement 1: ベースライン性能測定

**Objective:** As an インフラ担当者, I want 現行シングルノード構成の性能限界を実測したい, so that ノード追加とワークロード再配置の要否を推測ではなく数値で判断できる

#### Acceptance Criteria

1. The 負荷テスト shall CMS の API を主たる対象とする。公開 Web サイトの apex は Cloudflare Workers で配信され K3s への負荷を生じないため、測定対象に含めない
2. The 負荷テスト shall Cloudflare を経由しない経路とエッジキャッシュを経由する経路の双方を測定し、それぞれの結果を区別して記録する
3. When 負荷テストが段階的に負荷を増加させたとき、the インフラ担当者 shall HTTP 5xx またはタイムアウトが発生し始める RPS をブレークポイントとして記録する
4. While 負荷テストが実行されている間、the インフラ担当者 shall ノードのメモリ使用量・CPU 使用率・Pod の OOMKilled 発生有無・CMS と cms-db のレスポンス劣化を記録する
5. If ブレークポイントが合格ラインを上回る場合、then the インフラ担当者 shall ステートレスワークロードの分散を不要と判断する。この判断はデータ層の冗長化には影響しない
6. The 負荷テスト shall 本番クラスタの利用が少ない時間帯に実施し、実施前に CNPG バックアップの正常性を確認する
7. The 負荷テスト shall 新規の監視基盤を導入せず、既存の Netdata と `kubectl top` で取得可能な指標のみを用いる
8. The インフラ担当者 shall 設計が定義する算出式と係数により合格ラインを求める。係数は設計に定義され、入力値のみを作業時に取得する
9. The インフラ担当者 shall 1 ページビューあたりの CMS API コール数を実測する。Cloudflare Workers のキャッシュ設定に依存して大きく変動するため、推定値を用いない

### Requirement 2: ノード追加のためのインフラコード対応

**Objective:** As an インフラ担当者, I want ノードごとに異なるインスタンスタイプを指定できるコードにしたい, so that 既存ノードを変更せずに高スペックノードを追加できる

#### Acceptance Criteria

1. The Terraform 構成 shall ノードごとに `server_type` を指定できる構造を提供する
2. The Terraform 構成 shall spread 型の `hcloud_placement_group` を定義し、全ノードの所属先とする
3. When インフラ担当者がコード変更後に `terraform plan` を実行したとき、the Terraform 構成 shall prod-node-1 の `placement_group_id` 以外にいかなる変更も差分として出力しない
4. If `terraform plan` が prod-node-1 の再作成または置換を示した場合、then the インフラ担当者 shall apply を中止しコードを修正する
5. The Ansible inventory shall 追加ノードを `k3s_server_worker` グループとして定義し、既存の `k3s-bootstrap.yml` の Play 2 がこれを対象とする
6. When インフラ担当者が `--limit` を指定して playbook を実行したとき、the Ansible playbook shall 対象外のノードに対していかなる変更も行わない
7. The インフラ担当者 shall 要件 2 の段階ではノードを作成せず、コードのマージのみを行う
8. If `terraform plan` が authentik リソースの import 失敗により全体として完了しない場合、then the インフラ担当者 shall `-target` によりノード関連リソースへ対象を限定して差分を確認する
9. The インフラ担当者 shall apply の対象をノード関連リソースへ限定し、意図しないリソースの新規作成を伴う apply を行わない

### Requirement 3: ノード増減手順の事前検証

**Objective:** As an インフラ担当者, I want ノードを増やす手順と減らす手順の双方を本番以外の場所で確立したい, so that 本番で初めて実行する操作をなくし etcd クォーラムを破壊しない

#### Acceptance Criteria

1. The インフラ担当者 shall 本番クラスタとは独立した使い捨ての検証クラスタで手順を検証する
2. The 検証クラスタ shall 本番と同一の Ansible ロール (`k3s-server`) を用いて構築する
3. When 検証クラスタが 1 ノードから 3 ノードへ拡張されたとき、the K3s クラスタ shall 各段階で etcd メンバーを追加し全ノードが Ready 状態になる
4. The インフラ担当者 shall 拡張の過程で etcd メンバーが 2 となる区間を通ることを認識し、その区間の耐障害性が単一ノードより低いことを記録する
5. The 検証 shall `tls-san` の変更を既存ノードへ適用し、証明書の再生成または K3s の再起動が発生するかを記録する
6. The 検証 shall CNPG クラスタのインスタンス数を 1 から 3 へ増加させ、各インスタンスが別ノードへ配置されることを確認する
7. The 検証 shall CNPG クラスタのインスタンス数を 3 から 1 へ減少させる手順を確認する。Primary が縮退後に残すノード以外にある場合の switchover を含む
8. When 検証クラスタが 3 ノード構成から 1 ノード構成へ縮退したとき、the K3s クラスタ shall 縮退完了後に API server が応答し既存のワークロードがスケジュール可能な状態を維持する
9. The 縮退手順 shall ノードを 1 台ずつ削除し、各段階で etcd メンバー数とクォーラムの状態を確認する手順を含む
10. The 手順 shall 作業開始前に etcd スナップショットを取得する工程を含む
11. The 縮退手順 shall Hetzner サーバー削除後に Tailscale デバイスを削除する手順を含む
12. If 検証中にクォーラム喪失またはクラスタ停止が発生した場合、then the インフラ担当者 shall 原因を特定し手順を修正したうえで再検証する
13. The インフラ担当者 shall 検証完了後に手順を文書化し、どの工程をどの検証環境で確認したかを明記する
14. The インフラ担当者 shall 検証完了後に検証クラスタのリソースを削除し、Tailscale デバイスも併せて削除する

### Requirement 4: 本番スケールアウトの実行

**Objective:** As an インフラ担当者, I want イベント開催前に 3 ノード構成へ移行したい, so that 開催期間中に単一ノード障害が発生してもクラスタが停止しない

#### Acceptance Criteria

1. The インフラ担当者 shall 開催日より前にスケールアウトを完了し、動作確認の猶予期間を確保する
2. The インフラ担当者 shall 追加ノードを作成する時点で placement group を指定する。新規作成時の指定はサーバー停止を伴わないため
3. When 追加ノードが K3s クラスタへ参加したとき、the K3s クラスタ shall etcd メンバー数 3 かつ全ノードが Ready 状態になる
4. When データ層の冗長化が完了したとき、the インフラ担当者 shall prod-node-1 を停止して placement group へ追加し、再起動する。既存サーバーの追加はオフラインであることが前提となるため
5. While prod-node-1 が placement group 追加のために停止している間、the K3s クラスタ shall etcd メンバー 2 でクォーラムを維持し、CNPG は残存ノードの Standby を Primary へ昇格させる
6. The インフラ担当者 shall prod-node-1 の停止中に mailserver が停止することを認識し、停止時間を最小に保つ
7. If prod-node-1 の placement group 追加に失敗した場合、then the インフラ担当者 shall prod-node-1 を起動して従前の状態へ戻し、追加ノードのみが placement group に所属する状態を記録する
8. When スケールアウトが完了したとき、the インフラ担当者 shall 既存の全サービス (ArgoCD・CMS・認証基盤・メールサーバー) が正常応答することを確認する
9. The K3s クラスタ shall スケールアウト後も mailserver を prod-node-1 上で動作させ続ける
10. If スケールアウト後に既存サービスへ影響が発生した場合、then the インフラ担当者 shall 要件 5 の縮退手順により 1 ノード構成へ復帰する
11. The インフラ担当者 shall スケールアウト実行前に etcd スナップショットと CNPG バックアップの正常性を確認する

### Requirement 5: 縮退の実行とリソース解放

**Objective:** As an インフラ担当者, I want イベント終了後に確実に 1 ノード構成へ戻したい, so that 恒常的なコスト増が発生しない

#### Acceptance Criteria

1. The インフラ担当者 shall 要件 3 で文書化した手順に従って縮退を実行する
2. When 縮退を開始するとき、the インフラ担当者 shall 各 CNPG クラスタの Primary が prod-node-1 上にあることを確認し、他ノードにある場合は switchover により prod-node-1 へ移してから進める
3. When Primary の所在を確認したとき、the インフラ担当者 shall CNPG クラスタのインスタンス数を 1 へ戻し、Standby の削除完了を待ってからノード削除へ進む
4. When 縮退が完了したとき、the K3s クラスタ shall prod-node-1 単独で etcd とワークロードを担う状態へ復帰する
5. When 縮退が完了したとき、the インフラ担当者 shall Hetzner 上の追加サーバーが削除され課金が停止していることを確認する
6. When Hetzner サーバーが削除されたとき、the インフラ担当者 shall 対応する Tailscale デバイスを削除する
7. The Terraform 構成 shall 縮退後に `single-node-migration` が定義するシングルノード構成と等価な状態を宣言する
8. If 縮退中にクラスタが停止した場合、then the インフラ担当者 shall 取得済みの etcd スナップショットから復旧する

### Requirement 6: DR 自動復旧機構との整合

**Objective:** As an インフラ担当者, I want 3 ノード構成中の DR 自動復旧の挙動を定義したい, so that 誤検知によるノード再作成が本番構成を破壊しない

#### Acceptance Criteria

1. The インフラ担当者 shall スケールアウト前に `dr-trigger.yml` および `dr-recovery.yml` が 3 ノード構成でどう動作するかを評価する
2. If DR 自動復旧が 3 ノード構成で意図しないノード再作成を行うと判明した場合、then the インフラ担当者 shall スケールアウト期間中の自動復旧を無効化するか、3 ノード構成に対応させる
3. While 3 ノード構成が稼働している間、the DR 機構 shall 単一ノード障害でノード再作成を実行しない
4. When 縮退が完了したとき、the インフラ担当者 shall DR 自動復旧を通常の動作状態へ戻す
5. The DR 無効化 shall Git 管理下のファイル変更として行い、無効化の事実と復帰予定が Git 履歴から追跡できる状態にする。GitHub の UI 操作のみで完結し Git に痕跡が残らない手段を用いない

### Requirement 7: ワークロードの冗長化と分散

**Objective:** As an インフラ担当者, I want 稼働中のデータ層を複数ノードへ冗長化し、必要に応じてステートレスなワークロードを分散したい, so that prod-node-1 の障害時にもサービスが継続し、アクセス集中にも耐えられる

本要件は二つの部分からなる。データ層の冗長化は可用性を目的とし、測定結果に依存せず実施する。ステートレスワークロードの分散は処理能力の確保を目的とし、要件 1 の測定結果が必要性を示した場合に限り実施する。

#### Acceptance Criteria

**データ層の冗長化**

1. The インフラ担当者 shall 稼働中の DB クラスタである cms-db および zitadel-db のインスタンス数を 3 へ増加させる
2. The インフラ担当者 shall 停止中のワークロードに属する DB クラスタ (authentik-db・vaultwarden-db・room-presence-db) をインスタンス数 1 のまま据え置く。稼働していないため冗長化の効果がなく、ディスク使用量が増えるのみであるため
3. When CNPG クラスタのインスタンス数を増加させたとき、the CNPG クラスタ shall `podAntiAffinity` または `topologySpreadConstraints` により各インスタンスを別ノードへ配置する
4. When prod-node-1 が停止したとき、the CNPG クラスタ shall 残存ノード上の Standby を Primary へ昇格させ、データベース接続を継続する
5. The インフラ担当者 shall 各ノードのディスクが増加分を収容できることを事前に確認する

**ステートレスワークロードの分散**

6. Where 要件 1 のブレークポイントが想定ピーク負荷を下回る場合、the インフラ担当者 shall CMS のレプリカ数を増加させる
7. When レプリカ数を増加させたとき、the ワークロード shall `topologySpreadConstraints` または `podAntiAffinity` により複数ノードへ分散配置される
8. When 配置変更後に負荷テストを再実施したとき、the K3s クラスタ shall 変更前より高いブレークポイントを示す

**共通**

9. The インフラ担当者 shall mailserver を prod-node-1 上に据え置く。ポート 25 の bind と RDNS の制約により移動できないため
10. The インフラ担当者 shall nginx-ingress の配置を変更しない。autoconfig 専用の経路であり来場者トラフィックを担わないため、移動しても負荷分散の効果を持たない
11. The インフラ担当者 shall 停止中のワークロードを再開しない
12. The インフラ担当者 shall 縮退の前に、インスタンス数・レプリカ数・配置制約のすべてを変更前の値へ戻す
13. The GitOps マニフェスト shall すべての変更を Git コミットとして行い、クラスタへの直接操作を行わない

### Requirement 8: ドキュメント同期

**Objective:** As an 運営チームの後任者, I want 構成変更が steering とリポジトリのドキュメントへ反映されていることを期待する, so that 引き継ぎ後も現行構成を正しく把握できる

#### Acceptance Criteria

1. When スケールアウトまたは縮退が完了したとき、the インフラ担当者 shall `.kiro/steering/tech.md` のノード構成に関する記述を実態と一致させる
2. The インフラ担当者 shall `.kiro/steering/product.md` が参照する `ha-improvement` spec の状態を実態 (cancelled) と一致させる
3. The インフラ担当者 shall `.kiro/steering/structure.md` が言及する存在しない Ansible ロールの記述を実態と一致させる
4. The インフラ担当者 shall 確立した縮退手順を `docs/` 配下の運用ドキュメントへ反映する
5. The ドキュメント shall 来場者数・アクセス数などの実測値を含めず、手順と構成のみを記載する
