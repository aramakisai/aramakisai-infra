# 基本設計 (Design) - HA 改善

## 1. 変更対象と影響範囲

| リソース | 変更内容 | 影響 |
|---------|---------|------|
| `authentik/db-cluster.yaml` | `instances: 1 → 3` + podAntiAffinity | 再起動あり (ローリング) |
| `directus/db-cluster.yaml` | `instances: 1 → 3` + podAntiAffinity | 再起動あり (ローリング) |
| `cloudflared/deployment.yaml` | `topologySpreadConstraints` 追加 | Pod 再スケジュールの可能性あり |
| cloudflared PDB (新規) | `minAvailable: 1` | — |
| authentik PDB (新規) | server / worker 各 `minAvailable: 1` | — |
| roundcube PDB (新規) | `minAvailable: 1` | — |

## 2. コンポーネント詳細

### 2.1. CloudNativePG インスタンス数変更

CNPG は `instances` を増やすと自動でローリング方式で Standby を追加する。
Standby は Primary と同じ namespace に作成されるため、`podAntiAffinity` で別ノードへの分散を強制する。

#### db-cluster.yaml への変更 (Authentik / Directus 共通)

```yaml
spec:
  instances: 3   # 1 Primary + 2 Standby

  affinity:
    podAntiAffinity:
      preferredDuringSchedulingIgnoredDuringExecution:
        - weight: 100
          podAffinityTerm:
            labelSelector:
              matchLabels:
                cnpg.io/cluster: <cluster-name>
            topologyKey: kubernetes.io/hostname
```

`requiredDuringScheduling` ではなく `preferred` (Soft Affinity) を使う理由:
- `required` (Hard) にすると、1 ノードをドレインした際に 3 Pod 目が空きノードを見つけられず Pending のまま停止する
- `preferred` (Soft) にすると、ドレイン中は一時的に同一ノードへの共存を許容して全 Pod を Running のまま保てる
- 3 ノード構成では通常 `preferred` と `required` の効果は同じだが、メンテナンス中の可用性を優先して `preferred` を採用する

#### フェイルオーバー動作

- Primary ノード障害時: CNPG が自動で Standby を Primary に昇格 (30〜60秒)
- `-rw` Service は新 Primary を指すよう自動更新される
- `-ro` Service は残りの Standby を指す

### 2.2. cloudflared トポロジー分散

#### deployment.yaml への追加

```yaml
spec:
  template:
    spec:
      topologySpreadConstraints:
        - maxSkew: 1
          topologyKey: kubernetes.io/hostname
          whenUnsatisfiable: ScheduleAnyway   # 2 ノードしか空きがなくても起動する
          labelSelector:
            matchLabels:
              app: cloudflared
```

`DoNotSchedule` ではなく `ScheduleAnyway` を採用する理由:
ノードメンテナンス中に 2 Pod が同一ノードに集まる一時的な状態を許容し、
スケジュール失敗で Tunnel が落ちるリスクを避ける。

### 2.3. PodDisruptionBudget

各サービスのマニフェストディレクトリに `pdb.yaml` を新規作成する。

#### cloudflared PDB

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: cloudflared-pdb
  namespace: cloudflared
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app: cloudflared
```

#### authentik PDB (server と worker を別々に)

```yaml
# pdb.yaml (server)
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: authentik-server-pdb
  namespace: prod
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: authentik
      app.kubernetes.io/component: server
---
# pdb.yaml (worker)
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: authentik-worker-pdb
  namespace: prod
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: authentik
      app.kubernetes.io/component: worker
```

#### roundcube PDB

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: roundcube-pdb
  namespace: prod
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app: roundcube
```

## 3. ノード障害時の動作 (改善後)

| サービス | 障害前 | 改善後 |
|---------|-------|-------|
| Authentik DB | PVC がノードに縛られ停止 | Standby が Primary に昇格、30〜60秒で復旧 |
| Directus DB | 同上 | 同上 |
| cloudflared | 2 Pod が同一ノードなら全断 | 必ず別ノードに分散済み、片方が継続 |
| Authentik server/worker | Pod 再スケジュールで数分停止 | 変わらず (stateless のため許容) |

**Stalwart は改善対象外**: `hostNetwork: true` + `nodeSelector: prod-node-1` の制約により、
prod-node-1 障害時はメール送受信・Webmail ともに停止する。対応は別途検討。

## 4. 検証方法

```bash
# CNPG Standby 確認
kubectl get pods -n prod -l cnpg.io/cluster=authentik-db -o wide

# cloudflared Pod の分散確認
kubectl get pods -n cloudflared -o wide

# PDB 一覧確認
kubectl get pdb -A

# フェイルオーバーテスト (Standby 確認後)
kubectl delete pod -n prod -l cnpg.io/cluster=authentik-db,role=primary
kubectl get pods -n prod -l cnpg.io/cluster=authentik-db -w
```
