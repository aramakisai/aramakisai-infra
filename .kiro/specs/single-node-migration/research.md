# リサーチログ (Research) - シングルノード移行

## 調査サマリー

**調査種別**: 既存システム分析（Extension）  
**調査範囲**: Terraform・Ansible・GitOps 全レイヤーの依存関係分析、K3s シングルノード設計、Raspberry Pi webhook 自動化パターン

### 重要な知見

1. **ノード名 "prod-node-1" の維持が最小変更パス**  
   `terraform/dns.tf` が `hcloud_server.nodes["prod-node-1"]` を参照し、`gitops/manifests/prod/stalwart/statefulset.yaml` の nodeSelector も `prod-node-1` を使用している。ノード名を変えると両ファイルの変更が必要になるため、単一ノードも "prod-node-1" と命名する。

2. **K3s シングルノードは `cluster-init: true` のまま動作する**  
   `ansible/roles/k3s-server/templates/config.yaml.j2` の `cluster-init` ブランチはシングルノードでも有効。Play 2（`k3s_server_worker` グループへのローリングインストール）はグループが空になることで自動スキップされる。Playbook への変更は不要。

3. **infisical-auth と ArgoCD SSH 鍵は Ansible Play 6 が注入する（2026-06-02 インシデント）**  
   `argocd` namespace の `infisical-auth`・`aramakisai-infra-repo` は ESO を経由しない唯一の Secret。Ansible 実行前に `.env` を source しないと両方が空になり、ESO 全体が停止する。復旧スクリプトはこの手順を必須化しなければならない。

4. **Raspberry Pi からの HCP Terraform API トリガーが最適**  
   `null_resource` + `local-exec` による Ansible 自動実行は HCP Terraform のリモート実行環境で動作しない（`main.tf:82` でコメントアウト済み）。Raspberry Pi が Terraform Cloud API を呼び出してプランを実行し、完了後に直接 `ansible-playbook` を実行する構成が最もシンプル。

5. **CNPG シングルノードでの podAntiAffinity**  
   両 DB クラスター（authentik-db・directus-db）は `podAntiAffinityType: preferred` を設定済み。シングルノードでは `preferred` のため失敗ではなくスケジュール可能だが、`instances: 1` に変更してアフィニティブロックを削除するのが意図明確。

6. **Hetzner Object Storage は Terraform で作成不可**  
   `hcloud` プロバイダーは `hcloud_object_storage_bucket` をサポートしない（`terraform/storage.tf` のコメントに明記）。バケット作成は Hetzner Console での手動作業（`backup` スペックのタスク 1 が担当）。

## 調査ログ

### トピック 1: Terraform ノード依存関係の分析

**調査内容**: `terraform/` 全ファイルのノード参照  
**結果**:
- `main.tf`: `local.nodes` で3ノードを for_each 定義
- `dns.tf`: `hcloud_server.nodes["prod-node-1"]` が mail.aramakisai.com の IPv4/IPv6 に使用
- `outputs.tf`: `cp_node_ipv6`・`prod_node_1_ipv6`・`prod_node_2_ipv6` の3出力
- `tunnel.tf`: サービス名ベースのルーティング（ノード名参照なし）
- `firewall.tf`・`network.tf`・`tailscale.tf`・`access.tf`: ノード名参照なし

**設計への影響**: `main.tf` の `local.nodes` と `server_type`、`outputs.tf` の不要出力削除のみ変更。

### トピック 2: Ansible インベントリ・Playbook 構造

**調査内容**: `ansible/inventory/tailscale.yml`・`ansible/playbooks/k3s-bootstrap.yml`  
**結果**:
- インベントリは `k3s_server`（cp-node）と `k3s_server_worker`（prod-node-1, prod-node-2）の2グループ
- Playbook の Play 1 は `k3s_server` グループを対象（`cluster-init` ノード）
- Play 2 は `k3s_server_worker` グループを対象（`serial: 1`）
- Play 3〜6 は `k3s_server[0]` を対象（`run_once: true` 相当）

**設計への影響**: インベントリを単一ホスト（prod-node-1）・単一グループ（k3s_server）に変更。Playbook は変更不要。

### トピック 3: Raspberry Pi 復旧自動化パターン

**調査内容**: Terraform Cloud API・Grafana Cloud Webhook・Linux webhook ツール  
**結果**:
- Terraform Cloud API: `POST /api/v2/runs` でプランを作成・`POST /api/v2/runs/{id}/actions/apply` で適用
- Grafana Cloud: Contact Point に Webhook を設定可能（POST with JSON body）
- Python `http.server` or `flask` or `webhook` ツールでシンプルな受信サーバー構築可能
- ロック: `/tmp/recovery.lock` ファイルによる多重起動防止が最もシンプル
- 通知: 復旧完了後に Grafana Cloud の Resolved 状態を確認

**設計への影響**: Pi 上に Python Flask ベースの軽量 webhook サービスを実装。systemd unit で常駐化。

### トピック 4: CNPG 障害復旧戦略

**調査内容**: CNPG recovery bootstrap・barmanObjectStore  
**結果**:
- CNPG の `recovery` bootstrap は既存の S3 バックアップが必要（初回デプロイ時は `initdb`）
- WAL アーカイブが有効な場合、`recovery` bootstrap で任意の時点にリストア可能
- 移行後: `initdb` で起動 → pg_dump リストア → WAL アーカイブ開始
- 障害復旧時: Pi スクリプトが `db-cluster.yaml` の bootstrap を `recovery` に変更してコミット → ArgoCD が自動適用

**設計への影響**: 通常時は `initdb` ブートストラップを維持。Pi の DR スクリプトが `recovery` に切り替えるための `db-cluster-recovery.yaml` パッチを準備。

### リスク評価

| リスク | 確率 | 影響 | 軽減策 |
|--------|------|------|--------|
| Pi 自体の障害 | 低 | 高（自動復旧不可） | Grafana Cloud のアラートで手動対応は可能 |
| Tailscale 登録遅延 | 中 | 中（スクリプトが待機） | ポーリングに最大10分のタイムアウトを設定 |
| infisical-auth 空注入 | 中 | 高（ESO 全停止） | スクリプトに環境変数チェックを強制 |
| CNPG recovery 失敗 | 低 | 高（DB 復旧不可） | fallback: Google Drive の pg_dump からリストア |
