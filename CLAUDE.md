# 🔧 CLAUDE.md — infra

# CLAUDE.md — infra

## プロジェクト概要

荒牧祭実行委員会の情報基盤インフラを管理するリポジトリ。 Terraform で Hetzner Cloud・Cloudflare・Tailscale を管理し、Ansible で K3s をブートストラップする。

## ディレクトリ構成

```
infra/
├── terraform/
│   ├── providers.tf          Terraform provider 設定 (hcloud, cloudflare, tailscale, authentik)
│   ├── main.tf               ノード定義 (Hetzner)
│   ├── firewall.tf           Hetzner FW ルール
│   ├── network.tf            Hetzner プライベートネットワーク
│   ├── tailscale.tf          Tailscale auth key 発行
│   ├── storage.tf            Hetzner Object Storage バケット
│   ├── dns.tf                Cloudflare DNS レコード
│   ├── tunnel.tf             Cloudflare Tunnel 設定
│   ├── access.tf             Cloudflare Access (staging 保護 + Authentik OIDC IdP)
│   ├── authentik_main.tf     Authentik provider 基本設定
│   ├── authentik_apps.tf     Authentik Applications / Providers (OIDC・LDAP・Discord)
│   ├── authentik_ldap.tf     LDAP Outpost 設定
│   ├── authentik_discord.tf  Discord OAuth2 連携 (Discord ロール同期)
│   ├── authentik_policies.tf Authentik ポリシー定義
│   ├── authentik_imports.tf  既存リソースのインポート定義
│   ├── authentik_recovery.tf Authentik リカバリー設定
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
infisical run --env=prod --projectId=$INFISICAL_PROJECT_ID -- \
  terraform plan -var-file="secrets.tfvars"

# 適用 (ノード作成 → Ansible 自動実行)
infisical run --env=prod --projectId=$INFISICAL_PROJECT_ID -- \
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
  ARGOCD_GITHUB_DEPLOY_KEY            GitHub Deploy Key 秘密鍵 (ArgoCD がプライベートリポジトリを読む用)
                                      Infisical の ARGOCD_GITHUB_DEPLOY_KEY に保存。
                                      GitHub → Settings → Deploy keys で生成・登録すること。
```

アプリシークレット (Directus / Authentik / mailserver のパスワード等) は `scripts/push-secrets-to-infisical.sh` で Infisical に登録する。
ESO がクラスター内で Infisical から自動取得するため、Ansible や Terraform には渡さない。

## ブートストラップフロー

```
terraform apply
  └── 1. Hetzner ノード作成 (cloud-init で Tailscale 自動インストール)
      2. Tailscale が tailnet に接続するまで待機

↓ ノード確認後、手動で Ansible を実行

ansible-playbook k3s-bootstrap.yml
  └── Play 1: prod-node-1 に K3s (--cluster-init) をインストール (シングルノード)
      Play 2: k3s_server_worker (HA 復旧時のみ対象あり / 現在はスキップ)
      Play 3: Cilium CNI をインストール (flannel-backend: none のため必須)
              → ノードが Ready になるまで待機
      Play 4: cloudflared を事前インストール (ArgoCD への外部アクセス経路を確保)
              gitops/manifests/prod/cloudflared/ を直接 kubectl apply
      Play 5: kubeconfig を手元に取得・Tailscale IP に書き換え
      Play 6: ArgoCD をインストール
              → infisical-auth Secret を作成 (ESO を経由しない唯一の Secret)
              → GitHub Deploy Key を ArgoCD Repository Secret として登録
              → App of Apps (root.yaml) を適用
                 └── ESO + ClusterSecretStore (wave: -1) → 全アプリ (wave: 0) の順で sync
```

`null_resource` + `local-exec` による Ansible の自動呼び出しは HCP Terraform のリモート実行環境では動作しないためコメントアウトしてある (`main.tf:82`)。**Terraform 完了後、手動で Ansible を実行すること。**


