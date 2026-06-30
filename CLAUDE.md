# 🔧 CLAUDE.md

## プロジェクト概要
荒牧祭実行委員会の情報基盤インフラを管理するリポジトリ。
Terraform で Hetzner Cloud・Cloudflare・Tailscale を管理し、Ansible で K3s をブートストラップし、ArgoCD で GitOps 管理を行う。

## クイックリンク
- [GEMINI.md](GEMINI.md) — Agentic SDLC & Spec-Driven Development ルール
- [steering/tech.md](.kiro/steering/tech.md) — 詳細な技術構成・変数・シークレット一覧
- [steering/structure.md](.kiro/steering/structure.md) — ディレクトリ構成・パターン・ドキュメント同期ルール
- [steering/dr.md](.kiro/steering/dr.md) — DR・運用の知見

## 主要コマンド

### 1. Terraform (IaC)
```bash
# 初期化
cd terraform && terraform init

# 差分確認 (Infisical経由で環境変数を注入)
infisical run --env=prod -- terraform plan

# 適用
infisical run --env=prod -- terraform apply
```

### 2. Ansible (構成管理)
```bash
# K3s ブートストラップ実行 (Terraform適用後に手動実行)
infisical run -- ansible-playbook -i ansible/inventory/tailscale.yml ansible/playbooks/k3s-bootstrap.yml

# K3s バージョンアップ
infisical run -- ansible-playbook -i ansible/inventory/tailscale.yml ansible/playbooks/k3s-bootstrap.yml -e "k3s_version=v1.32.3+k3s1"

# ノードの強制再プロビジョニング (シングルノード prod-node-1)
infisical run -- ansible -i ansible/inventory/tailscale.yml prod-node-1 -m shell -a "/usr/local/bin/k3s-uninstall.sh"
infisical run -- ansible-playbook -i ansible/inventory/tailscale.yml ansible/playbooks/k3s-bootstrap.yml
```

### 3. K3s 操作・検証
```bash
# ノード接続 (Tailscale SSH)
ssh root@prod-node-1

# クラスター状態確認 (ホスト上またはmake経由)
make kubectl ARGS="get nodes -o wide"
make kubectl ARGS="get pods -A"
```
決して `kubectl` を直接ローカルで実行しないこと。必ず `make kubectl` 経由で kubeconfig を注入して実行すること。

### GitOps 原則：クラスタへの直接操作禁止

`gitops/` 配下のリソースはすべて ArgoCD が正本。**直接 `kubectl patch/edit/apply` でクラスタを変更することを禁止する。**

live リソースに Git にない差分（直接 patch で追加されたフィールド等）が残存していても、ArgoCD の client-side apply は「last-applied-configuration に記録されていない外部追加フィールド」を除去しないため、ArgoCD が "Synced" を表示していても実際はドリフトが存在する場合がある。

**クラスタリソースを修正する正しい手順:**

1. Git manifest を修正してコミット・プッシュ
2. ArgoCD で sync（必要なら Hard Refresh + Force Sync / server-side apply）

```bash
# SSA で全フィールドの ownership を ArgoCD に渡す（フィールドドリフト解消）
argocd app sync <app-name> --server-side --force
```

**例外（直接操作が許される場合）:** ArgoCD 自身が管理しない Bootstrap リソース（`infisical-auth` Secret、ArgoCD インストール直後の初期 Secret 等）のみ。

## ブートストラップフロー

1. **Terraform Apply**: Hetzner シングルノード（`prod-node-1`）作成 (cloud-init で Tailscale 自動インストール) & 各種 DNS・トンネル・サードパーティ（Authentik、Netdata、Healthchecks.io等）設定。
2. **Ansible 実行 (手動)**:
   - ホスト側の OOM 安全弁として swap ファイル（4GB）を作成。
   - K3s (`--cluster-init`, `fail-swap-on=false`) インストール。
   - Cilium CNI を Helm でデプロイ (ノードが Ready になるまで待機)。
   - cloudflared の事前インストール (ArgoCDへの外部アクセス経路確保)。
   - kubeconfig を手元に取得し、Tailscale IP に書き換え、Infisical へ登録。
   - ArgoCD インストール ＆ `infisical-auth` Secret の直接作成。
   - GitHub Deploy Key 登録 ＆ `root.yaml` (App of Apps) 適用。
3. **ESO同期**: `sync-wave: "-1"` により ESO と ClusterSecretStore が先行起動し、`wave: 0` の各アプリ（Authentik, Directus, DMS, Vaultwarden 等）に必要なシークレットを自動注入。

## 変更時更新ナビゲーション（ドキュメント同期）

コード変更やインフラ構成の変更を行った際、以下のチェックリストに従って関連ドキュメントを同期更新してください。

### ドキュメント更新チェックリスト
- [ ] **Terraform 設定（プロバイダー、出力など）の変更**:
  - 新規プロバイダーやバージョンの変更があるか？ → [.kiro/steering/tech.md](.kiro/steering/tech.md) の「Key Providers & Versions」を更新。
  - 新規出力パラメータを追加したか？ → [.kiro/steering/tech.md](.kiro/steering/tech.md) の「Key Technical Decisions (Terraform 変更)」や出力パラメータ説明に追記。
- [ ] **Ansible ロールやプレイブックの変更**:
  - ディレクトリ構造に変更があったか？ → [README.md](README.md) の「ディレクトリ構成」および [.kiro/steering/structure.md](.kiro/steering/structure.md) を更新。
  - ホスト制限、クラスター構成、swap やカーネルパラメータ設定などの変更か？ → [.kiro/steering/tech.md](.kiro/steering/tech.md) を更新。
- [ ] **GitOps マニフェストやアプリ構成の変更**:
  - 新規サービスやサブドメインを追加したか？ → [README.md](README.md) の「デプロイされるサービス」テーブルおよび [.kiro/steering/structure.md](.kiro/steering/structure.md) を更新。
  - 新規環境変数やシークレットが追加されたか？ → [.kiro/steering/tech.md](.kiro/steering/tech.md) の「Infisical で管理するシークレット一覧」にキー名を追記（※値は含めない）。
  - DB の構成（CNPG等）やリストア・バックアップロジックの変更か？ → [.kiro/steering/dr.md](.kiro/steering/dr.md) の CNPG / 各アプリのバックアップセクションを更新。
  - 監視（Falco）などの誤検知除外設定に変更があるか？ → [.kiro/steering/tech.md](.kiro/steering/tech.md) の「監視スタック」または「Key Technical Decisions」に知見を追記。
- [ ] **仕様完了時（phase: completed 前）**:
  - 新規サービスや追加シークレットが上記基準に沿ってプロジェクトメモリ（`steering/` 内）に正しく同期されているか検証・転記すること。

