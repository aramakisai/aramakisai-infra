# 要件定義 (Requirements) - HA 改善

## 1. 目的

現状、アプリケーション層のほぼすべてが単一障害点になっている。
特に CloudNativePG の `instances: 1` 設定はノード障害時に DB 停止を引き起こし、
cloudflared の 2 レプリカは同一ノードに集中する可能性がある。
最小コストで耐障害性を高める改善を行う。

## 2. 現状と前提条件

- **CloudNativePG**: Authentik DB / Directus DB ともに `instances: 1`。ノード障害でストレージごと消える
- **cloudflared**: `replicas: 2` だが `topologySpreadConstraints` なし。同一ノードに集中する可能性がある
- **PodDisruptionBudget**: すべてのサービスで未設定。ノードドレイン時に全 Pod が同時に停止する可能性
- **Stalwart**: `hostNetwork: true` + `nodeSelector: prod-node-1` で固定。HA は port 25 のバインド制約により実現困難
- **K3s etcd**: 3 ノード構成でクォーラム 2/3。1 ノード障害で継続可能 (対応不要)

## 3. 要件

### 要件 1: CloudNativePG レプリカ数の増加
- Authentik DB (`authentik-db`) のインスタンス数を 1 → 3 に変更する (Primary 1 + Standby 2)
- Directus DB (`directus-db`) のインスタンス数を 1 → 3 に変更する
- ノード障害時に Standby への自動フェイルオーバーが行われ、サービスが継続すること
- `podAntiAffinity` または `topologySpreadConstraints` で各インスタンスが別ノードに配置されること

### 要件 2: cloudflared のトポロジー分散
- `topologySpreadConstraints` を設定し、2 つの cloudflared Pod が必ず異なるノードに配置されること
- ノード障害時も残り 1 Pod が Cloudflare Tunnel を維持し、外部からの HTTP アクセスが継続すること

### 要件 3: 重要サービスへの PodDisruptionBudget 追加
- cloudflared に PDB (`minAvailable: 1`) を設定し、ノードドレイン中も最低 1 Pod が稼働すること
- Authentik server / worker に PDB (`minAvailable: 1`) を設定する
- Roundcube に PDB (`minAvailable: 1`) を設定する

## 4. スコープ外

- Stalwart の HA: `hostNetwork: true` + port 25 の制約で現実的な解がないため対象外
- Longhorn 等の分散ストレージ: 大規模な変更が必要なため別スペックとする
- OS レベルのノード自動更新 (kured / system-upgrade-controller): 別スペックとする
- Roundcube の複数レプリカ化: PVC が ReadWriteOnce のため実現困難。PDB のみ対応
