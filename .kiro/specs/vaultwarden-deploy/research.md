# Research & Design Decisions

---

## Summary
- **Feature**: vaultwarden-deploy
- **Discovery Scope**: 拡張型（既存 GitOps/K3s パターンの再利用）
- **Key Findings**:
  - Vaultwarden collections は `Can View` / `Can View Except Passwords` / `Can Edit` / `Can Manage` の権限レベルをネイティブに提供し、コード変更なしで要件 1 の RBAC を満たす。
  - PostgreSQL バックエンドには `DATABASE_URL` 環境変数のみ必要。コネクションプーリングは Vaultwarden 内部で実施。
  - 添付ファイル・アイコンには永続ボリューム（`DATA_FOLDER`）が必要。VolSync `ReplicationSource` によるバックアップは既存 mailserver パターンと一致。
  - Cloudflare Tunnel の ingress rule + DNS CNAME レコード追加が唯一のインフラ変更。
  - Vaultwarden は標準で SSO/OIDC をサポート（`SSO_ENABLED`, `SSO_AUTHORITY`, `SSO_CLIENT_ID`, `SSO_CLIENT_SECRET`）。Authentik 連携は Terraform `authentik_provider_oauth2` で実現。

## Research Log

### Vaultwarden RBAC モデル
- **Context**: 要件 1 はコレクション単位のアクセス制御（閲覧のみ、自動入力のみ、編集）を必要とする。
- **Sources Consulted**: Vaultwarden GitHub wiki、Bitwarden 公式ドキュメント（Organization Collections）
- **Findings**:
  - Vaultwarden（Bitwarden 互換サーバー）は Organization と Collection を実装している。
  - Collection 権限: `Can View`、`Can View Except Passwords`、`Can Edit`、`Can Manage`。
  - `Can View Except Passwords` は自動入力を許可しつつ UI で平文パスワードを非表示にする（要件 1.4）。
  - `Can View` は閲覧を許可し編集・削除を防止する（要件 1.3）。
  - `Can Edit` は完全な変更を許可する（要件 1.2）。
  - グループを Collection にこれらの権限レベルで割り当て可能。
- **Implications**: カスタム RBAC 実装は不要。デプロイ後に Vaultwarden 管理 UI で設定する。

### データベースバックエンドの選択
- **Context**: 要件 2.1 はリレーショナルデータベースを指定。SQLite がデフォルトだが本番では PostgreSQL を推奨。
- **Sources Consulted**: Vaultwarden wiki「Using the PostgreSQL Backend」、リポジトリ内の既存 CNPG パターン。
- **Findings**:
  - `DATABASE_URL=postgresql://user:pass@host:5432/dbname` のみ必要。
  - Vaultwarden Docker イメージはデフォルトで PostgreSQL をサポート。
  - CNPG Operator は `Cluster` CRD で自動バックアップ、フェイルオーバー、リカバリを提供。
  - 既存の `directus-db`、`authentik-db` クラスターと同一パターン。
- **Implications**: CloudNativePG `Cluster` を同一イメージ（`ghcr.io/cloudnative-pg/postgresql:16.8`）と同一バックアップ設定（Hetzner Object Storage via barman）で使用する。

### 添付ファイル用の永続ストレージ
- **Context**: 要件 2.2 は添付ファイル・組織アイコンの永続ボリュームを要求。
- **Sources Consulted**: Vaultwarden `.env.template`、リポジトリ内の既存 PVC パターン。
- **Findings**:
  - `DATA_FOLDER`（デフォルト `data`）は `attachments/`、`sends/`、`icon_cache/` を保存。
  - `ATTACHMENTS_FOLDER` で `/data/attachments` へ上書き可能。
  - Roundcube は SQLite PVC（`roundcube-db`）を使用。mailserver は `local-path` PVC + VolSync を使用。
  - VolSync `ReplicationSource` + `restic` mover で PVC 内容を Backblaze B2 へバックアップ。
- **Implications**: PVC `vaultwarden-data`（5Gi）と B2 への restic バックアップ用 `ReplicationSource` を mailserver パターンに従って作成。

### ネットワークと外部アクセス
- **Context**: 要件 3 は外部プロキシ経由の HTTPS・WebSocket 対応を要求。
- **Sources Consulted**: `terraform/tunnel.tf`、`terraform/dns.tf`、Cloudflare Tunnel ドキュメント。
- **Findings**:
  - Cloudflare Tunnel はネイティブで WebSocket をサポート（HTTP2/QUIC トランスポート）。
  - Vaultwarden はデフォルトで `WEBSOCKET_ENABLED=true`。リアルタイムボールト同期に使用。
  - Tunnel ingress rule 形式: `service = "http://svc.ns.svc.cluster.local:port"`。
  - DNS CNAME レコードは `<tunnel-id>.cfargotunnel.com` を指す。
- **Implications**: Tunnel 設定に `vault.aramakisai.com` の ingress rule を追加。`dns.tf` に CNAME レコードを追加。

### シークレット管理パターン
- **Context**: 要件 4 は Infisical/ESO 連携を要求。
- **Sources Consulted**: 各サービスの既存 `external-secret.yaml`。
- **Findings**:
  - パターン: `ExternalSecret` → `ClusterSecretStore`（infisical）→ K8s Secret。
  - 2 つの ExternalSecret が必要: アプリシークレット（`vaultwarden-secrets`）と DB 認証情報（`vaultwarden-db-credentials`）。
  - CNPG は credential Secret のキー `username` と `password` を期待。
