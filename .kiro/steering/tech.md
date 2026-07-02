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
| authentik | goauthentik/authentik | >= 2024.12.0 |
| uptimerobot | uptimerobot/uptimerobot | ~> 1.8 |
| healthchecksio | kristofferahl/healthchecksio | ~> 1.6 |
| netdata | netdata/netdata | ~> 0.4 |

## Key Technical Decisions

### シークレット管理
- **ルール**: マニフェストに平文シークレットを書かない
- **方法**: `ExternalSecret` リソースで Infisical から取得
- **唯一の例外**: `infisical-auth` Secret のみ Ansible が直接 `kubectl apply` (ESO 自体の起動に必要なため)
- **既知問題への対処**: ArgoCD が「sync成功」と報告しても `ExternalSecret` の `spec.data` 配列に新規追加した要素が実際にはクラスター側で反映されない場合がある（directus/vaultwarden で複数回再発、原因未解明）。全 `ExternalSecret` に `metadata.annotations: {argocd.argoproj.io/sync-options: Replace=true}` を付与済みだが、**これ単体では直らない**（ArgoCDの差分検出自体が新規配列要素を見逃し、selfHealが同期処理を一切起動しないケースがあるため）。`spec.data` を追加・変更した場合は必ず push 後に `kubectl get externalsecret <name> -n <ns> -o jsonpath='{.metadata.generation}'` 等で実反映を確認し、未反映なら Application オブジェクトへ直接 operation patch で強制sync（[[project_argocd_stale_apply_externalsecret]] 参照）。

### ブートストラップ順序
1. K3s インストール (prod-node-1: `--cluster-init`、シングルノード)
2. **Cilium CNI** を Helm でインストール (`--flannel-backend: none` のため必須。ないと全ノード NotReady)
3. cloudflared を `gitops/manifests/prod/cloudflared/` から直接 kubectl apply (ArgoCD への外部アクセス経路確保)
4. ArgoCD インストール → `infisical-auth` Secret 作成 → GitHub Deploy Key 登録 → App of Apps 適用
   - ArgoCD v3.4.4 以降の configmap informer ラベル要件に対応するため、`argocd-cm` および `argocd-rbac-cm` に `app.kubernetes.io/name` と `app.kubernetes.io/part-of` ラベルを明示的に付与する。
5. ESO が `sync-wave: "-1"` で先行 sync → 他アプリは `wave: 0`

### K3s クラスター設計
- シングルノード (prod-node-1) が etcd + ワークロードを担う。CX33 (2vCPU/8GB/80GB NVMe) 使用。
- **主要フラグ**: `--flannel-backend none` (Cilium用), `--disable-network-policy` (Ciliumが担当), `--disable traefik,servicelb` (GitOps/Tunnel代替), `--embedded-registry` (Spegel)
- **Swap設定**: 全ノード共通で 4GB swap を Ansible（swap ロール）で作成。kubelet `fail-swap-on=false` を設定（ホスト側プロセスの OOM 安全弁）。Pod cgroup には swap を割り当てない「NoSwap」挙動を維持し、K8s 資源モデルの予測可能性を保つ。
- **障害復旧**: 障害時は自動復旧ワークフロー（dr-trigger/dr-recovery）により無人復旧する。詳細は [dr.md](dr.md) 参照。

### Terraform の出力パラメータと外部連携
- **healthchecksio_mailserver_backup_ping_url**: mailserver バックアップの生存確認用。Infisical の `HEALTHCHECKS_MAILSERVER_BACKUP_PING_URL` へ反映。
- **netdata_room_id**: Netdata Cloud aramakisai-prod Room ID。Infisical の `NETDATA_CLAIM_ROOMS` へ反映し、エージェントの Claim に使用。

### Ansible 実行タイミング
- `null_resource` + `local-exec` は HCP Terraform リモート実行非対応のため `main.tf` でコメントアウト済み。
- **Terraform 完了後、常に手動で Ansible を実行する**（設定変更のみの場合も同様）。

## 監視スタック

