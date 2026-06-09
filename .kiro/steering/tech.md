# Technology Stack

## Architecture

3 層構成: **Terraform (IaC)** → **Ansible (構成管理)** → **ArgoCD GitOps (アプリ管理)**

```
infisical run -- terraform apply
  └── Hetzner ノード × 1 (CX33) + Cloudflare DNS/Tunnel/Access + Tailscale auth key

↓ Terraform 完了後に手動実行 (null_resource は HCP Terraform 非対応のためコメントアウト済み)

infisical run -- ansible-playbook k3s-bootstrap.yml
  └── K3s シングルノード → Cilium CNI → cloudflared → ArgoCD → App of Apps
```

ノードへの SSH は Tailscale 経由のみ。パブリックポート 22 は開放しない。

## Core Technologies

- **IaC**: Terraform >= 1.9、tfstate は Terraform Cloud で管理
- **構成管理**: Ansible >= 2.14
- **Kubernetes**: K3s v1.32.3+k3s1 (シングルノード、prod-node-1)
- **CNI**: Cilium (Flannel・NetworkPolicy は無効化)
- **GitOps**: ArgoCD — App of Apps パターン
- **シークレット**: Infisical + External Secrets Operator (ESO)
- **DB**: CloudNativePG (PostgreSQL Operator)

## Key Providers & Versions

| Provider | Source | Version |
|----------|--------|---------|
| hcloud | hetznercloud/hcloud | ~> 1.50 |
| tailscale | tailscale/tailscale | ~> 0.17 |
| cloudflare | cloudflare/cloudflare | ~> 4.0 |
| null | hashicorp/null | ~> 3.0 |

## Key Technical Decisions

### シークレット管理
- **ルール**: マニフェストに平文シークレットを書かない
- **方法**: `ExternalSecret` リソースで Infisical から取得
- **唯一の例外**: `infisical-auth` Secret のみ Ansible が直接 `kubectl apply` (ESO 自体の起動に必要なため)

### ブートストラップ順序
1. K3s インストール (prod-node-1: `--cluster-init`、シングルノード)
2. **Cilium CNI** を Helm でインストール (`--flannel-backend: none` のため必須。ないと全ノード NotReady)
3. cloudflared を `gitops/manifests/prod/cloudflared/` から直接 kubectl apply (ArgoCD への外部アクセス経路確保)
4. ArgoCD インストール → `infisical-auth` Secret 作成 → GitHub Deploy Key 登録 → App of Apps 適用
5. ESO が `sync-wave: "-1"` で先行 sync → 他アプリは `wave: 0`

### K3s クラスター設計
- シングルノード (prod-node-1) が etcd + ワークロードを担う。障害時は Raspberry Pi + Grafana Cloud Alerting による半自動コールドスタンバイ復旧
- サーバータイプ: CX33 (2vCPU/8GB/80GB NVMe)
- `--disable traefik,servicelb`: どちらも GitOps または Cloudflare Tunnel で代替
- `--embedded-registry`: Spegel によるノード間イメージキャッシュ

### Ansible 実行タイミング
- `null_resource` + `local-exec` は HCP Terraform のリモート実行環境で動作しないため `main.tf` でコメントアウト済み
- **Terraform 完了後に常に手動で Ansible を実行する**
- 設定変更のみの場合も Ansible を直接実行する

## 監視スタック

| コンポーネント | 役割 | 状態 |
|--------------|------|------|
| Grafana Alloy (DaemonSet) | Pod ログ + ノードメトリクス収集 | マニフェストあり・**未デプロイ** |
| Grafana Cloud Loki | ログ保存・検索 | 外部サービス (要アカウント) |
| Grafana Cloud Prometheus | メトリクス保存・アラート | 外部サービス (要アカウント) |

Alloy が収集したログ/メトリクスを Grafana Cloud に remote_write / Loki push する構成。
クラスター内に Prometheus サーバーは不要。

**未完了**: ArgoCD Application・ExternalSecret (`alloy-secrets`)・Infisical への6キー登録がない。

## Development Environment

### Required Tools
```
terraform >= 1.9
ansible >= 2.14
kubectl
tailscale (管理用SSH接続)
infisical (シークレット注入)
jq, curl
```

### シークレット管理ルール

**Infisical がすべてのシークレットの Single Source of Truth。**

- `.env` ファイルは使用しない。ローカルにシークレットを置かない
- コマンド実行時は `infisical run --` プレフィックスで環境変数を注入する
- `infisical login` で事前にログイン済みであること

### Common Commands
```bash
# IaC 差分確認
infisical run -- terraform -chdir=terraform plan -var-file="../secrets.tfvars"

# IaC 適用
infisical run -- terraform -chdir=terraform apply -var-file="../secrets.tfvars"

# Ansible 単体再実行
infisical run -- ansible-playbook -i ansible/inventory/tailscale.yml ansible/playbooks/k3s-bootstrap.yml

# K3s ローリングアップデート
infisical run -- ansible-playbook -i ansible/inventory/tailscale.yml ansible/playbooks/k3s-bootstrap.yml \
  -e "k3s_version=v1.33.0+k3s1"
```

### Infisical で管理するシークレット一覧
```
# Terraform プロバイダー認証
HCLOUD_TOKEN
CLOUDFLARE_API_TOKEN
TAILSCALE_OAUTH_CLIENT_ID / TAILSCALE_OAUTH_CLIENT_SECRET

# Terraform 変数 (TF_VAR_ prefix)
TF_VAR_k3s_token / TF_VAR_tailscale_api_key
TF_VAR_authentik_cf_client_id / TF_VAR_authentik_cf_client_secret

# Ansible ブートストラップ
K3S_TOKEN
CLOUDFLARE_TUNNEL_TOKEN / CLOUDFLARE_TUNNEL_ID
INFISICAL_CLIENT_ID / INFISICAL_CLIENT_SECRET
ARGOCD_GITHUB_DEPLOY_KEY

# Raspberry Pi 復旧スクリプト
TFC_API_TOKEN / TFC_WORKSPACE_ID
TAILSCALE_API_KEY / TAILSCALE_TAILNET
```

---
_Document standards and patterns, not every dependency_
