# Technology Stack

## Architecture

3 層構成: **Terraform (IaC)** → **Ansible (構成管理)** → **ArgoCD GitOps (アプリ管理)**

```
terraform apply
  ├── Hetzner ノード × 3 + Cloudflare DNS/Tunnel/Access + Tailscale auth key
  └── null_resource → Ansible → K3s HA + cloudflared + ArgoCD → App of Apps
```

ノードへの SSH は Tailscale 経由のみ。パブリックポート 22 は開放しない。

## Core Technologies

- **IaC**: Terraform >= 1.9、tfstate は Terraform Cloud で管理
- **構成管理**: Ansible >= 2.14
- **Kubernetes**: K3s v1.32.3+k3s1 (3ノード HA etcd)
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
1. cloudflared を Ansible で事前インストール (ArgoCD の外部アクセス経路確保)
2. ArgoCD インストール → App of Apps 適用
3. ESO が `sync-wave: "-1"` で先行 sync → 他アプリは `wave: 0`

### K3s クラスター設計
- 全ノード (cp-node, prod-node-1, prod-node-2) が etcd + ワークロードを担う (no-schedule taint なし)
- サーバータイプ: CX23 (2vCPU/4GB/40GB NVMe)
- `--disable traefik,servicelb`: どちらも GitOps または Cloudflare Tunnel で代替
- `--embedded-registry`: Spegel によるノード間イメージキャッシュ

### Ansible トリガー
- Terraform の `null_resource` + `local-exec` でノード再作成時のみ実行
- 設定変更だけの場合は Ansible を直接実行する (`null_resource` はトリガーされない)

## Development Environment

### Required Tools
```
terraform >= 1.9
ansible >= 2.14
kubectl
tailscale (管理用SSH接続)
jq, curl
```

### Common Commands
```bash
# IaC 差分確認
cd terraform && terraform plan -var-file="../secrets.tfvars"

# IaC 適用
cd terraform && terraform apply -var-file="../secrets.tfvars"

# Ansible 単体再実行
ansible-playbook -i ansible/inventory/tailscale.yml ansible/playbooks/k3s-bootstrap.yml

# K3s ローリングアップデート
ansible-playbook -i ansible/inventory/tailscale.yml ansible/playbooks/k3s-bootstrap.yml \
  -e "k3s_version=v1.33.0+k3s1"
```

### 必須環境変数
```
HCLOUD_TOKEN
CLOUDFLARE_API_TOKEN
TAILSCALE_OAUTH_CLIENT_ID / TAILSCALE_OAUTH_CLIENT_SECRET
TF_VAR_authentik_cf_client_id / TF_VAR_authentik_cf_client_secret
```

---
_Document standards and patterns, not every dependency_
