# 基本設計 (Design) - 監視

## 1. 全体構成

```
K3s クラスター (全3ノード)
  └── Grafana Alloy DaemonSet (monitoring namespace)
        ├── Kubernetes API → Pod ログ収集
        │     └── HTTP POST → Grafana Cloud Loki
        └── localhost:12345 → Alloy 自身のメトリクス収集
              └── remote_write → Grafana Cloud Prometheus

認証フロー:
  Infisical → ESO ClusterSecretStore
    → alloy-secrets Secret (monitoring namespace)
      → Alloy Pod が envFrom で読み込む
```

## 2. コンポーネント詳細

### 2.1. ExternalSecret

`gitops/manifests/shared/eso/alloy-external-secret.yaml` を新規作成。
`ClusterSecretStore` は namespace をまたいで使えるため `monitoring` namespace にも適用可能。

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: alloy-secrets
  namespace: monitoring
spec:
  secretStoreRef:
    kind: ClusterSecretStore
    name: infisical
  target:
    name: alloy-secrets
  data:
    - secretKey: LOKI_URL
      remoteRef:
        key: LOKI_URL
    - secretKey: LOKI_USERNAME
      remoteRef:
        key: LOKI_USERNAME
    - secretKey: LOKI_PASSWORD
      remoteRef:
        key: LOKI_PASSWORD
    - secretKey: PROMETHEUS_REMOTE_WRITE_URL
      remoteRef:
        key: PROMETHEUS_REMOTE_WRITE_URL
    - secretKey: PROMETHEUS_USERNAME
      remoteRef:
        key: PROMETHEUS_USERNAME
    - secretKey: PROMETHEUS_PASSWORD
      remoteRef:
        key: PROMETHEUS_PASSWORD
```

### 2.2. alloy.yaml の ConfigMap 修正

現状の `prometheus.scrape "node"` は `localhost:9100` (node-exporter) を scrape しているが node-exporter は未デプロイ。
**Alloy 自身のメトリクス** (`localhost:12345`) に変更することで node-exporter なしで動作させる。

```alloy
// 変更前
prometheus.scrape "node" {
  targets = [{ __address__ = "localhost:9100" }]
  ...
}

// 変更後
prometheus.scrape "alloy_self" {
  targets = [{ __address__ = "localhost:12345" }]
  forward_to = [prometheus.remote_write.default.receiver]
}
```

### 2.3. ArgoCD Application

`gitops/apps/prod/monitoring.yaml` を新規作成。
`shared/monitoring/` は prod・staging 共通のため `apps/prod/` に置くが path は `gitops/manifests/shared/monitoring` を指す。

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: monitoring
  namespace: argocd
  annotations:
    argocd.argoproj.io/sync-wave: "0"
  finalizers:
    - resources-finalizer.argocd.argoproj.io
spec:
  project: default
  source:
    repoURL: git@github.com:aramakisai/aramakisai-infra.git
    targetRevision: main
    path: gitops/manifests/shared/monitoring
  destination:
    server: https://kubernetes.default.svc
    namespace: monitoring
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
      - ServerSideApply=true
```

ExternalSecret は `gitops/manifests/shared/eso/` にあり `cluster-secret-store.yaml` と同じ App (`cluster-secret-store`) で管理される。
`alloy-external-secret.yaml` は既存の `cluster-secret-store` App のパスに追加するだけで自動 sync される。

## 3. 検証方法

```bash
# DaemonSet の状態確認
kubectl get daemonset alloy -n monitoring
kubectl get pods -n monitoring -o wide   # 全3ノードに1Podずつ

# ログ転送確認
kubectl logs -n monitoring -l app=alloy --tail=50 | grep -E "error|loki"

# Grafana Cloud Loki で確認
# Explore → Loki → クエリ: {namespace="prod"}
```