| コンポーネント | 役割 | 状態 |
|--------------|------|------|
| Grafana Alloy (DaemonSet) | Pod ログ + ノードメトリクス収集 | `shared/monitoring/alloy.yaml` (未デプロイ) |
| Grafana Cloud Loki / Prometheus | ログ/メトリクス保存・アラート (外部) | 接続用シークレット登録後に有効化 |

Alloy が収集したログ/メトリクスを Grafana Cloud にリモート送信する構成。クラスター内に Prometheus サーバーは不要。

### 監視の誤検知除外設定 (Falco カスタムルール)
eBPF ランタイム侵入検知（Falco）において、コントロールプレーン連携やコンテナ固有の正常な動作によるアラート誤検知を回避するため、以下の除外ルール（`gitops/helm-values/prod/falco.yaml`）を適用している：
- **k8s API サーバーアクセス除外**: `argocd`、`authentik`、`cloudnative-pg-operator`、`netdata`、CNPG postgres ポッドのインスタンスマネージャ（`proc.name=manager`、PostgreSQL 16.8）、および `vaultwarden-rbac-sync`（`docker.io/alpine/k8s` イメージ、cronjob=`sync` / trigger receiver=`trigger-receiver`）による API 定常アクセスを除外。※`container.image.repository` はレジストリ接頭辞込み（`docker.io/alpine/k8s`）で一致させる必要があり、接頭辞を欠くと除外が無効化される点に注意。
- **正常な `/etc` 配下書き込み除外**: `docker-mailserver`（起動時の設定生成）、および `cert-manager`（update-ca-certificates）による正常な `/etc` 内ファイルの書き込み・変更処理を除外。
- **stdout/stdin ネットワークリダイレクト除外**: `authentik` server（gunicorn/dumb-init が worker 通信のため `dup3` でソケットへ張り替える正常動作）による `Redirect STDOUT/STDIN to Network Connection in Container` の発報を除外。

## Development Environment

### Required Tools & Rules
- `terraform >= 1.9`, `ansible >= 2.14`, `kubectl`, `tailscale` (SSH接続用), `infisical` (シークレット注入), `uv`
- **Infisical が Single Source of Truth**。`.env` などのローカルファイルは無効化されており、`infisical run --` 経由で環境変数を注入する。

### Common Commands
```bash
# IaC 差分確認 / 適用
infisical run -- terraform -chdir=terraform plan
infisical run -- terraform -chdir=terraform apply

# Ansible 単体実行 / K3s アップデート
infisical run -- ansible-playbook -i ansible/inventory/tailscale.yml ansible/playbooks/k3s-bootstrap.yml
infisical run -- ansible-playbook -i ansible/inventory/tailscale.yml ansible/playbooks/k3s-bootstrap.yml -e "k3s_version=v1.33.0+k3s1"
```

