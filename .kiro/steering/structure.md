# Project Structure

## Organization Philosophy

**責務別レイヤー分離**: IaC (terraform) → 構成管理 (ansible) → GitOps (gitops) の順で責務が明確に分離されている。
レイヤーをまたいだ変更は依存関係の順序を意識すること (Terraform → Ansible → GitOps の順が bootstrap の基本)。

## Directory Patterns

### Terraform (`terraform/`)
**目的**: クラウドプロバイダーリソースの宣言的定義  
**ファイル粒度**: リソース種別ごとに 1 ファイル  
```
providers.tf         ← Terraform provider 設定 (hcloud, cloudflare, tailscale ほか)
main.tf              ← ノード (hcloud_server)  ※null_resource はコメントアウト済み
firewall.tf          ← Hetzner ファイアウォールルール
network.tf           ← Hetzner プライベートネットワーク
dns.tf               ← Cloudflare DNS レコード
tunnel.tf            ← Cloudflare Tunnel 設定
access.tf            ← Cloudflare Access (dev/workers.dev 保護 + Zitadel OIDC IdP)
tailscale.tf         ← Tailscale auth key 発行
storage.tf           ← Hetzner Object Storage (バケットは手動作成、TF リソースはコメントアウト)
authentik_*.tf.disabled ← Authentik 時代の定義一式。ルートモジュール外で plan/apply の対象外
variables.tf / outputs.tf  ← 変数・出力
```

### Ansible (`ansible/`)
**目的**: K3s クラスターのブートストラップと構成管理  
**構造**: `inventory/` + `playbooks/` + `roles/`  
- インベントリは Tailscale MagicDNS 名を使用 (IP ではなくホスト名)
- ロールは `k3s-server`（K3s インストール・設定）、`swap`（全ノード共通のホスト側 OOM 安全弁）、`os-auto-update`（ホスト OS 自動更新設定の配布・結果通知）、`zitadel-bootstrap`（Zitadel リソース管理）、`zitadel-cutover`（Zitadel カットオーバーの事前条件確認・検証）で構成する
- K3s 設定フラグは `k3s-server` ロールの `k3s_extra_args` で渡す

### ノード増減 (festival-peak-scaleout)
**目的**: 1 ノードと 3 ノードの一時拡張・縮退の手順と、その Hetzner 実機検証を再実行できる形で保持する
```
docs/node-scaling-runbook.md                ← スケールアウト・縮退の手順。各工程の検証環境 (k3d / Hetzner / 未検証) を明記
scripts/scaletest/scaletest.sh              ← 検証専用 Hetzner プロジェクトに使い捨ての 3 ノードを作る・消すハーネス
ansible/playbooks/scaletest-bootstrap.yml   ← 検証専用 playbook (k3s-server・swap ロール + Cilium。本番 k3s-bootstrap.yml は使わない)
ansible/inventory/scaletest.yml             ← 検証専用 inventory (scaletest-1..3、本番ホストを含まない)
```
検証は Infisical の `staging` 環境 `/scaletest` の資格情報で行い、`prod` は使わない。Cilium・Helm・`k3s_version` は本番の playbook・inventory から読み取る。

### kube-apiserver 認証 (GitHub OIDC・短命証明書)
**目的**: 共有 kubeconfig を使わず、CI・DR・人が kube-apiserver に認証する
```
ansible/roles/k3s-server/
  defaults/main.yml                     ← OIDC 許可リスト (k3s_github_oidc_workflows)・audience・証明書上限・監査ログ上限
  templates/authentication-config.yaml.j2 / audit-policy.yaml.j2  ← AuthenticationConfiguration・監査ポリシー
gitops/apps/prod/kube-access.yaml       ← RBAC の Application (wave -1)
gitops/manifests/prod/kube-access/
  workflows.yaml                        ← gha:* の最小権限 ClusterRole / Binding
  dr-recovery.yaml                      ← DR 用 binding (k3s-bootstrap が先行適用。単体ファイルで置く)
  humans.yaml                           ← github:<login>:<数値ID> の binding
.github/scripts/kube-oidc.sh            ← CI・DR 共通の OIDC kubeconfig 生成
.github/scripts/kube-cert-validate.sh   ← CSR 検証 (発行ワークフローと手元で共用)
.github/scripts/verify-environment-protection.sh ← Environment の保護設定検査
.github/workflows/kube-cert-issue.yml   ← 人向け証明書の発行
scripts/kube-login.sh                   ← `make kube-login` の実体 (コンテキスト aramakisai-prod の作成)
```

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

### 運用ポータル・運用ダッシュボード (`ops-dashboard`)
**目的**: executive 向けポータルと admin 向け運用ダッシュボードを `dash.aramakisai.com` で提供する
```
gitops/apps/prod/ops-dashboard.yaml             ← ArgoCD Application (wave 0、namespace ops-dashboard)
gitops/manifests/prod/ops-dashboard/
  portal/                                       ← nginx + oauth2-proxy + Homer。links.yaml がリンクの単一ソース
  collector/                                    ← Python の collector。dashboard.toml がプラン・期待サーバー等の宣言
```
公開経路は Cloudflare Tunnel (`terraform/tunnel.tf`) から portal の ClusterIP へ直結。運用は `docs/ops-dashboard-runbook.md`。

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
