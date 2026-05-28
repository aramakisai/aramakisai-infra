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
        │   ├── tasks/main.yml
        │   ├── handlers/main.yml
        │   └── templates/
        │       ├── k3s-server.service.j2
        │       └── config.yaml.j2
        └── k3s-agent/
            ├── tasks/main.yml
            ├── handlers/main.yml
            └── templates/
                └── k3s-agent.service.j2
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
# Ansible 単体実行 (再プロビジョニング・設定変更時)
ansible-playbook -i ansible/inventory/tailscale.yml \
  ansible/playbooks/k3s-bootstrap.yml

# 特定ノードのみ対象にする場合
ansible-playbook -i ansible/inventory/tailscale.yml \
  ansible/playbooks/k3s-bootstrap.yml \
  --limit prod-node-2

# K3s バージョンだけ変更したい場合
ansible-playbook -i ansible/inventory/tailscale.yml \
  ansible/playbooks/k3s-bootstrap.yml \
  -e "k3s_version=v1.32.3+k3s1"
```

## 変数・シークレット管理

* `secrets.tfvars` はコミット**禁止** (`.gitignore` 済み)
* シークレットは環境変数または Infisical から取得
* Tailscale auth key は 1 時間で失効するため terraform apply のたびに再発行される

```
必須環境変数 (すべて .env で管理 / source .env で読み込む):

  # Terraform プロバイダー認証
  HCLOUD_TOKEN                        Hetzner Cloud API トークン
  CLOUDFLARE_API_TOKEN                Cloudflare API トークン (Zone:DNS:Edit, Tunnel:Edit, Access:Edit)
  TAILSCALE_OAUTH_CLIENT_ID           Tailscale OAuth クライアント ID
  TAILSCALE_OAUTH_CLIENT_SECRET       Tailscale OAuth クライアントシークレット

  # Terraform 変数 (TF_VAR_ prefix で自動認識)
  TF_VAR_k3s_token                    K3s クラスタ参加トークン (K3S_TOKEN と同値)
  TF_VAR_tailscale_api_key            Tailscale HTTP API キー (ノード登録ポーリング用)
  TF_VAR_authentik_cf_client_id       Authentik OIDC Client ID (Cloudflare Access IdP 登録用)
  TF_VAR_authentik_cf_client_secret   Authentik OIDC Client Secret

  # Ansible 用 (ansible-playbook を直接実行するときも必要)
  K3S_TOKEN                           K3s クラスタ参加トークン (TF_VAR_k3s_token と同値)
  CLOUDFLARE_TUNNEL_TOKEN             Cloudflare Tunnel トークン (terraform output -raw tunnel_token)
  CLOUDFLARE_TUNNEL_ID                Cloudflare Tunnel ID (terraform output tunnel_id)
  INFISICAL_CLIENT_ID                 Infisical Machine Identity Client ID (ESO 用 / Read 権限のみ)
  INFISICAL_CLIENT_SECRET             Infisical Machine Identity Client Secret
  INFISICAL_PROJECT_ID                Infisical プロジェクト ID
```

アプリシークレット (Stalwart / Directus / Authentik のパスワード等) は `.env.app-secrets` で管理し、
`scripts/push-secrets-to-infisical.sh` で Infisical に登録する。
ESO がクラスター内で Infisical から自動取得するため、Ansible や Terraform には渡さない。

## ブートストラップフロー

```
terraform apply
  └── 1. Hetzner ノード作成 (cloud-init で Tailscale 自動インストール)
      2. Tailscale が tailnet に接続するまで待機 (local-exec で tsid ポーリング)
      3. Ansible が Tailscale IP 経由で K3s をインストール
      4. Ansible が cloudflared を kubectl apply (外部アクセス経路を確保)
      5. Ansible が ArgoCD をインストール・App of Apps を適用
         └── ESO (wave: -1) → 全アプリ (wave: 0) の順で sync
