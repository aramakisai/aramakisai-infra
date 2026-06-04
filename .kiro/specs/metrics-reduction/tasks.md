# Implementation Plan

## Tasks

- [x] 1. Grafana Alloy 設定ファイルの修正
  - `gitops/manifests/shared/monitoring/alloy.yaml` に `prometheus.relabel.keep_essential_metrics` を追加する。
  - `alloy_self`, `node_unix`, `kubelet`, `cadvisor`, `argocd` の各スクレイプジョブの `forward_to` 先を `[prometheus.relabel.keep_essential_metrics.receiver]` に変更する。
  - _Requirements: 1_
  - _Boundary: alloy.yaml_

- [x] 2. 設定ファイルの構文検証
  - 修正した `alloy.yaml` が yaml として正しく、かつ構文エラーがないか `pre-commit` (yamllint) を実行して確認する。
  - _Requirements: 2_

- [x] 3. クラスターへの適用と動作検証
  - GitOps のメインブランチに変更をプッシュし、ArgoCD に同期させる（または検証用に手動で `kubectl apply` する）。
  - `kubectl get pods -n monitoring` で `alloy` DaemonSet Pod が正常稼働しているか確認する。
  - _Requirements: 2_

- [x] 4. メトリクス削減効果の測定
  - `alloy` DaemonSet のいずれかの Pod に対して 12345 ポートをポートフォワードする.
  - `curl -s http://localhost:12345/metrics` から `prometheus_remote_write_active_series` の値を取得し、大幅に削減（1ノードあたり 2,000 以下、合計 6,000 以下）されていることを確認する。
  - 主要リソース監視用メトリクス（例: `container_memory_working_set_bytes` 等）が引き続き出力されていることを確認する。
  - _Requirements: 2_