### Infisical で管理するシークレット一覧
- **IaC & 認証**: `HCLOUD_TOKEN`, `CLOUDFLARE_API_TOKEN`, `TAILSCALE_OAUTH_CLIENT_ID`, `TAILSCALE_OAUTH_CLIENT_SECRET`, `TF_VAR_k3s_token`, `TF_VAR_tailscale_api_key`, `TF_VAR_authentik_cf_client_id`, `TF_VAR_authentik_cf_client_secret`
- **Ansible & 復旧**: `K3S_TOKEN`, `CLOUDFLARE_TUNNEL_TOKEN`, `CLOUDFLARE_TUNNEL_ID`, `INFISICAL_CLIENT_ID`, `INFISICAL_CLIENT_SECRET`, `ARGOCD_GITHUB_DEPLOY_KEY`, `TFC_API_TOKEN`, `TFC_WORKSPACE_ID`, `TAILSCALE_API_KEY`, `TAILSCALE_TAILNET`
- **アプリ用シークレット**:
  - **Authentik**: `AUTHENTIK_SECRET_KEY`, `AUTHENTIK_DB_PASSWORD`, `NOREPLY_SMTP_PASSWORD`（`noreply@aramakisai.com` 用 SMTP パスワード。Vaultwarden・Directus の SMTP 設定でも同一キーを再利用） <!-- confidential:allow -->
  - **DMS**: `MAILSERVER_LDAP_BIND_PASSWORD`, `MAILSERVER_DKIM_KEY`, `MAILSERVER_RESTIC_PASSWORD`, `B2_APPLICATION_KEY_ID`, `B2_APPLICATION_KEY`
  - **Directus**: `DIRECTUS_SECRET`, `DIRECTUS_ADMIN_EMAIL`, `DIRECTUS_ADMIN_PASSWORD`, `DIRECTUS_DB_PASSWORD`, `EMAIL_SMTP_PASSWORD`（Authentik の `NOREPLY_SMTP_PASSWORD` を再利用、新規キーなし）。`directus-db` のメモリ制限は、通常稼働時は約60MiBだが、barman-cloud-backup や wal-archive などのバックアップ処理に伴うメモリスパイクで OOM クラッシュループするのを回避するため、制限を `512Mi` に設定。
  - **Alloy**: `LOKI_URL`, `LOKI_USERNAME`, `LOKI_PASSWORD`, `PROMETHEUS_REMOTE_WRITE_URL`, `PROMETHEUS_USERNAME`, `PROMETHEUS_PASSWORD`
  - **Roundcube**: `MAIL_OAUTH2_CLIENT_SECRET`, `ROUNDCUBE_DES_KEY`
  - **Presence Tracker**: `TF_VAR_authentik_room_presence_client_secret` (TF/ESO共用), `PRESENCE_AUTH_SECRET`, `PRESENCE_AUTHENTIK_API_TOKEN`, `PRESENCE_RESET_SECRET`, `PRESENCE_DISCORD_BOT_TOKEN`
  - **Vaultwarden**: `VAULTWARDEN_ADMIN_TOKEN`, `VAULTWARDEN_DB_PASSWORD`, `VAULTWARDEN_ORG_CREATION_USERS`, `VAULTWARDEN_OIDC_CLIENT_ID`, `VAULTWARDEN_OIDC_CLIENT_SECRET`, `VAULTWARDEN_RESTIC_REPOSITORY`, `VAULTWARDEN_RESTIC_PASSWORD`（SMTP は専用キーを持たず、Authentik の `NOREPLY_SMTP_PASSWORD` を再利用）
  - **Directus SSO**: `DIRECTUS_PROD_OIDC_CLIENT_SECRET`（prod 用 Authentik OIDC Client Secret）, `DIRECTUS_STG_OIDC_CLIENT_SECRET`（stg 用）。`DIRECTUS_PROD_OIDC_CLIENT_ID` / `DIRECTUS_STG_OIDC_CLIENT_ID` は `"directus-prod"` / `"directus-stg"` 固定でコードに直書き（変数なし）。
  - **Vaultwarden RBAC Sync**: `VAULTWARDEN_RBAC_SYNC_AUTHENTIK_API_TOKEN`（`PRESENCE_AUTHENTIK_API_TOKEN`と同パターン、`terraform/authentik_vaultwarden_rbac_sync.tf`で発行）, `VAULTWARDEN_RBAC_SYNC_SERVICE_ACCOUNT_CLIENT_ID`, `VAULTWARDEN_RBAC_SYNC_SERVICE_ACCOUNT_CLIENT_SECRET`（Vaultwarden専用サービスアカウントのPersonal API Key、手動ブートストラップ必須）, `TF_VAR_vaultwarden_rbac_sync_trigger_token`（Trigger Receiver共有ベアラートークン）, `DISCORD_OPS_WEBHOOK_URL`（既存キーを再利用、新規作成なし）

### Commit Protection & Coding Standards
- **パス漏洩防止**: pre-commit フック `scripts/check-confidential-info.py` がローカル絶対パスや非許可メールのコミットをブロック。
- **メールアドレス命名規則**:
  - プロジェクト関連のサンプル: `<username>@aramakisai.invalid`
  - 一般外部ドメインのサンプル: `<username>@example.invalid`
  - 実設定で必要な実アドレスは、行末に `# confidential:allow` (MDは `<!-- confidential:allow -->`) を付与。

---
_Document standards and patterns, not every dependency_
