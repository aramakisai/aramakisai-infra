# ギャップ検証レポート — dr-automation

本レポートは、荒牧祭実行委員会インフラの DR（障害復旧）完全自動化および侵入検知・自動再構築機能（`dr-automation`）の仕様・設計と、現在のコードベースとの間のギャップ（不足している実装や不整合）を分析・検証した結果をまとめたものです。

---

## 1. 既存 DR 復旧スクリプトのバグ・古い参照のギャップ

現在存在する復旧スクリプト [.github/scripts/recovery.sh](../../../.github/scripts/recovery.sh) は、旧メールサーバー（Stalwart）時代の記述のままになっており、現行の構成（Docker Mailserver: DMS）に追従していません。この状態では DR ワークフロー実行時に必ず失敗します。

### 1.1. 旧 Stalwart 参照の残存（前提修正バグ）
* **現状のコード**: [.github/scripts/recovery.sh#L226-267](../../../.github/scripts/recovery.sh#L226-L267) で、`stalwart` StatefulSet、`stalwart-data` PVC、`stalwart-restic-secret` Secret、`stalwart-restore` ReplicationDestination を参照してリストアを試みています。
* **ギャップ**: これらは現在の GitOps 構成では存在せず、すべて `mailserver`（StatefulSet）、`mailserver-data`（PVC）、`mailserver-restic-secret`（Secret）、`mailserver-restore`（ReplicationDestination）に置き換える必要があります。また、バックアップ先も B2 から Hetzner Object Storage に移行しているため、コメント部分（B2）の修正も必要です。

---

## 2. 要件自動化ステップのギャップ（未実装機能）

[requirements.md](requirements.md) および [design.md](design.md) に定義されている `recovery.sh` 内の自己修復・自動化ステップが、現在のコードベースには一切実装されていません。

### 2.1. REQ-03-3: CNPG Job 残存クリーンアップ
* **要件**: 復旧の初期段階（Ansible 実行前）に、古い `full-recovery` ジョブが残っていると PVC の初期化でスタックするため、`cnpg.io/cluster` ラベルの付いた古い Job を削除する。
* **ギャップ**: [.github/scripts/recovery.sh](../../../.github/scripts/recovery.sh) の冒頭部分にこのクリーンアップ処理がありません。`kubectl delete jobs` を用いたベストエフォートな削除ステップ（対象クラスター不在時にもエラーでスクリプトが止まらない設計）を追加する必要があります。

### 2.2. REQ-03-1: `infisical-auth` / Deploy Key の空チェックと自己修復
* **要件**: ArgoCD 同期待機（Step 5）の完了後、`infisical-auth` の `clientId` および `aramakisai-infra-repo` の `sshPrivateKey` が空でないかをチェックする。もし空であった場合は Infisical から再取得して適用し、ExternalSecret の強制再同期を行う。
* **ギャップ**: [.github/scripts/recovery.sh](../../../.github/scripts/recovery.sh) 内にこのチェック・修復処理がありません。チェック処理と、空だった場合に `kubectl apply` を行い、ESO の注記 (`force-sync`) を行うロジックを追加する必要があります。

### 2.3. REQ-03-2: Docker Mailserver (DMS) TLS 証明書の自己修復と GitOps 欠落
* **要件**: mailserver Pod が `ContainerCreating` のまま 2分以上停止しており、かつ `mail-tls` Secret が存在しない場合、必要なマニフェストファイルを自動 apply して証明書を発行させる。
* **ギャップ**:
    1. [.github/scripts/recovery.sh](../../../.github/scripts/recovery.sh) 内に、この状態検知と `kubectl apply` を行う自動修復ロジックが存在しません。
    2. 適用対象となるべき証明書マニフェスト `gitops/manifests/prod/mailserver/certificate.yaml`（`letsencrypt-prod` ClusterIssuer を利用する `Certificate` リソース）自体がリポジトリ内に存在せず、GitOps 管理されていません。

### 2.4. REQ-03-4: Directus DB リストア確認
* **要件**: `recovery.sh` の完了時に Directus の DB レプリカが正常に復元されていることを確認するログを出力する。
* **ギャップ**: [.github/scripts/recovery.sh](../../../.github/scripts/recovery.sh) の末尾に、`directus-db` に対する復元確認処理（`status.firstRecoverabilityPoint` などの確認）が存在しません。

---

## 3. 侵入検知・自動隔離・再構築（REQ-04）のギャップ

ランタイム侵入検知（Falco）から隔離、承認を経て再構築に至るフローを実装するための構成要素が大幅に不足しています。

### 3.1. Falco / falcosidekick の Loki 転送設定の欠落
* **設計**: Falco のアラートを Loki に転送し、Grafana Cloud Loki Alerting を介して GitHub Actions をトリガーする。
* **現状**: [gitops/helm-values/prod/falco.yaml](../../../gitops/helm-values/prod/falco.yaml) は既に存在しますが、設定内容は Discord 転送のみになっており、Loki 送信（`falcosidekick.config.loki.*` および pod/namespace 情報をラベル化する `extralabels`）が有効化されていません。
* **ギャップ**: Loki へのログ送信設定を追加する必要があります。また、Loki 送信用トークンを格納する Secret を Infisical から取得するための ExternalSecret（`gitops/manifests/prod/falco/external-secret.yaml` など）が作成されていません。

### 3.2. `intrusion-response` ワークフローの不在
* **要件**: 侵入検知をトリガーに、フォレンジック取得・NetworkPolicy による通信遮断・人間による承認・自動再構築を行う。
* **ギャップ**: 新規作成すべきワークフローファイル `.github/workflows/intrusion-response.yml` が存在しません。
    * 起動トリガーである `repository_dispatch` (types: `intrusion-response`) の定義。
    * Loki API から障害対象 Pod をクエリして特定するロジック。
    * 対象 Pod のログやイベントのフォレンジック保存 (`kubectl logs`, `kubectl get events`)。
    * egress/ingress を遮断する `NetworkPolicy` の適用。
    * `intrusion-response` Environment への承認要求。
    * 承認を30分間監視し、タイムアウト時にワークフローを自動キャンセルするウォッチドッグジョブ。
    * 承認後に `recovery.sh` を呼び出して再構築を行う処理。
    これらがすべて未実装です。

---

## 4. 運用ドキュメントおよび外部設定のギャップ

### 4.1. 運用ランブック [docs/dr-runbook.md](../../../docs/dr-runbook.md) の更新欠落
* **現状**: 旧復旧トリガーワークフロー (`dr-trigger.yml`) に基づく説明や、手動での `stalwart` 証明書適用手順が残っています。
* **ギャップ**: 自動修復されたステップの反映、Stalwart から mailserver への用語修正、新規導入される `intrusion-response.yml` の運用手順、GitHub Actions Environment や Secrets、Tailscale ACL のセットアップ手順の追記が必要です。

### 4.2. 外部インフラ構成手順のドキュメント化
* **要件**: Grafana Cloud の Synthetic Monitoring 条件（2シグナル AND条件）、Loki Alert Rules、GitHub dispatches への Webhook Contact Point などの SaaS 側設定手順。
* **ギャップ**: 手動で一度だけ設定する必要がある前提条件についての手順が整理されていません（コード化対象外のためドキュメント化が必要）。

---

## まとめ

現在、`dr-automation` 仕様が目指す「完全自動化された障害復旧および自己修復」を実現するためには、以下の実装・修正が欠落しています：

1. **[`recovery.sh`](../../../.github/scripts/recovery.sh) の Stalwart→mailserver 参照修正と4つの自己修復ステップの追加。**
2. **[`certificate.yaml`](../../../gitops/manifests/prod/mailserver/certificate.yaml) の新規作成による mail-tls 証明書の GitOps 管理化。**
3. **[`falco.yaml`](../../../gitops/helm-values/prod/falco.yaml) の Loki 転送設定有効化および接続 Secret 取得マニフェストの作成。**
4. **[`.github/workflows/intrusion-response.yml`](../../../.github/workflows/intrusion-response.yml)（フォレンジック、遮断、承認、ウォッチドッグ、再構築）の新規実装。**
5. **[`dr-runbook.md`](../../../docs/dr-runbook.md) の更新。**
