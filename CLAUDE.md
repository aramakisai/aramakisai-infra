# 🔧 CLAUDE.md

## プロジェクト概要
荒牧祭実行委員会の情報基盤インフラを管理するリポジトリ。
Terraform で Hetzner Cloud・Cloudflare・Tailscale を管理し、Ansible で K3s をブートストラップし、ArgoCD で GitOps 管理を行う。

## クイックリンク
- [GEMINI.md](GEMINI.md) — Agentic SDLC & Spec-Driven Development ルール
- [steering/tech.md](.kiro/steering/tech.md) — 詳細な技術構成・変数・シークレット一覧
- [steering/structure.md](.kiro/steering/structure.md) — ディレクトリ構成・パターン・ドキュメント同期ルール
- [steering/dr.md](.kiro/steering/dr.md) — DR・運用の知見

## 主要コマンド

### 1. Terraform (IaC)
```bash
# 初期化
cd terraform && terraform init

# 差分確認 (Infisical経由で環境変数を注入)
infisical run --env=prod -- terraform plan

# 適用
infisical run --env=prod -- terraform apply
```

**既知の差分 (異常ではない):** `tailscale_tailnet_key.k3s_nodes` は `expiry = 3600`
(1時間) の設計により、前回 apply から1時間以上経過していれば `terraform plan` の
たびに `must be replaced` が出る（tailscale.tf のコメント参照）。これは意図した
挙動であり、他の変更と無関係に発生する。apply 対象を `-target` で絞る必要はなく、
他の変更とまとめて apply してよい。

### 2. Ansible (構成管理)
```bash
# K3s ブートストラップ実行 (Terraform適用後に手動実行)
infisical run -- ansible-playbook -i ansible/inventory/tailscale.yml ansible/playbooks/k3s-bootstrap.yml

# 稼働中クラスタへの再実行前に差分を確認 (main をチェックアウトして実行。差分は changed として表示される)
infisical run -- ansible-playbook -i ansible/inventory/tailscale.yml ansible/playbooks/k3s-bootstrap.yml --check --diff

# bootstrap Secret (cloudflared-token / infisical-auth / ArgoCD repo 鍵) の上書きが必要なときだけ
#   -e rotate_bootstrap_secrets=true (全件) または -e '{"rotate_bootstrap_secrets": ["infisical-auth"]}' (個別) を付ける
#   (既定は既存の非空値を保持)

# K3s バージョンアップ
infisical run -- ansible-playbook -i ansible/inventory/tailscale.yml ansible/playbooks/k3s-bootstrap.yml -e "k3s_version=v1.32.3+k3s1"

# ノードの強制再プロビジョニング (シングルノード prod-node-1)
infisical run -- ansible -i ansible/inventory/tailscale.yml prod-node-1 -m shell -a "/usr/local/bin/k3s-uninstall.sh"
infisical run -- ansible-playbook -i ansible/inventory/tailscale.yml ansible/playbooks/k3s-bootstrap.yml

# K3sバージョン新規検知の手動確認 (通常は週次cronで自動実行、Discordへ通知)
gh workflow run k3s-version-check.yml

# K3sアップグレード適用 (mainブランチにコミット済みのk3s_versionを使用、入力パラメータなし)
gh workflow run k3s-upgrade.yml
```

### 3. DR 復旧 (人の承認付き)
```bash
# dr-trigger は通知のみ。復旧は人が起動し、Environment `dr-recovery` の reviewer が承認する
gh workflow run dr-recovery.yml --repo aramakisai/aramakisai-infra -f target_node=prod-node-1
```
`force` (生存確認ゲート上書き) は既定で無効。mailserver データのリストアは自動化していない。手順は [docs/dr-runbook.md](docs/dr-runbook.md)。

### 4. K3s 操作・検証
```bash
# ノード接続 (Tailscale SSH)
ssh root@prod-node-1

# 初回と 7 日ごとの再発行: GitHub 経由で短命クライアント証明書を発行し、コンテキスト aramakisai-prod を作成・更新する
make kube-login

# クラスター状態確認
make kubectl ARGS="get nodes -o wide"
make kubectl ARGS="get pods -A"
```
`kubectl` は `make kubectl` 経由で、手元のコンテキスト `aramakisai-prod` を使って実行する (`kubectl --context aramakisai-prod` と同じ)。

- 前提: `gh` ログイン済み (2.87.0 以上)、tailnet 接続済み、リポジトリの write 権限。
- `make kube-login` は鍵と CSR を手元で作り、`kube-cert-issue.yml` で署名済み証明書を受け取る。証明書の有効期限は 7 日で、切れたら再実行する (再実行は新しい鍵で発行し直してコンテキストを置き換える)。秘密鍵は手元から出ない。
- server CA が既存のコンテキストと異なる場合 (クラスタ再作成など) は指紋を表示して止まる。指紋が正当と確認できたときだけ `make kube-login ARGS=--accept-new-ca` で更新する。
- クラスタ再作成後は CA が変わるため、全員が `make kube-login` で証明書を再発行する。

### 権限の付与・剥奪 (kube-access)

kube-apiserver への権限はすべて `gitops/manifests/prod/kube-access/` の RBAC が正本で、ArgoCD Application `kube-access` が同期する (直接 `kubectl` で binding を作らない)。

