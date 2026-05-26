# Project Structure

## Organization Philosophy

**責務別レイヤー分離**: IaC (terraform) → 構成管理 (ansible) → GitOps (gitops) の順で責務が明確に分離されている。
レイヤーをまたいだ変更は依存関係の順序を意識すること (Terraform → Ansible → GitOps の順が bootstrap の基本)。

## Directory Patterns

### Terraform (`terraform/`)
**目的**: クラウドプロバイダーリソースの宣言的定義  
**ファイル粒度**: リソース種別ごとに 1 ファイル  
```
main.tf       ← ノード (hcloud_server) + Ansible bootstrap null_resource
firewall.tf   ← Hetzner ファイアウォールルール
dns.tf        ← Cloudflare DNS レコード
tunnel.tf     ← Cloudflare Tunnel 設定
access.tf     ← Cloudflare Access (staging 保護 + Authentik OIDC IdP)
tailscale.tf  ← Tailscale auth key 発行
storage.tf    ← Hetzner Object Storage
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
- **ArgoCD Application 名**: サービス名そのまま (例: `external-secrets`, `stalwart`)
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

- `sync-wave: "-1"` → ESO (他 App が ExternalSecret を使うための前提)
- `sync-wave: "0"` (デフォルト) → その他すべてのアプリ

---
_Document patterns, not file trees. New files following patterns shouldn't require updates_
