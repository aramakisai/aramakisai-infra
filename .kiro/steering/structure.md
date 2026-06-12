# Project Structure

## Organization Philosophy

**責務別レイヤー分離**: IaC (terraform) → 構成管理 (ansible) → GitOps (gitops) の順で責務が明確に分離されている。
レイヤーをまたいだ変更は依存関係の順序を意識すること (Terraform → Ansible → GitOps の順が bootstrap の基本)。

## Directory Patterns

### Terraform (`terraform/`)
**目的**: クラウドプロバイダーリソースの宣言的定義  
**ファイル粒度**: リソース種別ごとに 1 ファイル  
```
providers.tf         ← Terraform provider 設定 (hcloud, cloudflare, tailscale, authentik)
main.tf              ← ノード (hcloud_server)  ※null_resource はコメントアウト済み
firewall.tf          ← Hetzner ファイアウォールルール
network.tf           ← Hetzner プライベートネットワーク
dns.tf               ← Cloudflare DNS レコード
tunnel.tf            ← Cloudflare Tunnel 設定
access.tf            ← Cloudflare Access (staging 保護 + Authentik OIDC IdP)
tailscale.tf         ← Tailscale auth key 発行
storage.tf           ← Hetzner Object Storage (バケットは手動作成、TF リソースはコメントアウト)
authentik_main.tf    ← Authentik provider 基本設定
authentik_apps.tf    ← Authentik Applications / Providers (OIDC・LDAP・Discord)
authentik_ldap.tf    ← LDAP Outpost 設定
authentik_discord.tf ← Discord OAuth2 連携 (Discord ロール同期)
authentik_policies.tf← Authentik ポリシー定義
authentik_imports.tf ← 既存リソースのインポート定義
authentik_recovery.tf← Authentik リカバリー設定
variables.tf / outputs.tf  ← 変数・出力
```

### Ansible (`ansible/`)
**目的**: K3s クラスターのブートストラップと構成管理  
**構造**: `inventory/` + `playbooks/` + `roles/`  
- インベントリは Tailscale MagicDNS 名を使用 (IP ではなくホスト名)
- ロールは `k3s-server` と `k3s-agent` (現状 agent は未使用)
- K3s 設定フラグは `k3s-server` ロールの `k3s_extra_args` で渡す

### GitOps (`gitops/`)
**目的**: ArgoCD が管理する Kubernetes マニフェスト一式  
**構造**: 3 つの関心で分割

```
apps/          ← ArgoCD Application 定義 (何を管理するかの宣言)
  prod/        ← 本番 Application 一覧
  staging/     ← ステージング Application 一覧
manifests/     ← 実際の Kubernetes リソース
  prod/<svc>/  ← サービスごとにディレクトリ
  staging/<svc>/
  shared/      ← ESO, Monitoring (全環境共通)
helm-values/   ← Helm chart の values ファイル
  prod/ / staging/
root.yaml      ← App of Apps エントリーポイント (apps/ 全体を監視)
```

## サービス追加パターン

新しいサービスを prod に追加する際の標準的なファイル構成:

```
gitops/
  apps/prod/<service>.yaml          ← ArgoCD Application 定義
  manifests/prod/<service>/
    namespace.yaml                  ← Namespace
    deployment.yaml (or statefulset)
    service.yaml
    external-secret.yaml            ← シークレットは必ず ExternalSecret で
```

## Naming Conventions

- **Terraform リソース**: `snake_case` (例: `hcloud_server.nodes`, `cloudflare_zero_trust_tunnel_cloudflared.main`)
- **Kubernetes リソース名**: `kebab-case` (例: `cloudflared-token`, `directus-secrets`)
- **ArgoCD Application 名**: サービス名そのまま (例: `external-secrets`, `mailserver`)
- **Namespace**: サービス名またはドメイン (例: `prod`, `staging`, `external-secrets`, `monitoring`)

## ExternalSecret パターン

シークレットが必要な全サービスはこのパターンに従う:

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: <service>-secrets
  namespace: prod
spec:
  secretStoreRef:
    kind: ClusterSecretStore
    name: infisical
  target:
    name: <service>-secrets
  data:
    - secretKey: <ENV_VAR_NAME>
      remoteRef:
        key: <INFISICAL_KEY>
```

## ArgoCD Sync Wave パターン

- `sync-wave: "-1"` → ESO・ClusterSecretStore・CloudNativePG Operator・cert-manager・nginx-ingress (他 App の前提)
- `sync-wave: "0"` (デフォルト) → Authentik・Directus・mailserver・Roundcube・cloudflared など

**CloudNativePG はマニフェストなし**: CNPG Operator は Helm chart (`cloudnativepg.yaml`) のみで管理。各サービスの DB Cluster は `manifests/prod/<service>/db-cluster.yaml` に置く。

## 実際の apps/prod/ 一覧

```
wave: -1  eso.yaml, cluster-secret-store.yaml, cloudnativepg.yaml,
          cert-manager.yaml, kube-state-metrics.yaml, namespace-config.yaml,
          snapshot-controller.yaml, volsync.yaml
wave: 1   nginx-ingress.yaml
wave: 0   authentik.yaml, directus.yaml, mailserver.yaml,
          roundcube.yaml, cloudflared.yaml, reloader.yaml,
          argocd-config.yaml, autoconfig.yaml, monitoring.yaml,
          cert-manager-config.yaml
```

---
_Document patterns, not file trees. New files following patterns shouldn't require updates_
