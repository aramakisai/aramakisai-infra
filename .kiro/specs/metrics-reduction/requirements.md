# Requirements Document

## Introduction
Grafana Cloud の無料枠（10,000 active series）制限を超過する警告が発生したため、Kubernetes クラスターの監視を担当する Grafana Alloy DaemonSet で収集・送信するメトリクス数を削減する。

## Boundary Context (Optional)
- **In scope**:
  - `gitops/manifests/shared/monitoring/alloy.yaml` (Alloy DaemonSet) の設定変更による送信メトリクスの削減
  - cAdvisor, Kubelet, node_unix (node exporter) から送信されるメトリクスのフィルタリング（リレーベルによる不要メトリクスのドロップ）
  - ArgoCD メトリクスの削減または無効化
- **Out of scope**:
  - `alloy-cluster` で収集している `kube-state-metrics` の削減（現在約 170 シリーズと非常に低いため）
  - Grafana Cloud 側での有償プランへの移行
  - ログ転送（Loki）の削減（現時点で制限を大きく下回るため対象外）
- **Adjacent expectations**:
  - 必要最低限のノード監視およびコンテナ監視（CPU、メモリ、ディスク使用量）のメトリクスは維持され、既存のアラートやダッシュボードが破損しないこと

## Requirements

### Requirement 1: メトリクスフィルタリング機能の実装
**Objective:** 開発者として、Grafana Alloy が Grafana Cloud に送信するメトリクス量を削減し、無料枠に収めることができるようにしたい。

#### Acceptance Criteria
1. The Alloy DaemonSet shall `cAdvisor` ジョブから送信されるメトリクスをフィルタリングし、コンテナの CPU・メモリ・ディスク・ネットワークに関する主要メトリクスのみを転送する。
2. The Alloy DaemonSet shall `Kubelet` ジョブから送信されるメトリクスをフィルタリングし、ボリューム状態や実行中 Pod 数などの主要メトリクスのみを転送する。
3. The Alloy DaemonSet shall `node_unix` ジョブから送信されるメトリクスをフィルタリングし、ホストの CPU・メモリ・ディスク・ロードアベレージに関する主要メトリクスのみを転送する。
4. The Alloy DaemonSet shall `argocd` ジョブから送信されるメトリクスをドロップまたは大幅に制限する。

### Requirement 2: 監視維持とメトリクス数抑制の検証
**Objective:** 開発者として、送信メトリクス数が削減され、かつ必要な監視データが不足していないことを確認したい。

#### Acceptance Criteria
1. When 設定が適用された際、the Alloy DaemonSet の active series 送信数が合計で 10,000 シリーズ未満になること。
2. The monitoring system shall 主要なメトリクス（例: `container_cpu_usage_seconds_total`, `container_memory_working_set_bytes`, `node_memory_Active_bytes`）が引き続き Grafana Cloud に届いていることを確認できること。
