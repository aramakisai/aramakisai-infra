# 要件定義 (Requirements) - 監視

## 1. 目的

現状、クラスターのログもメトリクスも収集されておらず障害発生時の原因調査ができない。
Grafana Alloy DaemonSet を全ノードにデプロイし、Pod ログとノードメトリクスを Grafana Cloud へ転送することで可視性を確保する。

## 2. 現状と前提条件

- **`gitops/manifests/shared/monitoring/alloy.yaml`**: DaemonSet・ConfigMap・ServiceAccount・ClusterRole が定義済み。Grafana Cloud への転送設定も記述されている
- **未作成のもの**:
  - `gitops/apps/prod/monitoring.yaml` — ArgoCD Application
  - `gitops/manifests/shared/eso/alloy-external-secret.yaml` — `alloy-secrets` ExternalSecret
- **Infisical 未登録**: Grafana Cloud の接続情報 (Loki / Prometheus) が未登録
- **Grafana Cloud**: アカウント作成・Stack 設定は実施済みを前提とする

## 3. 要件

### 要件 1: Grafana Cloud 接続情報の登録
- Grafana Cloud の Loki エンドポイント・ユーザー ID・API キーを Infisical に登録する
- Grafana Cloud の Prometheus remote_write エンドポイント・ユーザー ID・API キーを Infisical に登録する
- ESO ExternalSecret 経由で `alloy-secrets` Secret が `monitoring` namespace に展開されること

### 要件 2: Grafana Alloy の ArgoCD デプロイ
- `gitops/apps/prod/monitoring.yaml` を作成し、ArgoCD が `gitops/manifests/shared/monitoring/` を監視・同期すること
- Alloy DaemonSet が全 3 ノードに 1 Pod ずつ Running 状態でデプロイされること
- ノードに taint が付いていても Pod がスケジュールされること（`tolerations: Exists` 設定済み）

### 要件 3: ログ収集の確認
- 全 Pod のログが Grafana Cloud Loki に転送されること
- Grafana Cloud の Explore 画面でクラスターの Pod ログが検索できること

### 要件 4: メトリクス収集の確認
- ノードメトリクス (CPU / メモリ / ディスク) が Grafana Cloud Prometheus に転送されること
- ただし Alloy の設定では `localhost:9100` (node-exporter) を scrape しているが、**node-exporter は未デプロイ**のため、まず Alloy 自身のメトリクス (`localhost:12345`) を scrape する形に修正する

## 4. スコープ外

- Grafana Cloud アカウント・Stack の初期作成
- アラートルール・通知チャンネルの設定
- サービスメトリクス (Stalwart / Directus / Authentik の application metrics)
- node-exporter のデプロイ: 今回は Alloy 自身のメトリクスで疎通確認を行う。ホスト CPU / メモリ / ディスクの可視化は次のマイルストーンとして `prometheus-node-exporter` DaemonSet を追加し Alloy 経由で転送する構成を検討する