- **付与**: `humans.yaml` に ClusterRoleBinding を追加する PR を出す。ユーザー名は `github:<GitHubログイン名>:<数値ID>` (数値 ID は `gh api users/<login> --jq .id`)。ログイン名を変えると一致せず権限を失う。
- **剥奪**: binding を削除する PR をマージする。ArgoCD の prune で即時に反映される。
- クライアント証明書は **失効できない**。証明書自体は有効期限まで認証を通るため、即時の剥奪は binding の削除で行う (証明書は認証されるだけで権限がなくなる)。残存リスクの上限は証明書の有効期限 (最長 7 日)。
- 発行ワークフローが侵害された疑いがある場合に旧証明書を無効にできるのは client CA の forced rotation だけで、手順は設計 (`.kiro/specs/kube-github-auth/design.md`) にある。

### GitHub 障害時の挙動

認証の信頼の起点は GitHub (Actions と OIDC 発行) のため、障害中は次のとおり動く。

- 止まる: `make kube-login` による発行、CI (infra-health-check・intrusion-response など)、DR (`dr-recovery`)。
- 使える: 発行済みで有効期限内の証明書による `make kubectl`。kube-apiserver が OIDC の公開鍵を取得できなくても、クライアント証明書の認証は続く。
- 証明書が期限切れで再発行できない緊急時は、Tailscale SSH でノードに入り、ノード上のローカル admin (`/etc/rancher/k3s/k3s.yaml`) を使える。平常時の運用には使わない。
- DR が必要なときは `dr-recovery` が動かないため、`docs/dr-runbook.md` の「手動フォールバック」に従う。

### GitOps 原則：クラスタへの直接操作禁止

`gitops/` 配下のリソースはすべて ArgoCD が正本。**直接 `kubectl patch/edit/apply` でクラスタを変更することを禁止する。**

live リソースに Git にない差分（直接 patch で追加されたフィールド等）が残存していても、ArgoCD の client-side apply は「last-applied-configuration に記録されていない外部追加フィールド」を除去しないため、ArgoCD が "Synced" を表示していても実際はドリフトが存在する場合がある。

**クラスタリソースを修正する正しい手順:**

1. Git manifest を修正してコミット・プッシュ
2. ArgoCD で sync（必要なら Hard Refresh + Force Sync / server-side apply）

```bash
# SSA で全フィールドの ownership を ArgoCD に渡す（フィールドドリフト解消）
argocd app sync <app-name> --server-side --force
```

**例外（直接操作が許される場合）:** ArgoCD 自身が管理しない Bootstrap リソース（`infisical-auth` Secret、ArgoCD インストール直後の初期 Secret 等）のみ。

## ブートストラップフロー

1. **Terraform Apply**: Hetzner シングルノード（`prod-node-1`）作成 (cloud-init で Tailscale 自動インストール) & 各種 DNS・トンネル・サードパーティ（Authentik、Netdata、Healthchecks.io等）設定。
2. **Ansible 実行 (手動)**:
   - ホスト側の OOM 安全弁として swap ファイル（4GB）を作成。
   - K3s (`--cluster-init`, `fail-swap-on=false`) インストール。
   - Cilium CNI を Helm でデプロイ (ノードが Ready になるまで待機)。
   - cloudflared の事前インストール (ArgoCDへの外部アクセス経路確保)。
   - ArgoCD インストール ＆ `infisical-auth` Secret の直接作成。
   - GitHub Deploy Key 登録 ＆ `root.yaml` (App of Apps) 適用。
3. **ESO同期**: `sync-wave: "-1"` により ESO と ClusterSecretStore が先行起動し、`wave: 0` の各アプリ（Authentik, Directus, DMS, Vaultwarden 等）に必要なシークレットを自動注入。

## 変更時更新ナビゲーション（ドキュメント同期）

コード変更やインフラ構成の変更を行った際、以下のチェックリストに従って関連ドキュメントを同期更新してください。

### ドキュメント更新チェックリスト
- [ ] **Terraform 設定（プロバイダー、出力など）の変更**:
  - 新規プロバイダーやバージョンの変更があるか？ → [.kiro/steering/tech.md](.kiro/steering/tech.md) の「Key Providers & Versions」を更新。
  - 新規出力パラメータを追加したか？ → [.kiro/steering/tech.md](.kiro/steering/tech.md) の「Key Technical Decisions (Terraform 変更)」や出力パラメータ説明に追記。
- [ ] **Ansible ロールやプレイブックの変更**:
  - ディレクトリ構造に変更があったか？ → [README.md](README.md) の「ディレクトリ構成」および [.kiro/steering/structure.md](.kiro/steering/structure.md) を更新。
  - ホスト制限、クラスター構成、swap やカーネルパラメータ設定などの変更か？ → [.kiro/steering/tech.md](.kiro/steering/tech.md) を更新。
- [ ] **GitOps マニフェストやアプリ構成の変更**:
  - 新規サービスやサブドメインを追加したか？ → [README.md](README.md) の「デプロイされるサービス」テーブルおよび [.kiro/steering/structure.md](.kiro/steering/structure.md) を更新。
  - 新規環境変数やシークレットが追加されたか？ → [.kiro/steering/tech.md](.kiro/steering/tech.md) の「Infisical で管理するシークレット一覧」にキー名を追記（※値は含めない）。
  - DB の構成（CNPG等）やリストア・バックアップロジックの変更か？ → [.kiro/steering/dr.md](.kiro/steering/dr.md) の CNPG / 各アプリのバックアップセクションを更新。
  - 監視（Falco）などの誤検知除外設定に変更があるか？ → [.kiro/steering/tech.md](.kiro/steering/tech.md) の「監視スタック」または「Key Technical Decisions」に知見を追記。
- [ ] **仕様完了時（phase: completed 前）**:
  - 新規サービスや追加シークレットが上記基準に沿ってプロジェクトメモリ（`steering/` 内）に正しく同期されているか検証・転記すること。