- **Implications**: `directus/external-secret.yaml` と `authentik/external-secret.yaml` のパターンを厳密に踏襲する。

## Architecture Pattern Evaluation

| Option | Description | Strengths | Risks / Limitations | Notes |
|--------|-------------|-----------|---------------------|-------|
| GitOps Extension | 既存 K3s/ArgoCD 設定にマニフェスト + TF レコードを追加 | steering と 100% 整合。新しい概念が最小限 | 特になし | 採用 |
| 専用 VM | 別の Hetzner インスタンスで Vaultwarden を実行 | 完全な分離 | 追加コスト、複雑性、運用オーバーヘッド | 却下 |
| SaaS（Bitwarden） | ホスト型 Bitwarden を使用 | 運用オーバーヘッドゼロ | コスト、ベンダーロックイン、self-host 優先の steering に反する | 却下 |

## Design Decisions

### Decision: Vaultwarden Collections を RBAC に使用
- **Context**: 要件 1 は共有資格情報のロールベースアクセス制御を要求。
- **Alternatives Considered**:
  1. カスタム RBAC レイヤー — Vaultwarden 周りにラッパー API を構築。
  2. Vaultwarden Organization + Collection をネイティブに使用。
- **Selected Approach**: ネイティブ Vaultwarden Organization + Collection。
- **Rationale**: Collection は必要な権限モデル（閲覧、パスワード非表示、編集）を既にサポート。カスタムコード不要。管理者が管理 UI でユーザーを招待。
- **Trade-offs**: RBAC 設定は管理 UI で手動（宣言的ではない）。小規模組織では許容範囲。
- **Follow-up**: 運用手順書に collection 設定手順を記載。

### Decision: PVC + VolSync で添付ファイルをバックアップ
- **Context**: 要件 2.4 はボリュームバックアップを要求。
- **Alternatives Considered**:
  1. CNPG barman で全て — barman は DB のみバックアップし、添付ファイルは対象外。
  2. VolSync ReplicationSource + restic — mailserver と同一。
  3. 手動 S3 同期 CronJob。
- **Selected Approach**: VolSync `ReplicationSource` + restic mover で B2 へ。
- **Rationale**: リポジトリ内の既存パターン（mailserver）。自動化、増分、暗号化保存。
- **Trade-offs**: Infisical に restic リポジトリ Secret が必要。
- **Follow-up**: VolSync Operator がデプロイ済みか確認（`volsync.yaml` アプリ経由でデプロイ済み）。

### Decision: Authentik OIDC SSO 連携
- **Context**: 要件で Authentik との SSO 連携が必須。
- **Alternatives Considered**:
  1. Vaultwarden SSO 機能を使用して Authentik OIDC Provider と直接連携。
  2. OAuth2 Proxy などのサイドカーを使用して間接連携。
- **Selected Approach**: Vaultwarden ネイティブ SSO（`SSO_ENABLED=true`）+ Authentik OIDC Provider。
- **Rationale**: Vaultwarden は標準で OpenID Connect をサポート（`SSO_ENABLED`, `SSO_AUTHORITY`, `SSO_CLIENT_ID`, `SSO_CLIENT_SECRET`）。Terraform で `authentik_provider_oauth2` + `authentik_application` を作成し、既存パターン（Roundcube / ArgoCD / Room Presence）と同一構造。
- **Trade-offs**: マスターパスワードは依然として必要（Vaultwarden の暗号化キー導出に使用）。SSO は認証のみ担当し、マスターパスワードはユーザーが初回ログイン時に設定する。
- **Follow-up**: `SSO_AUTHORITY` は `issuer` 値と完全に一致する必要あり。末尾スラッシュ要確認。

## Risks & Mitigations

- **Risk**: Vaultwarden 管理者トークンが環境変数で露出 → Mitigation: Infisical で保存し ESO 経由で注入。リポジトリへのコミットは絶対に行わない。
- **Risk**: Collection RBAC の設定ミスで資格情報が漏洩 → Mitigation: 設定手順を文書化。組織作成を管理者のみに制限（`ORG_CREATION_USERS` 環境変数）。
- **Risk**: 添付ファイルボリュームが無限に増加 → Mitigation: 5Gi で初期設定。監視で追跡し必要に応じて拡張。
- **Risk**: DR 時 — DB と添付ファイルの両方のバックアップが完全復旧に必要 → Mitigation: CNPG recovery bootstrap + VolSync リストアを DR 運用手順書に記載。

## References

- [Vaultwarden GitHub](https://github.com/dani-garcia/vaultwarden) — アップストリームサーバー
- [Vaultwarden Configuration](https://github.com/dani-garcia/vaultwarden/blob/main/.env.template) — 環境変数リファレンス
- [Vaultwarden PostgreSQL Backend](https://github.com/dani-garcia/vaultwarden/wiki/Using-the-PostgreSQL-Backend) — DB 設定
- [Bitwarden Organizations](https://bitwarden.com/help/about-organizations/) — RBAC モデルドキュメント
- [CloudNativePG Documentation](https://cloudnative-pg.io/documentation/) — Operator リファレンス
- [VolSync Documentation](https://volsync.readthedocs.io/) — PVC バックアップ/リストア
