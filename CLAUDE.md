# 🔧 CLAUDE.md — infra

# CLAUDE.md — infra

## プロジェクト概要

荒牧祭実行委員会の情報基盤インフラを管理するリポジトリ。 Terraform で Hetzner Cloud・Cloudflare・Tailscale を管理し、Ansible で K3s をブートストラップする。

## ディレクトリ構成

```
infra/
├── terraform/
│   ├── main.tf        ノード定義 (Hetzner)
│   ├── firewall.tf    Hetzner FW ルール
│   ├── network.tf     Hetzner プライベートネットワーク
│   ├── tailscale.tf   Tailscale auth key 発行
│   ├── storage.tf     Hetzner Object Storage バケット
│   ├── dns.tf         Cloudflare DNS レコード
│   ├── tunnel.tf      Cloudflare Tunnel 設定
│   ├── access.tf      Cloudflare Access (staging 保護 + Authentik OIDC IdP)
│   ├── variables.tf
│   └── outputs.tf
└── ansible/
    ├── inventory/
    │   └── tailscale.yml    Tailscale IP ベースのインベントリ
    ├── playbooks/
    │   └── k3s-bootstrap.yml
    └── roles/
        ├── k3s-server/
        └── k3s-agent/
```

## 使用プロバイダー

| プロバイダー | 用途  |
|--------|-----|
| `hetznercloud/hcloud` | VPS・FW・ネットワーク・Object Storage |
| `tailscale/tailscale` | auth key 発行・デバイス管理 |
| `cloudflare/cloudflare` | DNS・Tunnel・Access・Pages |

## コマンド

```bash
# 初期化
cd terraform
terraform init

# 差分確認
terraform plan -var-file="secrets.tfvars"

# 適用 (ノード作成 → Ansible 自動実行)
terraform apply -var-file="secrets.tfvars"
```

```bash
# Ansible 単体実行 (再プロビジョニング)
ansible-playbook -i ansible/inventory/tailscale.yml \
  ansible/playbooks/k3s-bootstrap.yml
```

## 変数・シークレット管理

* `secrets.tfvars` はコミット**禁止** (`.gitignore` 済み)
* シークレットは環境変数または Infisical から取得
* Tailscale auth key は 1 時間で失効するため terraform apply のたびに再発行される

```
必須環境変数:
  HCLOUD_TOKEN
  CLOUDFLARE_API_TOKEN
  TAILSCALE_OAUTH_CLIENT_ID
  TAILSCALE_OAUTH_CLIENT_SECRET
  TF_VAR_authentik_cf_client_id      Authentik OIDC Client ID
  TF_VAR_authentik_cf_client_secret  Authentik OIDC Client Secret
```

## ブートストラップフロー

```
terraform apply
  └── 1. Hetzner ノード作成 (cloud-init で Tailscale 自動インストール)
      2. Tailscale が tailnet に接続するまで待機
      3. Ansible が Tailscale IP 経由で K3s をインストール
      4. ArgoCD を bootstrap
```

## Cloudflare Access (access.tf) について

staging 環境と PR プレビューを Authentik OIDC で保護する。リポジトリが public のため staging URL がコードから発見されうることへの対策。

```
保護対象:
  stg.aramakisai.com       CF Access Application
  api.stg.aramakisai.com   CF Access Application
  *.pages.dev              Cloudflare Pages プロジェクト設定で一括保護
                           (access.tf ではなく Pages ダッシュボードで設定)
```

Authentik 側では `idp.aramakisai.com/application/o/cloudflare/` に OAuth2/OIDC Provider を作成し、Client ID / Secret を Infisical に保存しておく。

## 注意事項

* `null_resource` の Ansible トリガーは冪等ではないため、ノードが存在する状態で再実行する場合は `-target` を使うか Ansible 単体で実行する
* Hetzner FW でポート 22 は**開放しない** (Tailscale SSH を使用)
* tfstate は Terraform Cloud で管理する (ローカルに置かない)
* Cloudflare Access の IdP 設定 (Authentik の Client ID/Secret) は Infisical から取得し、コードに直接書かない
