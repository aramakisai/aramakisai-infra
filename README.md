# aramakisai-infra

荒牧祭実行委員会の情報基盤を管理するモノレポ。

Terraform でクラウドリソースを定義し、Ansible で K3s クラスターを初期化、GitOps (ArgoCD) でアプリケーションを継続管理する。

---

## アーキテクチャ概要

```
┌─────────────────────────────────────────────────────┐
│  Hetzner Cloud (fsn1)                               │
│                                                     │
│  cp-node (cx23)      prod-node-1 (cx23)             │
│  prod-node-2 (cx23)                                 │
│                                                     │
│  ├── K3s (etcd HA クラスター, 3ノード全てワークロード有) │
│  ├── Cilium (CNI)                                   │
│  └── cloudflared → Cloudflare Tunnel               │
└──────────────────┬──────────────────────────────────┘
                   │ Tailscale (管理用 SSH / kubectl)
                   │ Cloudflare Tunnel (外部トラフィック)
┌──────────────────▼──────────────────────────────────┐
│  Cloudflare                                         │
│  ├── DNS (aramakisai.com)                           │
│  ├── Tunnel → argocd / idp / stg / api.stg          │
│  └── Access (Authentik OIDC で保護)                  │
└─────────────────────────────────────────────────────┘
```

**ノードへの SSH はすべて Tailscale 経由。パブリックインターネットにポート 22 は開放しない。**

---

## ディレクトリ構成

```
.
├── terraform/          クラウドリソース定義 (Hetzner / Cloudflare / Tailscale)
├── ansible/            K3s クラスター初期化
│   ├── inventory/      Tailscale MagicDNS ベースのホスト定義
│   ├── playbooks/      k3s-bootstrap.yml (ブートストラップ手順)
│   └── roles/          k3s-server / k3s-agent
└── gitops/             ArgoCD が管理するすべてのマニフェスト
    ├── root.yaml        App of Apps エントリーポイント
    ├── apps/            ArgoCD Application 定義
    │   ├── prod/
    │   └── staging/
    ├── manifests/       実際の Kubernetes マニフェスト
    │   ├── prod/
    │   ├── staging/
    │   └── shared/      ESO / Monitoring (全環境共通)
    └── helm-values/     Helm chart のカスタム values
```

---

## デプロイされるサービス