---

## Ansible: インベントリ (tailscale.yml)

Tailscale の MagicDNS 名または IP アドレスでノードを管理する。SSH は Tailscale 経由のみ (ポート 22 はパブリックに閉じている)。

```yaml
# ansible/inventory/tailscale.yml (シングルノード構成 / HA 復旧時は k3s_server_worker グループを追加)
all:
  children:
    k3s_server:
      hosts:
        prod-node-1:
          ansible_host: prod-node-1   # Tailscale MagicDNS 名 (初回 apply 後に確認)
          k3s_role: server
          k3s_cluster_init: true
          k3s_private_ip: 10.0.1.1
  vars:
    ansible_user: root
    ansible_ssh_extra_args: "-o ConnectTimeout=10"
    k3s_version: v1.32.3+k3s1
    k3s_token: "{{ lookup('env', 'K3S_TOKEN') }}"
```

**グループ設計の考え方:**

* `k3s_server` — etcd クラスタ初期化担当 (cluster-init)。prod-node-1 が etcd + ワークロードを両用
* `k3s_server_worker` — HA 復旧時に prod-node-2/3 を追加するグループ (現在は未使用)


---

## Ansible: Playbook (k3s-bootstrap.yml)

6 つの Play で構成される。実行順に依存関係があるため通しで実行する。

| Play | 対象 | 内容 |
|------|------|------|
| 1 | `k3s_server` (prod-node-1) | K3s `--cluster-init` でインストール。etcd クラスタを初期化 |
| 2 | `k3s_server_worker` | K3s をローリングインストール (`serial: 1`)。**現在はグループが空のためスキップ** (HA 復旧時に有効化) |
| 3 | `k3s_server[0]` | **Cilium CNI** を Helm でインストール (`flannel-backend: none` のため必須)。全ノード Ready 待ち |
| 4 | `k3s_server[0]` | cloudflared を事前インストール。`gitops/manifests/prod/cloudflared/` を直接 kubectl apply |
| 5 | `k3s_server[0]` | kubeconfig を手元に取得し、サーバーアドレスを Tailscale IP に書き換え |
| 6 | `k3s_server[0]` | ArgoCD インストール → `infisical-auth` Secret 作成 → GitHub Deploy Key 登録 → root.yaml 適用 |

**Play 3 (Cilium) の注意点:**
`--flannel-backend: none` で K3s を起動するとノードが `NotReady` のままになる。Cilium がいないと全 Pod が Pending になるため、cloudflared より先にインストールする。

**Play 4 (cloudflared) の注意点:**
マニフェストは `gitops/manifests/prod/cloudflared/` をそのままノードに転送して適用する (Single Source of Truth)。ArgoCD bootstrap 後、gitops 側の cloudflared App が同名リソースを adopt して管理を引き継ぐ。

**Play 6 (ArgoCD) の注意点:**
- `infisical-auth` Secret (clientId / clientSecret) は ESO を経由しない唯一の Secret。`kubectl apply` で直接作成する
- GitHub Deploy Key はプライベートリポジトリへのアクセスに必要。`ARGOCD_GITHUB_DEPLOY_KEY` 環境変数から注入する
- ArgoCD CRD が大きいため `--server-side --force-conflicts` で apply する


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
Ansible が gitops/manifests/prod/cloudflared/ を kubectl apply で起動
  └── ArgoCD bootstrap 後、gitops repo の cloudflared App が sync
        └── 既存リソースに argocd.argoproj.io/managed-by アノテーションが付与され adopt 完了
              以降は ArgoCD UI から管理
