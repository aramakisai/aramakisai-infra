# Research Report - 実装とドキュメントの乖離調査 (document-update)

本レポートは、最新のインフラ実装（Terraform、Ansible、GitOps）のコードベースと、主要ドキュメント（`CLAUDE.md`、`README.md`、`.kiro/steering/` 以下の各種設計ドキュメント）を照合し、検出された記述の乖離（ギャップ）をまとめたものです。

---

## 1. 検出された乖離（ギャップ）一覧

### 1.1. 3ノードHA構成からシングルノード構成（`prod-node-1`）への移行関連
- **対象ドキュメント**: `README.md`, `CLAUDE.md`, `tech.md`, `dr.md`
- **コードの実態**:
  - `ansible/inventory/tailscale.yml` では `k3s_server` グループに `prod-node-1`（CX33、シングルノード）のみが登録されており、HA構成用の `cp-node` や `prod-node-2/3` は使用されていません。
- **乖離内容**:
  - `README.md` の「アーキテクチャ概要」の図および説明に `cp-node`、`prod-node-1`、`prod-node-2` による 3 ノード HA クラスターの記述が残っています。
  - `README.md` の「初回セットアップ（Hetzner ノード × 3 作成...）」や「K3s バージョンアップ（cp-node → prod-node-1 → prod-node-2）」に古い HA 構成前提の記述が残っています。
  - `README.md` の「ブレークグラス」に `ssh root@cp-node` という存在しないホストへの言及があります。
  - `CLAUDE.md` の「主要コマンド」等で `--limit prod-node-2` や `cp-node` への言及が残っています。

### 1.2. メールサーバーの DMS (Docker Mailserver) への移行関連
- **対象ドキュメント**: `README.md`
- **コードの実態**:
  - `gitops/apps/prod/mailserver.yaml` および `gitops/manifests/prod/mailserver/` で `Docker Mailserver (DMS)` v14 がデプロイされています（`Stalwart` から移行済み）。
- **乖離内容**:
  - `README.md` の「デプロイされるサービス」のテーブルで、メールサーバー the project uses が `Stalwart` のままになっています。

### 1.3. Terraform プロバイダー・出力パラメータの追加
- **対象ドキュメント**: `.kiro/steering/tech.md`
- **コードの実態**:
  - `terraform/providers.tf` に `authentik`、`uptimerobot`、`healthchecksio`、`netdata` プロバイダーが追加されています。
  - `terraform/outputs.tf` に `healthchecksio_mailserver_backup_ping_url` (sensitive) および `netdata_room_id` 出力が定義されています。
- **乖離内容**:
  - `tech.md` の「Key Providers & Versions」テーブルに `authentik`、`uptimerobot`、`healthchecksio`、`netdata` プロバイダーが掲載されていません。
  - `tech.md` で、これら新規追加された出力パラメータのドキュメント化が不足しています。

### 1.4. Ansible `swap` ロールの追加
- **対象ドキュメント**: `.kiro/steering/structure.md`, `README.md`
- **コードの実態**:
  - `ansible/roles/swap/` ロールが追加され、全ノードに 4GB の swap ファイルを追加し、kubelet 設定の `fail-swap-on=false` と合わせて機能（Pod は NoSwap 構成）させています。
- **乖離内容**:
  - `structure.md` の「Ansible (`ansible/`)」ディレクトリ構造および説明に `swap` ロールが記述されていません。
  - `README.md` の「ディレクトリ構成」に `ansible/roles/swap` ロールが記述されていません。また、swap 設定についての言及もありません。

### 1.5. Directus DB メモリ制限と CNPG PostgreSQL バージョン
- **対象ドキュメント**: `.kiro/steering/tech.md`, `.kiro/steering/dr.md`
- **コードの実態**:
  - `gitops/manifests/prod/directus/db-cluster.yaml` で `resources.limits.memory` が `512Mi` に設定されており、`ghcr.io/cloudnative-pg/postgresql:16.8` イメージが使用されています。
- **乖離内容**:
  - `tech.md` では、Directus DB メモリ制限（512Mi）に関する詳細な更新理由（バックアップ時のメモリスパイクによる OOM 回避）の補強が必要です。
  - `dr.md` には既に PostgreSQL 16.8 や WAL バックアップからの自動復元について記述がありますが、`tech.md` にも PostgreSQL のバージョン情報を同期する必要があります。

### 1.6. ArgoCD configmap informer のラベル要件
- **対象ドキュメント**: `.kiro/steering/dr.md`, `.kiro/steering/tech.md`
- **コードの実態**:
  - `playbooks/k3s-bootstrap.yml` および `gitops/manifests/prod/argocd/` で、ArgoCD の configmap (`argocd-cm` / `argocd-rbac-cm`) にラベル（`app.kubernetes.io/name` / `app.kubernetes.io/part-of`）を明示的に付与し、ArgoCD v3.4.4 での起動クラッシュを回避しています。
- **乖離内容**:
  - `tech.md` の「Key Technical Decisions (ArgoCD)」等で、この安定性向上のためのラベル要件についての解説が不足しています。

### 1.7. 監視除外（Falco カスタムルール）の知見
- **対象ドキュメント**: `.kiro/steering/tech.md`
- **コードの実態**:
  - `gitops/helm-values/prod/falco.yaml` で、`argocd`、`authentik`、`cloudnative-pg`、`netdata` 等の k8s API への定常アクセス、および `mailserver` や `cert-manager` による `/etc` 配下への書き込みを Falco の警告から除外するカスタムルールが定義されています。
- **乖離内容**:
  - `tech.md` の「監視スタック」や「Key Technical Decisions」にこれらの具体的な除外マクロ・ルールの知見が十分に反映されていません。

---

## 2. ドキュメント別 修正計画

| 修正対象ファイル | 修正内容 |
|---|---|
| `CLAUDE.md` | - `prod-node-2` や `cp-node` への言及を削除し、シングルノード（`prod-node-1`）に統一。<br>- ドキュメント同期ガイドの記述を整理・チェックリスト化。 |
| `README.md` | - アーキテクチャ図および記述をシングルノード構成に更新。<br>- デプロイされるサービスから `Stalwart` を削除し、`Docker Mailserver (DMS)` に変更。<br>- ディレクトリ構造図に `ansible/roles/swap` を追記。<br>- ブレークグラスなどのコマンド例から `cp-node` や `prod-node-2` を排除し `prod-node-1` に変更。<br>- `swap` ロールについての簡潔な説明を追加。 |
| `tech.md` | - Key Providers テーブルに `authentik`、`uptimerobot`、`healthchecksio`、`netdata` を追記。<br>- 新規追加された Terraform 出力（`healthchecksio_mailserver_backup_ping_url` / `netdata_room_id`）を追記。<br>- `Swap設定` にホスト側の OOM 安全弁および NoSwap 設計の詳細を追加。<br>- `Directus` の DB メモリ制限を 512Mi に戻した経緯を追記。<br>- Falco で除外されている k8s API 定常アクセスや正常プロセスの監視除外ナレッジを追記。<br>- ArgoCD のラベル要件（v3.4.4 での configmap informer クラッシュ対策）を追記。 |
| `structure.md` | - `ansible/` の構成説明に `swap` ロール（全ノード共通のホスト側 OOM 安全弁）を追記。 |
| `dr.md` | - 記述全体がコードと整合していることを再確認し、必要に応じて微修正。 |
