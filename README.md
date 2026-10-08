# aramakisai-infra

荒牧祭実行委員会の情報基盤を管理するモノレポ。

Terraform でクラウドリソースを定義し、Ansible で K3s クラスターを初期化、GitOps (ArgoCD) でアプリケーションを継続管理する。

---

## アーキテクチャ概要

```
┌─────────────────────────────────────────────────────┐
│  Hetzner Cloud (fsn1)                               │
│                                                     │
│  prod-node-1 (cx33)                                 │
│                                                     │
│  ├── K3s (シングルノード、コントロールプレーン＋ワークロード)  │
│  ├── Cilium (CNI)                                   │
│  └── cloudflared → Cloudflare Tunnel               │
└──────────────────┬──────────────────────────────────┘
                   │ Tailscale (管理用 SSH / kubectl)
                   │ Cloudflare Tunnel (外部トラフィック)
┌──────────────────▼──────────────────────────────────┐
│  Cloudflare                                         │
│  ├── DNS (aramakisai.com)                           │
│  ├── Tunnel → argocd / idp / stg / api.stg          │
│  └── Access (Zitadel OIDC で保護)                    │
└─────────────────────────────────────────────────────┘
```

**ノードへの SSH はすべて Tailscale 経由。パブリックインターネットにポート 22 は開放しない。**

---

## ディレクトリ構成

```
.
├── docs/               運用手順書 (dr-runbook.md = DR、node-scaling-runbook.md = ノード増減 ほか)
├── terraform/          クラウドリソース定義 (Hetzner / Cloudflare / Tailscale)。tailnet policy の正本は tailscale-acl.hujson.tftpl
├── .github/            ワークフロー (DR・k3s-upgrade・kube-cert-issue 等) と scripts/ (kube-oidc.sh 等)
├── scripts/            運用スクリプト (kube-login.sh = make kube-login の実体、scaletest/ = ノード増減の Hetzner 実機検証ハーネス 等)
├── ansible/            K3s クラスター初期化
│   ├── inventory/      Tailscale MagicDNS ベースのホスト定義
│   ├── playbooks/      k3s-bootstrap.yml (ブートストラップ手順・再実行安全) / scaletest-bootstrap.yml (増減手順の検証専用) / tasks/ (共通タスク)
│   └── roles/          k3s-server / swap (ホスト側の OOM 安全弁)
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
| [Zitadel](https://zitadel.com) | `prod` | Identity Provider (SSO) |
| [Directus](https://directus.io) | `prod` / `staging` | Headless CMS |
| [Docker Mailserver (DMS)](https://docker-mailserver.github.io/docker-mailserver/) | `prod` | メールサーバー |
| cloudflared | `cloudflared` | Cloudflare Tunnel クライアント |
| [ESO](https://external-secrets.io) | `external-secrets` | Kubernetes ↔ Infisical シークレット同期 |
| [CloudNativePG](https://cloudnative-pg.io) | `cnpg-system` | PostgreSQL Operator |
| [Vaultwarden](https://github.com/dani-garcia/vaultwarden) | `prod` | パスワードマネージャー (SSO対応) |
| 運用ポータル・運用ダッシュボード (`ops-dashboard`、`dash.aramakisai.com`) | `ops-dashboard` | 実行委員向けポータル (executive) と管理者向け運用ダッシュボード (admin)。運用は [docs/ops-dashboard-runbook.md](docs/ops-dashboard-runbook.md) |

---

## はじめる前に

### 必要なツール

```bash
terraform >= 1.9
ansible >= 2.14
kubectl
jq
curl
gh >= 2.87.0   # make kube-login が発行ワークフローの run ID 取得に使う
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

# プロジェクトID・既定環境(prod)はコミット済みの .infisical.json (資格情報なし) から読み込まれるため infisical init は不要
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
1. Hetzner シングルノード (prod-node-1) 作成 (cloud-init で Tailscale 自動インストール)
2. ノードが tailnet に登録されるまで待機 (Tailscale API ポーリング)
3. Ansible で swap ファイル（4GB）を作成
4. Ansible で K3s をインストール
5. Ansible で cloudflared を起動 (ArgoCD への外部アクセス経路を確保)
6. Ansible で ArgoCD をインストール・App of Apps (gitops/root.yaml) を適用
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

### 5. Renovate & Dependabot alerts の有効化 (一度きり)

依存関係の追従・脆弱性検知を有効化します(リポジトリ管理者権限が必要)。