```

`gitops/manifests/prod/cloudflared/` が **Single Source of Truth**。Ansible も ArgoCD も同じファイルを参照するため差分は発生しない。以前あった `terraform/templates/cloudflared.yaml.j2` は廃止済み。


---

## ESO (External Secrets Operator) の bootstrap 方針

ESO は ArgoCD の **Sync Wave** `-1` で管理する。他のすべての App より先に sync されることが保証されるため、Ansible での事前インストールは不要。

```
ArgoCD App of Apps (root.yaml) 適用
  └── wave: -1  ESO (Helm Chart)
                ClusterSecretStore (Infisical 接続設定)
      wave: 0   Authentik, Directus, mailserver, cloudflared (adopt), ...
```

**ClusterSecretStore の接続情報 (Infisical) について:**

`ClusterSecretStore` は ESO と同じ wave `-1` に含める。Infisical への接続に必要な認証情報は `infisical-auth` Secret (`clientId` / `clientSecret`) として Ansible の **Play 6** が直接 `kubectl apply` で作成する。この Secret のみ ESO を経由しない。gitops マニフェストには平文で書かず、`INFISICAL_CLIENT_ID` / `INFISICAL_CLIENT_SECRET` 環境変数から注入すること。


---

## K3s クラスタの操作

### ノードの状態確認

```bash
# Tailscale 経由で prod-node-1 に接続して確認
ssh root@prod-node-1

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

シングルノード構成のため再起動中は一時停止する。HA 復旧後は `serial: 1` のローリング更新で無停止が可能になる。

### ノードの強制再プロビジョニング

```bash
# K3s をアンインストールしてから再インストール
ansible -i ansible/inventory/tailscale.yml prod-node-1 \
  -m shell -a "/usr/local/bin/k3s-uninstall.sh"

ansible-playbook -i ansible/inventory/tailscale.yml \
  ansible/playbooks/k3s-bootstrap.yml \
  --limit prod-node-1
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
  webmail.aramakisai.com   Roundcube が Authentik OAuth2 (oauth_login_redirect) で保護
                           CF Access を重ねると二重認証になるため除外
  argocd.aramakisai.com    Authentik OIDC で保護 (argocd-admins グループが role:admin)
                           CF Access は不要 (Authentik → idp.aramakisai.com が CF Access 保護済みのため)
                           OIDC 動作確認後に admin.enabled: "false" にすること (argocd-cm.yaml)
  mail.aramakisai.com         Docker Mailserver (管理 Web UI なし)
                              直接 AAAA レコード / CF Access 不要
```

Authentik 側では `idp.aramakisai.com/application/o/cloudflare/` に OAuth2/OIDC Provider を作成し、Client ID / Secret を Infisical に保存しておく。


---

## 注意事項

* `null_resource` + `local-exec` による Ansible 自動実行は `main.tf:82` でコメントアウト済み。HCP Terraform のリモート実行環境では動作しないため、**Terraform 完了後に手動で Ansible を実行する**
* Hetzner FW でポート 22 は**開放しない** (Tailscale SSH を使用)
* tfstate は Terraform Cloud で管理する (ローカルに置かない)
* Cloudflare Access の IdP 設定 (Authentik の Client ID/Secret) は Infisical から取得し、コードに直接書かない
* cloudflared のマニフェストは `gitops/manifests/prod/cloudflared/` が Single Source of Truth。Ansible も ArgoCD もここを参照する
* `infisical-auth` Secret は唯一 ESO を経由しない Secret であり、管理方法を変えないこと
* **Cilium はブートストラップ時に必須** (`--flannel-backend: none` のため K3s は CNI を持たない。Cilium がないとノードが NotReady のまま)
* **サーバータイプは CX33 (2vCPU/8GB/80GB NVMe)** を使用 (シングルノード構成)
* **Hetzner FW でポート 443 を開放** (メールサーバー直接アクセス用)
  - `mail.aramakisai.com` は proxied=false (直接 AAAA) のため Cloudflare 保護なし / cert-manager TLS で保護
  - Webmail は Cloudflare Tunnel (webmail.aramakisai.com) 経由でアクセス


---

## メールサーバー (Docker Mailserver)

### アーキテクチャ