```

Terraform から Ansible を呼び出す部分は `null_resource` + `local-exec` で実装されており、 ノードの再作成 (taint) 時のみ再実行される。設定変更だけしたい場合は Ansible を直接実行すること。


---

## Ansible: インベントリ (tailscale.yml)

Tailscale の MagicDNS 名または IP アドレスでノードを管理する。SSH は Tailscale 経由のみ (ポート 22 はパブリックに閉じている)。

```yaml
# ansible/inventory/tailscale.yml
all:
  children:
    k3s_server:
      hosts:
        cp-node:
          ansible_host: cp-node.tail<hash>.ts.net
          k3s_role: server
          k3s_cluster_init: true   # etcd クラスタの初期化担当 (最初の1台のみ true)
    k3s_server_worker:
      hosts:
        prod-node-1:
          ansible_host: prod-node-1.tail<hash>.ts.net
          k3s_role: server         # server+agent (etcd 参加 + ワークロード実行)
        prod-node-2:
          ansible_host: prod-node-2.tail<hash>.ts.net
          k3s_role: server
  vars:
    ansible_user: root
    ansible_ssh_private_key_file: ~/.ssh/id_ed25519
    k3s_version: v1.32.3+k3s1
    k3s_token: "{{ lookup('env', 'K3S_TOKEN') }}"  # Infisical から取得
```

**グループ設計の考え方:**

* `k3s_server` — etcd クラスタ初期化担当 (cluster-init)。**ノータントのためワークロードも受け付ける**。cp-node はリソースを無駄にしないよう全ノード同様にワークロードを実行する
* `k3s_server_worker` — etcd 参加 + ワークロード両用。prod-node-1/2 はここ


---

## Ansible: Playbook (k3s-bootstrap.yml)

```yaml
# ansible/playbooks/k3s-bootstrap.yml
- name: K3s server (cluster-init) のセットアップ
  hosts: k3s_server
  become: true
  roles:
    - role: k3s-server
      vars:
        k3s_extra_args: "--cluster-init"

- name: K3s server+worker のセットアップ
  hosts: k3s_server_worker
  become: true
  serial: 1          # ローリングアップデート: 1台ずつ順番に処理
  roles:
    - role: k3s-server

- name: cloudflared の事前インストール
  hosts: k3s_server[0]
  become: true
  tasks:
    - name: cloudflared namespace 作成
      command: kubectl create namespace cloudflared --dry-run=client -o yaml | kubectl apply -f -
    - name: Cloudflare Tunnel token を Secret として登録
      command: >
        kubectl create secret generic cloudflared-token
        --from-literal=token={{ lookup('env', 'CLOUDFLARE_TUNNEL_TOKEN') }}
        -n cloudflared
        --dry-run=client -o yaml | kubectl apply -f -
    - name: cloudflared Deployment を適用
      command: kubectl apply -f /tmp/cloudflared.yaml
      # /tmp/cloudflared.yaml は Terraform が output した Tunnel ID を埋めて転送する
    - name: cloudflared が Ready になるまで待機
      command: kubectl rollout status deployment/cloudflared -n cloudflared --timeout=120s

- name: kubeconfig を手元に取得
  hosts: k3s_server[0]
  become: true
  tasks:
    - name: kubeconfig をフェッチ
      fetch:
        src: /etc/rancher/k3s/k3s.yaml
        dest: "{{ playbook_dir }}/../kubeconfig"
        flat: true
    - name: kubeconfig のサーバーアドレスを Tailscale IP に書き換え
      delegate_to: localhost
      replace:
        path: "{{ playbook_dir }}/../kubeconfig"
        regexp: 'https://127\.0\.0\.1:6443'
        replace: "https://{{ ansible_host }}:6443"

- name: ArgoCD の初期 bootstrap
  hosts: k3s_server[0]
  become: true
  tasks:
    - name: ArgoCD namespace 作成
      command: kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -
    - name: ArgoCD インストール
      command: >
        kubectl apply -n argocd
        -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
    - name: ArgoCD が Ready になるまで待機
      command: kubectl rollout status deployment/argocd-server -n argocd --timeout=300s
    - name: App of Apps (root.yaml) 適用
      command: kubectl apply -n argocd -f /tmp/root.yaml
    # root.yaml 適用後は ESO (wave: -1) → 全アプリ (wave: 0) の順で ArgoCD が自律的に sync する
    # cloudflared は既に稼働しているため ArgoCD が同名リソースを adopt する
    # (annotate で argocd.argoproj.io/managed-by: argocd を付与しておくこと → gitops repo 参照)
```


---

## Ansible: k3s-server ロール

### tasks/main.yml の主要タスク

```yaml
# 1. 前提パッケージのインストール
- name: Install prerequisites
  apt:
    name: [curl, open-iscsi, nfs-common]
    state: present
    update_cache: true