1. [Renovate GitHub App](https://github.com/apps/renovate) を本リポジトリへインストール
   - `renovate.json` (リポジトリルート) が Terraform provider・GitHub Actions・GitOps管理下コンテナイメージのバージョン追従PRを自動生成します
   - 生成されたPRは自動マージされません。必ず人手でレビュー・マージしてください
2. GitHub Dependabot alerts を有効化

   ```bash
   gh api -X PUT repos/<owner>/<repo>/vulnerability-alerts
   ```

   または Settings → Code security → "Dependabot alerts" を Enable

---

## Day-2 オペレーション

### K3s バージョンアップ

```bash
infisical run --env=prod -- ansible-playbook -i ansible/inventory/tailscale.yml \
  ansible/playbooks/k3s-bootstrap.yml \
  -e "k3s_version=v1.36.3+k3s1"
# prod-node-1 の K3s を更新
```

### 新しいサービスを追加

1. `gitops/manifests/prod/<service-name>/` にマニフェストを作成
2. `gitops/apps/prod/<service-name>.yaml` に ArgoCD Application を定義
3. シークレットが必要な場合は `external-secret.yaml` を追加
4. PR を出してマージすると ArgoCD が自動で sync

### Zitadel リソースの投入

`zitadel-bootstrap` 系の playbook は、本番では `ZITADEL_EXTERNAL_DOMAIN=idp.aramakisai.com` の指定が必須です (未指定だとインスタンスを解決できません)。`--check` には対応していません (参照 API の応答を前提とする処理が check モードで失敗します)。

```bash
infisical run --env=prod -- env ZITADEL_EXTERNAL_DOMAIN=idp.aramakisai.com \
  ansible-playbook -i ansible/inventory/tailscale.yml ansible/playbooks/zitadel-resources.yml
```

### ノードの強制再プロビジョニング

```bash
infisical run --env=prod -- ansible -i ansible/inventory/tailscale.yml prod-node-1 \
  -m shell -a "/usr/local/bin/k3s-uninstall.sh"

infisical run --env=prod -- ansible-playbook -i ansible/inventory/tailscale.yml \
  ansible/playbooks/k3s-bootstrap.yml
```

### kubectl によるクラスタ操作

kube-apiserver への認証は GitHub Actions OIDC (CI・DR) と、GitHub 経由で発行する短命クライアント証明書 (人) で行い、共有 kubeconfig はありません。

```bash
make kube-login                          # 証明書を発行し、コンテキスト aramakisai-prod を作成・更新 (有効期限 7 日。切れたら再実行)
make kubectl ARGS="get pods -A"          # kubectl --context aramakisai-prod
```

- 前提: `gh` ログイン済み、tailnet 接続済み、リポジトリの write 権限
- server CA が変わった場合 (クラスタ再作成など) は指紋を表示して止まります。正当と確認できたときだけ `make kube-login ARGS=--accept-new-ca` を使います
- 権限は `gitops/manifests/prod/kube-access/` の RBAC binding で付与・剥奪します (PR をマージすると ArgoCD が同期)。証明書は失効できないため、即時の剥奪は binding の削除で行います
- GitHub 障害時は発行・CI・DR が止まります。発行済みの証明書は有効期限まで使えます。詳細は [CLAUDE.md](CLAUDE.md) を参照

---

### Tailscale ACL (tailnet policy)

正本は `terraform/tailscale-acl.hujson.tftpl` で、`tailscale_acl.this` が適用する。Admin console では編集しない。

- 変更手順: tftpl を編集して PR → マージ後に `infisical run --env=prod -- terraform apply -target=tailscale_acl.this`。ファイル内の `tests` は適用時 (保存時) に検証され、満たさない policy は拒否される。
- tagOwners の個人アカウントは sensitive 変数 `tailscale_acl_owner_email` (Infisical の `TF_VAR_tailscale_acl_owner_email`) で注入する。リポジトリには書かない。
- grants は許可リスト方式で、全許可は置かない。人の端末 (`autogroup:member`) から `tag:k3s-node`・自分の端末・`tag:scaletest` へは全ポート、`tag:ci` から `tag:k3s-node` へは tcp 22・6443・10250 のみ許可する。`tag:k3s-node` と `tag:scaletest` 発の許可はない。
- 新たな tailnet 経路を足すときは、grants と `tests` の両方を更新する。

---

## ArgoCD 管理画面

| 環境 | URL |
|-----|-----|
| ArgoCD | https://argocd.aramakisai.com |
| Zitadel IdP | https://idp.aramakisai.com |

認証: Cloudflare Access → Zitadel OIDC

### ブレークグラス: IdP 障害時の ArgoCD アクセス

Zitadel が落ちていて argocd.aramakisai.com にアクセスできない場合:

```bash
ssh root@prod-node-1.tail<hash>.ts.net  # confidential:allow
make kubectl ARGS="port-forward svc/argocd-server -n argocd 8080:443"
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

- `.env`, `.env.app-secrets`, `terraform/secrets.tfvars` などのローカルシークレットファイルはすべて無効化されています。
- シークレットは Infisical から取得します。kubectl の認証情報は Infisical に置かず、`make kube-login` で発行した短命証明書を使います。
- tfstate は Terraform Cloud で管理 (ローカルに置かない)
- ポート 22 は公開しない (Tailscale SSH を使用)
- staging から prod の DB へのアクセス禁止 (別 Namespace / 別 CNPG Cluster)
- 0→1 検証フェーズかつ1人メンテナーのため `main` への直接 push 可 (ブランチ保護ルールは未設定。`.kiro/specs/repo-governance/` は導入検討中の未承認spec)
