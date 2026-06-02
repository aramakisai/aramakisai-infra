# タスク定義 (Tasks) - 監視

## タスク一覧

- [x] 1. Grafana Cloud 接続情報を Infisical に登録 (手動作業)
  - Grafana Cloud → My Account → Stack → Loki の「Details」から `LOKI_URL`・`LOKI_USERNAME` を取得する
  - Grafana Cloud → Prometheus の「Details」から `PROMETHEUS_REMOTE_WRITE_URL`・`PROMETHEUS_USERNAME` を取得する
  - API キーを Access Policies で生成 (`logs:write` + `metrics:write` スコープ必須) し `LOKI_PASSWORD` / `PROMETHEUS_PASSWORD` として使用する
  - `.env.app-secrets` に6キーを記入し `scripts/push-secrets-to-infisical.sh` で Infisical に登録する
  - _Requirements: 1_

- [x] 2. alloy-external-secret.yaml を作成
  - `gitops/manifests/shared/eso/alloy-external-secret.yaml` を新規作成
  - `cluster-secret-store` ArgoCD App が `shared/eso/` 全体を管理しているため自動的に管理下に入る
  - `cluster-secret-store.yaml` に `ignoreDifferences` を追加 (ESO デフォルト値付与による無限 sync 防止)
  - _Requirements: 1_

- [x] 3. alloy.yaml の ConfigMap を修正・拡張
  - 当初の `prometheus.scrape "node" (localhost:9100)` を `prometheus.scrape "alloy_self" (localhost:12345)` に変更
  - ArgoCD メトリクス (`argocd-metrics:8082`, `argocd-server-metrics:8083`, `argocd-repo-server:8084`) を追加
  - `prometheus.exporter.unix "node"` (Alloy 組み込み) でノードメトリクスを収集
  - kubelet / cAdvisor を API サーバープロキシ経由 (`kubernetes.default.svc:443/api/v1/nodes/<node>/proxy/metrics`) でスクレイプ
    - `MY_NODE_NAME` Downward API で自ノードのみに絞り込み
  - ClusterRole に `pods/log`, `nodes/metrics`, `nodes/proxy`, `endpoints`, `services`, nonResourceURLs を追加
  - DaemonSet に `/proc`, `/sys` ホストマウントと `MY_NODE_NAME` 環境変数を追加
  - メモリリミットを 256Mi → 512Mi に増加 (OOMKilled 対策)
  - _Requirements: 3, 4_

- [x] 4. ArgoCD Application と monitoring のデプロイ
- [x] 4.1 (P) monitoring ArgoCD Application を作成
  - `gitops/apps/prod/monitoring.yaml` を新規作成 (`gitops/manifests/shared/monitoring` 参照)
  - _Requirements: 2_

- [x] 4.2 (P) kube-state-metrics をデプロイ
  - `gitops/apps/prod/kube-state-metrics.yaml` を新規作成 (prometheus-community Helm chart)
  - `gitops/manifests/shared/monitoring/alloy-cluster.yaml` を新規作成 (1レプリカ Deployment)
    - kube-state-metrics をスクレイプ (DaemonSet 全台スクレイプによる 3x 問題を回避)
  - _Requirements: 3_

- [x] 5. ログ・メトリクス収集の疎通確認
  - 全3ノードに Alloy Pod が Running で配置されていることを確認
  - ログ収集: 76 ストリーム・全 namespace の Pod ログが Loki に到達
  - メトリクス送信: 199,921 サンプルが Grafana Cloud Prometheus に送信済み
  - 収集中のメトリクス: alloy_self / ArgoCD / node (unix exporter) / kubelet / cAdvisor / kube-state-metrics
  - _Requirements: 2, 3, 4_