# 2. K3s インストールスクリプトの実行
- name: Install K3s
  environment:
    INSTALL_K3S_VERSION: "{{ k3s_version }}"
    K3S_TOKEN: "{{ k3s_token }}"
    # cluster-init ノードの場合は K3S_URL を設定しない
    K3S_URL: "{{ '' if k3s_cluster_init | default(false) else 'https://' + hostvars[groups['k3s_server'][0]]['ansible_host'] + ':6443' }}"
  shell: curl -sfL https://get.k3s.io | sh -s - {{ k3s_extra_args | default('') }}
  args:
    creates: /usr/local/bin/k3s   # 冪等: k3s バイナリが既にあれば skip

# 3. サービス起動確認
- name: Wait for K3s to be ready
  wait_for:
    port: 6443
    host: "{{ ansible_host }}"
    timeout: 120
```

### K3s の主要設定フラグ

| フラグ | 値   | 説明  |
|-----|-----|-----|
| `--flannel-backend` | `none` | Cilium を使うため Flannel 無効 |
| `--disable-network-policy` | —   | Cilium が NetworkPolicy を担当 |
| `--disable` | `traefik` | Traefik は gitops で管理するため無効 |
| `--disable` | `servicelb` | MetalLB / CloudLB を使わないため無効 |
| `--embedded-registry` | —   | Spegel によるノード間イメージキャッシュ |


---

## Ansible: k3s-agent ロール

現状のクラスタ構成は **全ノードが server+worker** のため、このロールは使用していない。 将来的に pure agent ノード (ストレージ特化など) を追加する場合に備えて構造だけ用意してある。


---

## cloudflared の bootstrap 方針

cloudflared は ArgoCD の管理画面への外部アクセス経路そのものであるため、ArgoCD より先に Ansible でインストールする。その後 ArgoCD が GitOps リポジトリの定義を元に同リソースを adopt して以降の管理を引き継ぐ。

```
Ansible が kubectl apply で cloudflared を起動
  └── ArgoCD bootstrap 後、gitops repo の cloudflared App が sync
        └── 既存リソースに argocd.argoproj.io/managed-by アノテーションが付与され adopt 完了
              以降は ArgoCD UI から管理
```

adopt のためのアノテーション付与は gitops リポジトリ側の cloudflared マニフェストに記載すること。 Ansible 側の `/tmp/cloudflared.yaml` (事前インストール用) と gitops 側のマニフェストは**同一の内容**を維持すること。差分があると ArgoCD が apply 時に予期しない変更を検知する。


---

## ESO (External Secrets Operator) の bootstrap 方針

ESO は ArgoCD の **Sync Wave** `-1` で管理する。他のすべての App より先に sync されることが保証されるため、Ansible での事前インストールは不要。

```
ArgoCD App of Apps (root.yaml) 適用
  └── wave: -1  ESO (Helm Chart)
                ClusterSecretStore (Infisical 接続設定)
      wave: 0   Authentik, Directus, Stalwart, cloudflared (adopt), ...
```

**ClusterSecretStore の接続情報 (Infisical) について:**

`ClusterSecretStore` は ESO と同じ wave `-1` に含める。Infisical への接続に必要な Service Account Token は `infisical-auth` Secret として同 wave 内で先に作成する。この Secret のみ ESO を経由せず直接 `kubectl apply` で作成するため、gitops マニフェストに平文で書かず Ansible の vault または Terraform output 経由で渡すこと。


---

## K3s クラスタの操作

### ノードの状態確認

```bash
# Tailscale 経由で cp-node に接続して確認
ssh root@cp-node.tail<hash>.ts.net

kubectl get nodes -o wide
kubectl get pods -A
```

### K3s バージョンアップ

Ansible の `k3s_version` 変数を変更してから `serial: 1` のローリング更新で適用する。

```bash
# 例: v1.32.3 → v1.33.0
ansible-playbook -i ansible/inventory/tailscale.yml \
  ansible/playbooks/k3s-bootstrap.yml \
  -e "k3s_version=v1.33.0+k3s1"
```

cp-node → prod-node-1 → prod-node-2 の順で 1 台ずつ再起動される。etcd クォーラムは常に 2/3 以上を維持するため無停止。

### ノードの強制再プロビジョニング

```bash
# K3s をアンインストールしてから再インストール
ansible -i ansible/inventory/tailscale.yml prod-node-2 \
  -m shell -a "/usr/local/bin/k3s-uninstall.sh"

ansible-playbook -i ansible/inventory/tailscale.yml \
  ansible/playbooks/k3s-bootstrap.yml \
  --limit prod-node-2
