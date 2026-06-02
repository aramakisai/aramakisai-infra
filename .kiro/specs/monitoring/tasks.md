# タスク定義 (Tasks) - 監視

## タスク一覧

- [x] 1. Grafana Cloud 接続情報を Infisical に登録 (手動作業)
  - Grafana Cloud → My Account → Stack → Loki の「Details」から `LOKI_URL`・`LOKI_USERNAME` を取得する
  - Grafana Cloud → Prometheus の「Details」から `PROMETHEUS_REMOTE_WRITE_URL`・`PROMETHEUS_USERNAME` を取得する
  - API キーを「My Account → API Keys」で生成し `LOKI_PASSWORD` / `PROMETHEUS_PASSWORD` として使用する (同一キーで可)
  - `.env.app-secrets.example` を参考に `.env.app-secrets` に6キーを記入し `scripts/push-secrets-to-infisical.sh` で Infisical に登録する
  - Infisical のダッシュボードで6キーが確認できれば完了
  - _Requirements: 1_

- [x] 2. alloy-external-secret.yaml を作成
  - `gitops/manifests/shared/eso/alloy-external-secret.yaml` を新規作成し、`monitoring` namespace に `alloy-secrets` Secret が展開されるよう6キーを定義する
  - ArgoCD sync 後に `kubectl get secret alloy-secrets -n monitoring` で6キーが存在することを確認すれば完了
  - _Requirements: 1_
  - _Depends: 1_

- [x] 3. alloy.yaml の ConfigMap を修正
  - `gitops/manifests/shared/monitoring/alloy.yaml` の `prometheus.scrape "node"` ブロックを `prometheus.scrape "alloy_self"` に変更し、scrape 対象を `localhost:9100` → `localhost:12345` (Alloy 自身のメトリクス) に修正する
  - `kubectl apply --dry-run=client -f alloy.yaml` で検証通過すれば完了
  - _Requirements: 4_

- [x] 4. ArgoCD Application と monitoring のデプロイ
- [x] 4.1 (P) monitoring ArgoCD Application を作成
  - `gitops/apps/prod/monitoring.yaml` を新規作成し、`gitops/manifests/shared/monitoring` を参照する Application を定義する
  - ArgoCD UI で `monitoring` Application が Synced / Healthy になれば完了
  - _Requirements: 2_
  - _Boundary: gitops/apps/prod/monitoring.yaml_

- [x] 4.2 (P) alloy-external-secret を cluster-secret-store App に追加
  - `gitops/manifests/shared/eso/alloy-external-secret.yaml` が `cluster-secret-store` ArgoCD App の管理下に入ることを ArgoCD UI で確認する
  - `kubectl get secret alloy-secrets -n monitoring` で Secret が展開されれば完了
  - _Requirements: 1_
  - _Boundary: gitops/manifests/shared/eso/_
  - _Depends: 2_

- [x] 5. ログ・メトリクス収集の疎通確認
  - `kubectl get pods -n monitoring -o wide` で全3ノードに Alloy Pod が Running で配置されていることを確認する
  - `kubectl logs -n monitoring -l app=alloy --tail=100` でエラーがなく Loki / Prometheus へのリクエストが送信されていることを確認する
  - Grafana Cloud の Explore 画面で `{namespace="prod"}` クエリを実行し Pod ログが表示されることを確認する
  - Grafana Cloud の Metrics Explore で `alloy_build_info` などの Alloy 自身のメトリクスが表示されることを確認する
  - 4項目すべて確認できれば完了
  - _Requirements: 2, 3, 4_
  - _Depends: 4.1, 4.2_
