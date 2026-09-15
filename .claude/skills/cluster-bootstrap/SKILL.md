---
name: cluster-bootstrap
description: prod-node-1 クラスタをゼロから新規構築するブートストラップフロー（Terraform → Ansible → ESO同期）。クラスタの新規作成・全面再構築時に使用。
---

# ブートストラップフロー

1. **Terraform Apply**: Hetzner シングルノード（`prod-node-1`）作成 (cloud-init で Tailscale 自動インストール) & 各種 DNS・トンネル・サードパーティ（Authentik、Netdata、Healthchecks.io等）設定。
2. **Ansible 実行 (手動)**:
   - ホスト側の OOM 安全弁として swap ファイル（4GB）を作成。
   - K3s (`--cluster-init`, `fail-swap-on=false`) インストール。
   - Cilium CNI を Helm でデプロイ (ノードが Ready になるまで待機)。
   - cloudflared の事前インストール (ArgoCDへの外部アクセス経路確保)。
   - kubeconfig を手元に取得し、Tailscale IP に書き換え、Infisical へ登録。
   - ArgoCD インストール ＆ `infisical-auth` Secret の直接作成。
   - GitHub Deploy Key 登録 ＆ `root.yaml` (App of Apps) 適用。
3. **ESO同期**: `sync-wave: "-1"` により ESO と ClusterSecretStore が先行起動し、`wave: 0` の各アプリ（Authentik, Directus, DMS, Vaultwarden 等）に必要なシークレットを自動注入。