```


---

## Cloudflare Access (access.tf) について

staging 環境と PR プレビューを Authentik OIDC で保護する。リポジトリが public のため staging URL がコードから発見されうることへの対策。

```
保護対象:
  stg.aramakisai.com       CF Access Application (Authentik OIDC)
  api.stg.aramakisai.com   CF Access Application (Authentik OIDC)
  *.pages.dev              Cloudflare Pages プロジェクト設定で一括保護
                           (access.tf ではなく Pages ダッシュボードで設定)

非保護 (自前認証あり):
  webmail.aramakisai.com   Snappymail 独自ログイン (IMAP 認証) で保護
                           CF Access を重ねると二重認証になるため除外
  argocd.aramakisai.com    ArgoCD 自前の認証 (admin / Authentik SSO) で保護
                           CF Access は不要なため access.tf に含めない
  mail.aramakisai.com/admin/  Stalwart 管理 UI は kubectl port-forward 経由のみ
                              (外部公開しない / Tailscale + kubectl で十分)
```

Authentik 側では `idp.aramakisai.com/application/o/cloudflare/` に OAuth2/OIDC Provider を作成し、Client ID / Secret を Infisical に保存しておく。


---

## 注意事項

* `null_resource` の Ansible トリガーは冪等ではないため、ノードが存在する状態で再実行する場合は `-target` を使うか Ansible 単体で実行する
* Hetzner FW でポート 22 は**開放しない** (Tailscale SSH を使用)
* tfstate は Terraform Cloud で管理する (ローカルに置かない)
* Cloudflare Access の IdP 設定 (Authentik の Client ID/Secret) は Infisical から取得し、コードに直接書かない
* Ansible 用の `/tmp/cloudflared.yaml` と gitops 側のマニフェストは常に同期して差分を出さないこと
* `infisical-auth` Secret は唯一 ESO を経由しない Secret であり、管理方法を変えないこと
* **サーバータイプは CX23 (2vCPU/4GB/40GB NVMe, $4.99/月)** を使用。CX22 は在庫なしのため移行
* **cp-node に no-schedule taint は設定しない**。全3ノードがワークロードを受け付ける
* **Hetzner FW でポート 443 を開放** (可用性重視)
  - Stalwart admin UI: `https://mail.aramakisai.com/admin/` (直接アクセス可)
  - Webmail は Cloudflare Tunnel (webmail.aramakisai.com) 経由でアクセス
  - `mail.aramakisai.com` は proxied=false (直接 AAAA) のため Cloudflare 保護なし / Stalwart の TLS で保護


---

## Stalwart メールサーバー

### アーキテクチャ

```
外部 MTA (port 25/587/465/143/993)
  → Hetzner FW → hostNetwork Pod (prod-node-1) → Stalwart

Webmail ブラウザアクセス
  → webmail.aramakisai.com → CF Tunnel → Snappymail (独自ログイン) → Stalwart IMAP

管理 UI
  → https://mail.aramakisai.com/admin/ (port 443 直接 / Stalwart TLS)

Webmail ブラウザアクセス (代替)
  → webmail.aramakisai.com → CF Tunnel → Snappymail (独自ログイン) → Stalwart IMAP
```

**port 443 は開放。** 可用性重視。Stalwart が Let's Encrypt (ACME DNS-01) で TLS 証明書を自動取得・更新する。
JMAP クライアントも `https://mail.aramakisai.com` で直接利用可能。

### stalwart-cli

```bash
# stalwart-cli 取得
wget -qO- https://github.com/stalwartlabs/cli/releases/download/v1.0.7/stalwart-cli-x86_64-unknown-linux-musl.tar.xz \
  | tar -xJf - -C /tmp/ && chmod +x /tmp/stalwart-cli

ADMIN_PASS=$(kubectl get secret stalwart-secrets -n prod \
  -o jsonpath='{.data.STALWART_ADMIN_SECRET}' | base64 -d)

/tmp/stalwart-cli --url https://mail.aramakisai.com \
  --user admin --password "$ADMIN_PASS" \
  query Domain
```

### settings.ndjson の適用 (再セットアップ時)

`gitops/manifests/prod/stalwart/settings-configmap.yaml` の NDJSON を適用する際は
全設定を destroy → create する一括プランを使うこと。
部分適用では `#lets-encrypt` 等のプラン内参照が解決できずエラーになる。