| サービス | Namespace | 用途 |
|---------|-----------|------|
| [Authentik](https://goauthentik.io) | `prod` | Identity Provider (SSO) |
| [Directus](https://directus.io) | `prod` / `staging` | Headless CMS |
| [Stalwart](https://stalw.art) | `prod` | メールサーバー |
| cloudflared | `cloudflared` | Cloudflare Tunnel クライアント |
| [ESO](https://external-secrets.io) | `external-secrets` | Kubernetes ↔ Infisical シークレット同期 |
| [CloudNativePG](https://cloudnative-pg.io) | `cnpg-system` | PostgreSQL Operator |
| [Grafana Alloy](https://grafana.com/oss/alloy/) | `monitoring` | メトリクス・ログ収集 |

---

## はじめる前に

### 必要なツール

```bash
terraform >= 1.9
ansible >= 2.14
kubectl
jq
curl
```

### 必要なアカウント・サービス

- [Hetzner Cloud](https://www.hetzner.com/cloud) — VPS ホスティング
- [Cloudflare](https://cloudflare.com) — DNS / Tunnel / Access
- [Tailscale](https://tailscale.com) — VPN メッシュ (管理用)
- [Infisical](https://infisical.com) — シークレット管理
- [Terraform Cloud](https://app.terraform.io) — tfstate 管理 (無料枠)

---

## 初回セットアップ

### 1. Infisical ログイン

このプロジェクトでは Infisical を Single Source of Truth (SSoT) として使用するため、`.env` や `secrets.tfvars` などのローカルシークレットファイルはすべて無効化されています。

シークレット情報をロードして動作させるには、まず Infisical CLI を使用してログインします。

```bash
# ログイン (ブラウザが開くので認証します)
infisical login

# プロジェクトID等はリポジトリ直下の .infisical.json から自動的に読み込まれます
```

### 2. Terraform Cloud 設定

`terraform/providers.tf` の organization / workspace 名を実際の値に変更:

```hcl
cloud {
  organization = "your-org-name"   # ← 変更
  workspaces {
    name = "aramakisai-infra"
  }
}
```

### 3. Terraform 初期化・適用

`infisical run` を使用して、Infisical に保存されている環境変数（`TF_VAR_*` など）を注入しながら実行します。

```bash
cd terraform
terraform init

# 差分確認
infisical run --env=prod -- terraform plan

# 適用 (ノード作成 → Ansible 自動実行)
infisical run --env=prod -- terraform apply
```

`terraform apply` は以下を自動で実行する:

```
1. Hetzner ノード × 3 作成 (cloud-init で Tailscale 自動インストール)
2. ノードが tailnet に登録されるまで待機 (Tailscale API ポーリング)
3. Ansible で K3s をインストール
4. Ansible で cloudflared を起動 (ArgoCD への外部アクセス経路を確保)
5. Ansible で ArgoCD をインストール・App of Apps (gitops/root.yaml) を適用
   └── ESO (wave: -1) → 全アプリ (wave: 0) の順で自律 sync
```

### 4. MagicDNS ホスト名を更新

初回 apply 後、Tailscale admin console でノードのホスト名を確認し、
`ansible/inventory/tailscale.yml` を実際の値に更新する。

```bash
# 確認方法
tailscale status
# または https://login.tailscale.com/admin/machines
```

---

## Day-2 オペレーション

### K3s バージョンアップ

```bash
ansible-playbook -i ansible/inventory/tailscale.yml \
  ansible/playbooks/k3s-bootstrap.yml \
  -e "k3s_version=v1.33.0+k3s1"
# cp-node → prod-node-1 → prod-node-2 の順でローリング更新
```

### 新しいサービスを追加

1. `gitops/manifests/prod/<service-name>/` にマニフェストを作成
2. `gitops/apps/prod/<service-name>.yaml` に ArgoCD Application を定義
3. シークレットが必要な場合は `external-secret.yaml` を追加
4. PR を出してマージすると ArgoCD が自動で sync

### ノードの強制再プロビジョニング

```bash
ansible -i ansible/inventory/tailscale.yml prod-node-2 \
  -m shell -a "/usr/local/bin/k3s-uninstall.sh"

ansible-playbook -i ansible/inventory/tailscale.yml \
  ansible/playbooks/k3s-bootstrap.yml \
  --limit prod-node-2
```

---

## ArgoCD 管理画面

| 環境 | URL |
|-----|-----|
| ArgoCD | https://argocd.aramakisai.com |
| Authentik IdP | https://idp.aramakisai.com |

認証: Cloudflare Access → Authentik OIDC

### ブレークグラス: Authentik 障害時の ArgoCD アクセス

Authentik が落ちていて argocd.aramakisai.com にアクセスできない場合:

```bash
ssh root@cp-node.tail<hash>.ts.net  # confidential:allow
kubectl port-forward svc/argocd-server -n argocd 8080:443
# → http://localhost:8080 でアクセス (Cloudflare Access を通らない)
```

---

## シークレット管理

**マニフェストにシークレットを直接書かない。** すべて [Infisical](https://infisical.com) + ESO 経由で注入する。

```yaml
# ExternalSecret の書き方
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: my-service-secrets
  namespace: prod
spec:
  secretStoreRef:
    kind: ClusterSecretStore
    name: infisical
  target:
    name: my-service-secrets
  data:
    - secretKey: MY_KEY
      remoteRef:
        key: MY_INFISICAL_KEY
```

唯一の例外: `infisical-auth` Secret は Ansible が直接 kubectl apply で作成する
(ESO 自体の起動に必要なため ESO 経由にできない)。

---

## 注意事項

- `.env`, `.env.app-secrets`, `terraform/secrets.tfvars`, `kubeconfig` などのローカルシークレットファイルはすべて無効化されています。
- シークレットおよび `kubeconfig` は Infisical から取得します（`ansible/kubeconfig` は Git 管理から除外されています）。
- tfstate は Terraform Cloud で管理 (ローカルに置かない)
- ポート 22 は公開しない (Tailscale SSH を使用)
- staging から prod の DB へのアクセス禁止 (別 Namespace / 別 CNPG Cluster)
- 0→1 検証フェーズかつ1人メンテナーのため `main` への直接 push 可 (ブランチ保護ルールは未設定。`.kiro/specs/repo-governance/` は導入検討中の未承認spec)
