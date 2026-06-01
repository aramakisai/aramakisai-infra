# タスク定義 (Tasks) - HA 改善

## タスク一覧

- [ ] 1. CloudNativePG レプリカ数の増加
- [x] 1.1 (P) Authentik DB を instances: 3 に変更
  - `gitops/manifests/prod/authentik/db-cluster.yaml` の `instances` を `1` → `3` に変更する
  - `spec.affinity.podAntiAffinity` (preferredDuringScheduling / topologyKey: kubernetes.io/hostname) を追加して各インスタンスが別ノードに分散されるよう設定する
  - `kubectl get pods -n prod -l cnpg.io/cluster=authentik-db -o wide` で 3 Pod が異なるノードに配置されれば完了
  - _Requirements: 1_
  - _Boundary: authentik/db-cluster.yaml_

- [x] 1.2 (P) Directus DB を instances: 3 に変更
  - `gitops/manifests/prod/directus/db-cluster.yaml` に同様の変更を加える
  - `kubectl get pods -n prod -l cnpg.io/cluster=directus-db -o wide` で 3 Pod が異なるノードに配置されれば完了
  - _Requirements: 1_
  - _Boundary: directus/db-cluster.yaml_

- [ ] 2. cloudflared のトポロジー分散設定
  - `gitops/manifests/prod/cloudflared/deployment.yaml` に `topologySpreadConstraints` (maxSkew: 1 / topologyKey: kubernetes.io/hostname / whenUnsatisfiable: ScheduleAnyway) を追加する
  - `kubectl get pods -n cloudflared -o wide` で 2 Pod が異なるノードに配置されれば完了
  - _Requirements: 2_

- [ ] 3. PodDisruptionBudget の追加
- [ ] 3.1 (P) cloudflared PDB を作成
  - `gitops/manifests/prod/cloudflared/pdb.yaml` を新規作成し、`minAvailable: 1` を設定する
  - `kubectl get pdb -n cloudflared` で `cloudflared-pdb` が表示されれば完了
  - _Requirements: 3_
  - _Boundary: cloudflared/pdb.yaml_

- [ ] 3.2 (P) Authentik PDB を作成
  - `gitops/manifests/prod/authentik/pdb.yaml` を新規作成し、server と worker それぞれに `minAvailable: 1` の PDB を定義する
  - `kubectl get pdb -n prod | grep authentik` で 2 件表示されれば完了
  - _Requirements: 3_
  - _Boundary: authentik/pdb.yaml_

- [ ] 3.3 (P) Roundcube PDB を作成
  - `gitops/manifests/prod/roundcube/pdb.yaml` を新規作成し、`minAvailable: 1` を設定する
  - `kubectl get pdb -n prod | grep roundcube` で表示されれば完了
  - _Requirements: 3_
  - _Boundary: roundcube/pdb.yaml_

- [ ] 4. フェイルオーバー動作の検証
  - Authentik DB の Primary Pod を手動削除し、Standby が Primary に昇格することを確認する (`kubectl delete pod -n prod <primary-pod>` → `kubectl get pods -n prod -l cnpg.io/cluster=authentik-db -w`)
  - cloudflared の一方の Pod を削除し、もう一方が Tunnel を維持することを確認する
  - `kubectl drain <node> --ignore-daemonsets --delete-emptydir-data` でノードドレイン時に PDB が効いて最低 1 Pod が残ることを確認する
  - 3 項目すべての動作が確認できれば完了
  - _Requirements: 1, 2, 3_
  - _Depends: 1.1, 1.2, 2, 3.1, 3.2, 3.3_