```bash
kubectl get configmap stalwart-settings -n prod \
  -o jsonpath='{.data.settings\.ndjson}' > /tmp/settings.ndjson

/tmp/stalwart-cli --url https://mail.aramakisai.com \
  --user admin --password "$ADMIN_PASS" \
  apply --file /tmp/settings.ndjson
```

### stalwart-cli NDJSON フォーマットの注意点 (v0.16)

stalwart-cli が期待する型と JSON の対応:

| 型 | 正しい形式 | 誤り (エラーになる) |
|----|-----------|-------------------|
| `set<T>` | `{"value": true}` | `["value"]` (配列) |
| `list<T>` | `{"0": value}` | `[value]` (配列) |
| AcmeProvider.contact | `{"email@example.com": true}` | `["mailto:email@example.com"]` |
| NetworkListener.bind | `{"[::]:25": true}` | `["[::]:25"]` |
| DkimManagement.algorithms | `{"Dkim1Ed25519Sha256": true}` | — |
| DnsManagement.publishRecords | `{"dkim": true}` | — |
| MtaOutboundStrategy.route.match | `{"0": {"if": "...", "then": "..."}}` | `[{"if": ...}]` |
| Account.credentials | `{"0": {"@type": "Password", "secret": "..."}}` | — |

その他:
- `DkimManagement.rotateAfter` は存在しないフィールド (削除すること)
- `NetworkListener.useTls` は存在しない (`tlsImplicit` のみ使用)
- `NetworkListener` と `MtaRoute` の作成時は `name` フィールドが必須


---

## Snappymail Webmail

Stalwart は webmail を持たないため、Snappymail を別途デプロイ。

```
URL:    https://webmail.aramakisai.com
認証:   Snappymail 独自ログイン (メールアドレス + パスワード)
        ※ CF Access なし: 二重認証を避けるため
接続先: stalwart-mail.prod.svc.cluster.local:993 (IMAP) / :587 (SMTP)
```

### 初期セットアップ (初回デプロイ後)

```bash
kubectl port-forward -n prod deploy/snappymail 8888:8888

SNAP_ADMIN=$(kubectl get secret snappymail-secrets -n prod \
  -o jsonpath='{.data.SNAPPYMAIL_ADMIN_PASSWORD}' | base64 -d)

# ブラウザで http://localhost:8888/?admin を開く
# パスワード: $SNAP_ADMIN
```

管理画面で Domains → "+" から aramakisai.com を追加:
- IMAP: `stalwart-mail.prod.svc.cluster.local` port `993` SSL (cert verify 無効)
- SMTP: `stalwart-mail.prod.svc.cluster.local` port `587` STARTTLS

# 🚀 CLAUDE.md — gitops

# CLAUDE.md — gitops

## プロジェクト概要

荒牧祭実行委員会の K3s クラスター上のリソースを GitOps で管理するリポジトリ。 ArgoCD の App of Apps パターンを使用し、このリポジトリの状態がそのままクラスターの状態になる。

**このリポジトリは public で公開されている。** シークレットを manifest に直接書かないこと。

## ディレクトリ構成

```
gitops/
├── apps/                    ArgoCD App of Apps
│   ├── root.yaml            ルートアプリ (他の App を管理)
│   ├── prod/
│   │   ├── authentik.yaml
│   │   ├── directus.yaml
│   │   ├── stalwart.yaml
│   │   ├── snappymail.yaml
│   │   ├── cloudflared.yaml
│   │   └── cloudnativepg.yaml
│   └── staging/
│       └── directus.yaml
├── manifests/
│   ├── prod/
│   │   ├── authentik/
│   │   ├── directus/
│   │   ├── stalwart/
│   │   ├── snappymail/      Webmail (Snappymail)
│   │   ├── cloudflared/
│   │   └── cloudnativepg/
│   ├── staging/
│   │   └── directus/
│   └── shared/
│       ├── eso/             ExternalSecret 定義
│       └── monitoring/      Grafana Alloy
└── helm-values/
    ├── prod/
    └── staging/
```

## ArgoCD の仕組み

```
root.yaml を ArgoCD に登録するだけで、以下が自動で行われる:
  root.yaml → apps/prod/*.yaml と apps/staging/*.yaml を監視
            → 各 App が manifests/ 以下を監視
            → Git の変更が自動でクラスターに反映
```

## ArgoCD 管理画面