```
外部 MTA (port 25/587/465/143/993)
  → Hetzner FW → hostNetwork Pod (prod-node-1) → Docker Mailserver (DMS)

Webmail ブラウザアクセス
  → webmail.aramakisai.com → CF Tunnel → Roundcube (Authentik OAuth2) → DMS IMAP (OAUTHBEARER)

管理 UI なし (DMS は Web 管理 UI を持たない)
```

StatefulSet `mailserver` が `prod-node-1` に固定 (`nodeSelector`)。hostNetwork で標準ポートを直接公開。
TLS 証明書は cert-manager (`mail-tls` Certificate) が管理。

### メールデータのバックアップ

VolSync (ReplicationSource) で Backblaze B2 に定期バックアップ。DR 時は `recovery.sh` が自動復元する。

### Infisical に必要なシークレット

| キー名 | 内容 |
|--------|------|
| `MAILSERVER_LDAP_BIND_PASSWORD` | LDAP bind パスワード (DMS が Authentik LDAP で認証) |
| `MAILSERVER_DKIM_KEY` | DKIM 秘密鍵 |
| `MAILSERVER_RESTIC_PASSWORD` | Restic バックアップ暗号化パスワード |
| `B2_APPLICATION_KEY_ID` | Backblaze B2 key ID (VolSync 用) |
| `B2_APPLICATION_KEY` | Backblaze B2 application key |


---

## 監視 (Grafana Alloy + Grafana Cloud)

### アーキテクチャ

```
全ノード (DaemonSet)
  └── Grafana Alloy
        ├── Pod ログ収集 → Grafana Cloud Loki
        └── ノードメトリクス (port 9100) → Grafana Cloud Prometheus
```

Alloy は `gitops/manifests/shared/monitoring/alloy.yaml` で定義済み。
Grafana Cloud をバックエンドとして使うため、クラスター内に Prometheus サーバーは不要。

### 必要なシークレット (Infisical 登録が必要)

| キー名 | 内容 |
|--------|------|
| `LOKI_URL` | Grafana Cloud Loki のプッシュ URL |
| `LOKI_USERNAME` | Loki のユーザー ID (数値) |
| `LOKI_PASSWORD` | Grafana Cloud API キー |
| `PROMETHEUS_REMOTE_WRITE_URL` | Grafana Cloud Prometheus の remote_write URL |
| `PROMETHEUS_USERNAME` | Prometheus のユーザー ID (数値) |
| `PROMETHEUS_PASSWORD` | Grafana Cloud API キー (Loki と同じキーで可) |

Grafana Cloud の接続情報: My Account → Stack → Prometheus / Loki の「Details」から取得。

---

## Roundcube Webmail

Docker Mailserver は webmail を持たないため、Roundcube を別途デプロイ。

```
URL:    https://webmail.aramakisai.com
認証:   Authentik OAuth2 (oauth_login_redirect: true で自動リダイレクト)
        ※ CF Access なし: 二重認証を避けるため
接続先: mailserver.prod.svc.cluster.local:993 (IMAP OAUTHBEARER)
        mailserver.prod.svc.cluster.local:587  (SMTP OAUTHBEARER)
```

### Authentik 側の必須設定 (初回のみ)

1. Applications → Providers → 新規作成
   - Type: OAuth2/OpenID Provider
   - Name: Roundcube
   - Client ID: `aramakisai-mail` (固定 — ConfigMap に直書き済み)
   - Redirect URIs: `https://webmail.aramakisai.com/index.php/login/oauth`
   - Scopes: openid, email, profile, offline_access
2. Applications → 新規作成: slug=`aramakisai-mail`, Provider=上記
3. Infisical に登録:
   - `MAIL_OAUTH2_CLIENT_SECRET` = Authentik で生成されたシークレット
   - `ROUNDCUBE_DES_KEY` = 24文字のランダム文字列 (`openssl rand -base64 18`)

# 🚀 CLAUDE.md — gitops

# CLAUDE.md — gitops

## プロジェクト概要

