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
- ロールは `k3s-server`、`swap`（全ノード共通のホスト側 OOM 安全弁）、および `k3s-agent` (現状 agent は未使用)
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

## ApplicationSet (ephemeral PR-preview) パターン

`apps/<env>/` には通常の `Application` に加え、`ApplicationSet`(`pullRequest` generator)も配置できる。open な PR ごとに ephemeral な `Application` を自動生成・自動削除する用途で、対象リソースは既存サービスの一部ファイルを kustomize overlay で参照する専用ディレクトリ(例: `manifests/staging/<service>-preview/`)に切り出し、`spec.source.kustomize.nameSuffix` でリソース名をユニーク化して常設 Application と競合しないようにする。詳細は `tech.md` の「Directus schema PR の staging 事前検証」参照。

## ArgoCD Sync Wave パターン

- `sync-wave: "-1"` → ESO・ClusterSecretStore・CloudNativePG Operator・cert-manager・nginx-ingress (前提基盤)
- `sync-wave: "0"` (デフォルト) → 各種アプリ (Authentik, Directus, mailserver, Roundcube, cloudflared等)

**DBリソースについて**: CloudNativePG Operator 自体は Helm (`cloudnativepg.yaml`) で管理。DB Cluster 定義は各アプリの `manifests/prod/<service>/db-cluster.yaml` に配置。

## プロジェクトメモリ同期プロセス

仕様完了（`phase: completed`）または変更時、インフラの変更情報を主要ドキュメント（`CLAUDE.md`, `steering/` 内ドキュメント）に確実に反映・同期します。

### 1. 同期・転記基準
- **新規公開サービス・サブドメイン**: `CLAUDE.md` および `steering/structure.md` (apps/prod/一覧等) に追加。
- **新規環境変数・シークレット**: `CLAUDE.md` および `steering/tech.md` (シークレット一覧) にキーを追加（値は含めない）。
- **手順・コマンドの変更**: 運用コマンドやブートストラップ手順に変更があれば `CLAUDE.md` を更新。

### 2. [RULE] ドキュメントの自律的同期
コード（Terraform、Ansible、GitOpsマニフェスト）に変更を加えた場合、AIエージェントは自律的に関連するドキュメント（`CLAUDE.md`、`steering/` 内ドキュメント）をスキャンし、最新の状態に同期しなければならない。コードの変更のみでタスクを完了してはならない。

---
_Document patterns, not file trees. New files following patterns shouldn't require updates_