```
URL:  https://argocd.aramakisai.com
認証: Cloudflare Access + Authentik OIDC
      → idp.aramakisai.com でログインするとアクセス可能
      → 委員会メンバー全員がブラウザからアクセスできる (Tailscale 不要)
```

sync 状況の確認・手動 sync・ロールバックはここから行う。

## 新しいサービスを追加する手順


1. `manifests/prod/<service-name>/` にマニフェストを作成
2. `apps/prod/<service-name>.yaml` に ArgoCD Application を定義
3. シークレットが必要な場合は `manifests/prod/<service-name>/external-secret.yaml` を作成
4. PR を出してマージすると ArgoCD が自動で sync する

## シークレット管理 (ESO + Infisical)

マニフェストにシークレットを**直接書かない**。すべて ExternalSecret 経由で Infisical から取得する。

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: directus-secrets
  namespace: prod
spec:
  secretStoreRef:
    name: infisical
  target:
    name: directus-secrets
  data:
  - secretKey: DB_PASSWORD
    remoteRef:
      key: DIRECTUS_DB_PASSWORD
```

## Namespace 規約

| Namespace | 用途  |
|-----------|-----|
| `prod`    | 本番サービス |
| `staging` | ステージングサービス |
| `argocd`  | ArgoCD 本体 |
| `external-secrets` | ESO |
| `monitoring` | Grafana Alloy |

staging から prod へのアクセスは**禁止** (別 DB を使用すること)。

## デプロイフロー

```
変更を加えて PR を作成
  → レビュー → main にマージ
  → ArgoCD が自動で sync (通常 3 分以内)
  → https://argocd.aramakisai.com で sync 状況を確認
```

## 緊急時の手動 sync

```bash
# ArgoCD 管理画面から操作するのが最も簡単
# https://argocd.aramakisai.com

# CLI で強制 sync (Tailscale 接続 + argocd CLI 必要)
argocd app sync <app-name>

# kubectl で直接適用 (ArgoCD をバイパスするため原則禁止)
kubectl apply -f manifests/prod/<service>/
```

## 注意事項

* **シークレットを直接 manifest に書かない** (ExternalSecret を使う)
* `main` への直接 push は禁止 (ブランチ保護ルール)
* Helm values を変更した場合は必ず staging で動作確認してから prod に適用
* CloudNativePG のバージョンアップは DB migration を伴う可能性があるため単独 PR で行う
* シークレット・内部トークンをコードや PR コメント・Issue に貼らない


# Agentic SDLC and Spec-Driven Development

Kiro-style Spec-Driven Development on an agentic SDLC

## Project Context

### Paths
- Steering: `.kiro/steering/`
- Specs: `.kiro/specs/`

### Steering vs Specification

**Steering** (`.kiro/steering/`) - Guide AI with project-wide rules and context
**Specs** (`.kiro/specs/`) - Formalize development process for individual features

### Active Specifications
- Check `.kiro/specs/` for active specifications
- Use `/kiro:spec-status [feature-name]` to check progress

## Development Guidelines
- Think in English, generate responses in Japanese. All Markdown content written to project files (e.g., requirements.md, design.md, tasks.md, research.md, validation reports) MUST be written in the target language configured for this specification (see spec.json.language).

## Minimal Workflow
- Phase 0 (optional): `/kiro:steering`, `/kiro:steering-custom`
- Phase 1 (Specification):
  - `/kiro:spec-init "description"`
  - `/kiro:spec-requirements {feature}`
  - `/kiro:validate-gap {feature}` (optional: for existing codebase)
  - `/kiro:spec-design {feature} [-y]`
  - `/kiro:validate-design {feature}` (optional: design review)
  - `/kiro:spec-tasks {feature} [-y]`
- Phase 2 (Implementation): `/kiro:spec-impl {feature} [tasks]`
  - `/kiro:validate-impl {feature}` (optional: after implementation)
- Progress check: `/kiro:spec-status {feature}` (use anytime)

## Development Rules
- 3-phase approval workflow: Requirements → Design → Tasks → Implementation
- Human review required each phase; use `-y` only for intentional fast-track
- Keep steering current and verify alignment with `/kiro:spec-status`
- Follow the user's instructions precisely, and within that scope act autonomously: gather the necessary context and complete the requested work end-to-end in this run, asking questions only when essential information is missing or the instructions are critically ambiguous.

## Steering Configuration
- Load entire `.kiro/steering/` as project memory
- Default files: `product.md`, `tech.md`, `structure.md`
- Custom files are supported (managed via `/kiro:steering-custom`)