荒牧祭実行委員会の K3s クラスター上のリソースを GitOps で管理するリポジトリ。 ArgoCD の App of Apps パターンを使用し、このリポジトリの状態がそのままクラスターの状態になる。

**このリポジトリは public で公開されている。** シークレットを manifest に直接書かないこと。

## ディレクトリ構成

```
gitops/
├── root.yaml                ルートアプリ (apps/ を再帰的に監視)
├── apps/                    ArgoCD App of Apps
│   ├── prod/
│   │   ├── eso.yaml                  wave: -1 (ESO Helm chart)
│   │   ├── cluster-secret-store.yaml wave: -1 (ClusterSecretStore / Infisical 接続)
│   │   ├── cloudnativepg.yaml        wave: -1 (CNPG Operator Helm chart)
│   │   ├── cert-manager.yaml         wave: -1 (cert-manager Helm chart)
│   │   ├── kube-state-metrics.yaml   wave: -1
│   │   ├── namespace-config.yaml     wave: -1 (Namespace / LimitRange)
│   │   ├── snapshot-controller.yaml  wave: -1 (VolumeSnapshot API)
│   │   ├── volsync.yaml              wave: -1 (VolSync バックアップ Operator)
│   │   ├── nginx-ingress.yaml        wave: 1  (nginx-ingress Helm chart)
│   │   ├── cert-manager-config.yaml  (ClusterIssuer)
│   │   ├── authentik.yaml
│   │   ├── directus.yaml
│   │   ├── mailserver.yaml           (Docker Mailserver)
│   │   ├── argocd-config.yaml        (ArgoCD OIDC / RBAC 設定)
│   │   ├── autoconfig.yaml           (メールクライアント自動設定エンドポイント)
│   │   ├── monitoring.yaml           (Grafana Alloy)
│   │   ├── roundcube.yaml
│   │   ├── cloudflared.yaml
│   │   └── reloader.yaml             (Stakater Reloader — Secret 変更で自動 rollout)
│   └── staging/
│       └── directus.yaml
├── manifests/
│   ├── prod/
│   │   ├── argocd/          (argocd-cm, argocd-rbac-cm, external-secret)
│   │   ├── authentik/       (db-cluster, external-secret, ldap-outpost, redis)
│   │   ├── autoconfig/      (certificate, configmap, deployment, ingress)
│   │   ├── cert-manager/    (cluster-issuer, external-secret)
│   │   ├── cloudflared/     (deployment, namespace)
│   │   ├── directus/        (db-cluster, deployment, external-secret, namespace, service)
│   │   ├── mailserver/      (configmap, dkim-external-secret, dovecot-oauth2-external-secret,
│   │   │                     external-secret, pvc, replication-source, restic-external-secret,
│   │   │                     service, statefulset)
│   │   ├── namespace-config/ (namespace, limit-range)
│   │   └── roundcube/       Webmail (config-configmap, deployment, external-secret, pvc, service)
│   ├── staging/
│   │   └── directus/
│   └── shared/
│       ├── eso/             (cluster-secret-store, alloy-external-secret, b2-external-secret)
│       └── monitoring/      (alloy.yaml, alloy-cluster.yaml)
└── helm-values/
    └── prod/
        └── authentik.yaml
```

**CloudNativePG はマニフェストなし**: CNPG Operator は Helm chart のみで管理 (`cloudnativepg.yaml` 参照)。各サービスの DB Cluster 定義は `manifests/prod/<service>/db-cluster.yaml` に置く。

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
認証: Authentik OIDC (CF Access なし)
      → ログイン画面の「Log in via Authentik」から idp.aramakisai.com で認証
      → argocd-admins グループのメンバーが role:admin、それ以外は role:readonly
      → OIDC 設定: gitops/manifests/prod/argocd/argocd-cm.yaml
      → OIDC 動作確認後: argocd-cm.yaml の admin.enabled を false に変更してコミット
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
