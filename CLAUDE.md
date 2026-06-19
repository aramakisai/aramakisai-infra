# 🔧 CLAUDE.md

## プロジェクト概要
荒牧祭実行委員会の情報基盤インフラを管理するリポジトリ。
Terraform で Hetzner Cloud・Cloudflare・Tailscale を管理し、Ansible で K3s をブートストラップし、ArgoCD で GitOps 管理を行う。

## クイックリンク
- [GEMINI.md](GEMINI.md) — Agentic SDLC & Spec-Driven Development ルール
- [steering/tech.md](.kiro/steering/tech.md) — 詳細な技術構成・変数・シークレット一覧
- [steering/structure.md](.kiro/steering/structure.md) — ディレクトリ構成・パターン・ドキュメント同期ルール

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
ansible-playbook -i ansible/inventory/tailscale.yml ansible/playbooks/k3s-bootstrap.yml

# 特定ノードのみ対象
ansible-playbook -i ansible/inventory/tailscale.yml ansible/playbooks/k3s-bootstrap.yml --limit prod-node-2

# K3s バージョンアップ
ansible-playbook -i ansible/inventory/tailscale.yml ansible/playbooks/k3s-bootstrap.yml -e "k3s_version=v1.33.0+k3s1"

# ノードの強制再プロビジョニング
ansible -i ansible/inventory/tailscale.yml prod-node-1 -m shell -a "/usr/local/bin/k3s-uninstall.sh"
ansible-playbook -i ansible/inventory/tailscale.yml ansible/playbooks/k3s-bootstrap.yml --limit prod-node-1
```

### 3. K3s 操作・検証
```bash
# ノード接続 (Tailscale SSH)
ssh root@prod-node-1

# クラスター状態確認 (ホスト上)
kubectl get nodes -o wide
kubectl get pods -A
```

## ブートストラップフロー

1. **Terraform Apply**: Hetzner ノード作成 (cloud-init で Tailscale 自動インストール) & 各種 DNS・トンネル設定。
2. **Ansible 実行 (手動)**:
   - K3s (`--cluster-init`) インストール。
   - Cilium CNI を Helm でデプロイ (ノードが Ready になるまで待機)。
   - cloudflared の事前インストール (ArgoCDへの外部アクセス経路確保)。
   - kubeconfig を手元に取得し、Tailscale IP に書き換え。
   - ArgoCD インストール ＆ `infisical-auth` Secret の直接作成。
   - GitHub Deploy Key 登録 ＆ `root.yaml` (App of Apps) 適用。
3. **ESO同期**: `sync-wave: "-1"` により ESO と ClusterSecretStore が先行起動し、`wave: 0` の各アプリ（Authentik, Directus, mailserver 等）に必要なシークレットを自動注入。

## 変更時更新ナビゲーション（ドキュメント同期）

コード変更やインフラ構成の変更を行った際、以下の基準に従って関連ドキュメントを同期更新してください。

- **インフラ/構成の変更**:
  - **Ansible タスク/ロールの追加・変更**: [CLAUDE.md](CLAUDE.md) の「主要コマンド」や [steering/tech.md](.kiro/steering/tech.md) の「Key Technical Decisions」に影響がないか確認し更新。
  - **新規サービス/マニフェストの追加**: [steering/structure.md](.kiro/steering/structure.md) の設計思想に沿っているか確認し、必要に応じて構成を追記。
  - **Infisical 環境変数/シークレットの追加**: [steering/tech.md](.kiro/steering/tech.md) の「Infisical で管理するシークレット一覧」へキー名を追記（※値自体は記載しない）。
- **仕様完了時**:
  - `spec.json` の `phase` を `completed` にする前に、新規サービスや追加シークレットが上記基準に沿ってプロジェクトメモリに正しく同期されているか検証・転記すること。
