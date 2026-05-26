# Product Overview

荒牧祭実行委員会の情報基盤インフラを管理するモノレポ。
クラウドリソースの定義からクラスター初期化・アプリケーション運用まで、一貫して Infrastructure as Code で管理する。

## Core Capabilities

- **クラウドプロビジョニング**: Terraform で Hetzner Cloud・Cloudflare・Tailscale を宣言的に管理
- **K3s HA クラスター**: 3ノード全員が etcd + ワークロードを担う構成で単一障害点なし
- **GitOps 運用**: ArgoCD の App of Apps パターンで Git の状態がそのままクラスターの状態になる
- **セキュアなシークレット管理**: マニフェストにシークレットを書かず、Infisical + ESO で全注入
- **外部アクセス管理**: Cloudflare Tunnel + Access で VPN 不要の安全な公開 / 保護

## Target Use Cases

- 荒牧祭開催期間中の情報サービス (CMS・メール・認証) の安定稼働
- 委員会メンバーが Tailscale なしでブラウザから管理画面 (ArgoCD / Authentik) にアクセス
- インフラ担当者が宣言的な変更だけで安全にサービスを追加・更新できる

## Value Proposition

- **完全自動化されたブートストラップ**: `terraform apply` 一発で VPS 作成 → K3s 構築 → GitOps 起動まで完結
- **ゼロ秘密漏洩アーキテクチャ**: パブリックリポジトリでも secrets を一切含まない設計
- **低コスト・高可用**: CX23 (¥700/月) × 3 台で HA etcd クラスターを実現

---
_Focus on patterns and purpose, not exhaustive feature lists_
