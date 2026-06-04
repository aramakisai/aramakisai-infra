# Design Document - Metrics Volume Reduction

## Overview
Grafana Cloud の無料枠（10,000 active series）に収めるため、Grafana Alloy DaemonSet が送信するメトリクスをフィルタリング（relabeling）し、必要最小限のリソース監視用メトリクスのみをホワイトリスト方式で保持する。

### Goals
- 各ノードで動作する `alloy` DaemonSet の active series 送信数を 1,000 〜 2,000 シリーズ程度に抑制する（3ノード全体で 6,000 シリーズ以下に抑え、無料枠 10,000 に確実に収める）。
- コンテナ（cAdvisor）、ノード（node_unix）、および Kubelet の基本的なリソース監視に必要なメトリクス（CPU、メモリ、ディスク、ネットワーク等）の送信を維持する。

### Non-Goals
- ログ（Loki）送信のフィルタリング設定の追加（現時点で容量に問題がないため）。
- `alloy-cluster` 側の `kube-state-metrics` の削減（現在 170 シリーズ程度と極めて小さいため対象外）。

---

## Boundary Commitments

### This Spec Owns
- `gitops/manifests/shared/monitoring/alloy.yaml` 内の ConfigMap の修正。
- `prometheus.relabel` コンポーネントの新規追加によるメトリクスホワイトリストフィルタの実装。
- 各スクレイプジョブ (`alloy_self`, `node_unix`, `kubelet`, `cadvisor`, `argocd`) の `forward_to` 先の変更。

### Out of Boundary
- `alloy-cluster` 側の設定変更。
- Grafana Cloud 側のアラートルールの削除・変更。

---

## File Structure Plan

### Modified Files
- [alloy.yaml](gitops/manifests/shared/monitoring/alloy.yaml) — `prometheus.relabel` の追加および各スクレイプジョブの `forward_to` の書き換え。

---

## Architecture

### 既存のメトリクスフロー
```
[cadvisor]    \
[kubelet]     -->  prometheus.remote_write.default  --> (Grafana Cloud Prometheus)
[node_unix]   /
```
不要なメトリクスを含むすべてがそのまま送信されていました。

### 変更後のメトリクスフロー
```
[cadvisor]    \
[kubelet]     -->  prometheus.relabel.keep_essential_metrics
[node_unix]   /    └── (ホワイトリストで keep されたメトリクスのみ転送)
                   └──  prometheus.remote_write.default  --> (Grafana Cloud Prometheus)
```

### Relabel 設定詳細
`alloy.yaml` に以下のコンポーネントを追加します。

```alloy
    prometheus.relabel "keep_essential_metrics" {
      forward_to = [prometheus.remote_write.default.receiver]

      // 必要な主要メトリクスのみをホワイトリスト形式で保持
      rule {
        source_labels = ["__name__"]
        regex         = "(container_cpu_usage_seconds_total|container_memory_working_set_bytes|container_fs_usage_bytes|container_fs_limit_bytes|container_network_receive_bytes_total|container_network_transmit_bytes_total|kubelet_running_pods|kubelet_volume_stats_used_bytes|kubelet_volume_stats_capacity_bytes|node_cpu_seconds_total|node_memory_Active_bytes|node_memory_MemTotal_bytes|node_memory_MemAvailable_bytes|node_filesystem_size_bytes|node_filesystem_free_bytes|node_load1|node_load5|node_load15|node_disk_io_time_seconds_total|node_disk_read_bytes_total|node_disk_written_bytes_total|alloy_build_info)"
        action        = "keep"
      }
    }
```

各スクレイプジョブの `forward_to` を `[prometheus.relabel.keep_essential_metrics.receiver]` に書き換えます。これにより、ホワイトリストに合致しない `argocd` 等のその他のメトリクスは自動的にドロップされます。

---

## Testing Strategy

### 1. シンタックス検証
- 修正後の `alloy.yaml` が Kubernetes のマニフェストとして正しいか、`pre-commit`（yamllintなど）を実行して検証します。

### 2. デプロイ後の動作検証
- 修正を反映後、ArgoCD の同期を確認します。
- `alloy` DaemonSet の Pod が正常稼働していることを確認します。

### 3. メトリクス削減効果の測定
- `alloy` DaemonSet Pod へのポートフォワードを実行し、`metrics` エンドポイントから `prometheus_remote_write_active_series` の値を取得します。
- 送信されている active series が 10,000 未満（目標: 合計 6,000 以下）に削減されたことを確認します。
