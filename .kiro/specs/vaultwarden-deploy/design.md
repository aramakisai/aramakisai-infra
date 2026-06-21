# Design Document — Vaultwarden Deploy

---

## Overview

本仕様は、荒牧祭実行委員会のメンバー間でパスワードおよび資格情報を安全に共有・管理するための認証情報管理サービス（Vaultwarden）を K3s クラスター上にデプロイする。

**Users**: 実行委員会メンバーが Web ブラウザ、ブラウザ拡張、モバイルアプリから資格情報を参照・編集・自動入力する。
**Impact**: GitOps マニフェストの追加、Cloudflare Tunnel/DNS の更新、Infisical シークレットの登録により、新たな基盤サービスとして `vault.aramakisai.com` が提供される。

### Goals
- Vaultwarden を PostgreSQL バックエンド・永続ボリューム・自動バックアップとともにデプロイする
- Cloudflare Tunnel 経由で `vault.aramakisai.com` に HTTPS・WebSocket 対応で公開する
- Infisical/ESO 経由ですべての機密設定を動的に注入する
- Vaultwarden Organization/Collection の権限モデルを活用し、共有資格情報の RBAC を実現する
- Authentik を OIDC Provider として SSO 連携を実現する
- Authentik グループを RBAC の真の情報源（SSOT）として使用し、Collection 名とグループ名を一致させて権限をマッピングする

### Non-Goals
- クライアントアプリ（ブラウザ拡張・モバイルアプリ）の配布・設定
- 高可用性（シングルノード構成のため既存パターンに従う）
- 監視アラートの新規定義（基盤監視は steering ドキュメントに従う）

## Boundary Commitments

### This Spec Owns
- Vaultwarden Deployment、Service、PVC、ExternalSecret のマニフェスト定義
- CloudNativePG Cluster (`vaultwarden-db`) と ScheduledBackup の定義
- VolSync ReplicationSource (`vaultwarden-data` バックアップ) の定義
- Cloudflare Tunnel ingress rule と DNS CNAME レコードの追加
- Infisical へ登録が必要なシークレットキーの一覧定義
- 管理者トークン・無効化されたユーザー登録・招待制運用の構成
- Authentik OIDC Provider/Application の Terraform 定義

### Out of Boundary
- クライアントアプリのインストール・設定手順
- ノードレベルの監視・Falco ルール（既存 steering に従う）
- DR 自動復旧スクリプトの改修（新規 PVC/DB のリストアは `dr.md` 運用手順に追加するのみ）

### Allowed Dependencies
- K3s クラスター（prod-node-1）、Cilium、Cloudflare Tunnel（稼働中）
- ArgoCD、External Secrets Operator、CloudNativePG Operator（wave: -1 で先行インストール済み）
- VolSync Operator（mailserver と同様に利用可能）
- Infisical（シークレット Single Source of Truth）
- Authentik（既存 OIDC Provider として利用）
- Hetzner Object Storage（CNPG バックアップ先、既存 credential Secret 利用）
- Backblaze B2（VolSync restic バックアップ先、既存 credential Secret 利用）

### Revalidation Triggers
- Vaultwarden イメージタグ変更（マイグレーション手順要確認）
- Cloudflare Tunnel 設定構造変更（Terraform provider 更新時）
- CNPG Operator バージョンアップ（recovery bootstrap 手順要確認）
- Infisical キー名変更（ExternalSecret 定義追従が必要）

## Architecture

### Existing Architecture Analysis

当プロジェクトは **Terraform → Ansible → ArgoCD GitOps** の3層構成。本仕様は GitOps レイヤーの拡張に留まる。

- **Database**: CloudNativePG Operator が `Cluster` CRD で PostgreSQL クラスターを管理。既存サービス（Directus、Authentik、Roundcube）と同一パターン。
- **Secret Management**: External Secrets Operator が Infisical から K8s Secret を同期。全サービスが `ClusterSecretStore` (`infisical`) を参照。
- **Ingress**: Cloudflare Tunnel（cloudflared DaemonSet）が外部アクセスをクラスター Service に転送。HTTPS 終端・WebSocket 中継は Cloudflare が担当。
- **Backup**:
  - DB: CNPG `barmanObjectStore` で Hetzner Object Storage へ日次フルバックアップ + WAL アーカイブ。
  - PVC: VolSync `ReplicationSource` で Backblaze B2 へ restic バックアップ。mailserver が既に採用。

### Architecture Pattern & Boundary Map

```mermaid
graph TB
    subgraph External
        User[User / Browser Extension]
        CF[Cloudflare Edge]
        Authentik[Authentik IdP]
    end

    subgraph K3s_Cluster
        Tunnel[cloudflared Pod]
        VW[Vaultwarden Pod]
        VW_SVC[Vaultwarden Service]
        PVC[vaultwarden-data PVC]
        ESO[ExternalSecret Operator]
        CNPG[CloudNativePG Cluster]
        VS[VolSync ReplicationSource]
    end

    subgraph Storage
        B2[B2 VolSync Backup]
        HOS[Hetzner Object Storage CNPG Backup]
        Infisical[Infisical Secret Store]
    end

    User --> CF
    CF --> Tunnel
    Tunnel --> VW_SVC
    VW_SVC --> VW
    VW --> PVC
    VW --> CNPG
    VW -.->|SSO redirect| Authentik
    Authentik -.->|id_token callback| VW
    ESO --> Infisical
    VW -.->|reads secrets| ESO
    CNPG --> HOS
    VS --> B2
    PVC -.->|backed up by| VS
```

**Selected pattern**: GitOps Extension — 既存 ArgoCD + CNPG + ESO + VolSync パターンの再利用。
**Domain boundaries**:
- Vaultwarden app 層: Deployment + Service + PVC
- Data 層: CNPG Cluster + ScheduledBackup
- Secret 層: ExternalSecret (Infisical 連携)
- Backup 層: VolSync ReplicationSource
- Infra 層: Terraform DNS + Tunnel ingress

### Technology Stack

| Layer | Choice / Version | Role in Feature | Notes |
|-------|------------------|-----------------|-------|
| Backend / Services | Vaultwarden (latest stable Docker image) | Bitwarden-compatible password manager server | SSO/OIDC enabled. PostgreSQL backend enabled by default |
| Data / Storage | CloudNativePG (ghcr.io/cloudnative-pg/postgresql:16.8) | PostgreSQL cluster for vaultwarden-db | `skipEmptyWalArchiveCheck` annotation required |
| Data / Storage | Kubernetes PVC (5Gi) | Attachments / sends / icon_cache | `local-path` provisioner (single-node) |
| Data / Storage | VolSync (restic mover) | PVC backup to Backblaze B2 | Existing mailserver pattern reuse |
| Infrastructure / Runtime | K3s v1.32.3+k3s1 + Cilium | Container orchestration | Single-node prod-node-1 |
| Infrastructure / Runtime | ArgoCD | GitOps declarative sync | `sync-wave: "0"` |
| Infrastructure / Runtime | External Secrets Operator | Secret synchronization from Infisical | `ClusterSecretStore: infisical` |
| Infrastructure / Runtime | Cloudflare Tunnel (cloudflared) | External ingress without public ports | WebSocket native support |

## File Structure Plan

### Directory Structure

```
gitops/
  apps/prod/vaultwarden.yaml          # ArgoCD Application
  manifests/prod/vaultwarden/
    db-cluster.yaml                   # CloudNativePG Cluster
    scheduled-backup.yaml             # CNPG ScheduledBackup
    deployment.yaml                   # Vaultwarden Deployment
    service.yaml                      # Vaultwarden Service
    pvc.yaml                          # Data volume PVC
    external-secret.yaml              # Infisical → K8s Secret (app + DB + SSO)
    replication-source.yaml           # VolSync PVC backup
terraform/
  dns.tf                              # CNAME record for vault.aramakisai.com
  tunnel.tf                           # Ingress rule for vault.aramakisai.com
  authentik_apps.tf                   # Authentik OIDC Provider + Application for Vaultwarden
```

### Modified Files

- `terraform/dns.tf` — Add `cloudflare_record` for `vault` subdomain (CNAME to tunnel)
- `terraform/tunnel.tf` — Add `ingress_rule` for `vault.aramakisai.com` → `http://vaultwarden.prod.svc.cluster.local:80`
- `terraform/authentik_apps.tf` — Add `authentik_provider_oauth2` and `authentik_application` for Vaultwarden SSO

## System Flows

### Bootstrap and Sync Flow

```mermaid
sequenceDiagram
    participant User as User
    participant CF as Cloudflare Edge
    participant Tunnel as cloudflared
    participant SVC as vaultwarden Service
    participant VW as Vaultwarden Pod
    participant CNPG as vaultwarden-db-rw
    participant PVC as vaultwarden-data PVC

    User->>CF: HTTPS request vault.aramakisai.com
    CF->>Tunnel: WSS/HTTP2 tunnel
    Tunnel->>SVC: HTTP forward
    SVC->>VW: Route to Pod
    VW->>CNPG: SQL queries (DATABASE_URL)
    VW->>PVC: Read/write attachments
```

### Backup Flow

```mermaid
sequenceDiagram
    participant CNPG as vaultwarden-db Cluster
    participant HOS as Hetzner Object Storage
    participant VS as VolSync ReplicationSource
    participant B2 as Backblaze B2

    CNPG->>HOS: Daily barman-cloud-backup (cron)
    CNPG->>HOS: WAL archive (continuous)
    VS->>B2: restic backup every 6 hours
```

## Requirements Traceability

| Requirement | Summary | Components | Interfaces | Flows |
|-------------|---------|------------|------------|-------|
| 1.1 | Shared secrets grouped into collections | Vaultwarden Organization/Collection | Vaultwarden Web UI / API | — |
| 1.2 | Assign permissions per collection | Vaultwarden Collection permissions | Vaultwarden Web UI / API | — |
| 1.3 | View-only prevents edit/delete | Vaultwarden `Can View` permission | Vaultwarden API authz | — |
| 1.4 | Auto-fill only hides plaintext | Vaultwarden `Can View Except Passwords` | Vaultwarden API + Web UI | — |
| 2.1 | Database records persisted in PostgreSQL | `vaultwarden-db` CNPG Cluster | `DATABASE_URL` env var | Bootstrap flow |
| 2.2 | Persistent volume for attachments | `vaultwarden-data` PVC | `DATA_FOLDER` mount | Bootstrap flow |
| 2.3 | Automatic DB backup to object storage | CNPG `barmanObjectStore` | S3 API (Hetzner OS) | Backup flow |
| 2.4 | Periodic volume backup to remote storage | VolSync `ReplicationSource` | restic (Backblaze B2) | Backup flow |
| 3.1 | External proxy forwards traffic | Cloudflare Tunnel ingress rule | HTTP proxy | Bootstrap flow |
| 3.2 | WebSocket support for real-time sync | Cloudflare Tunnel (native WSS) | WebSocket upgrade | Bootstrap flow |
| 3.3 | HTTPS enforced for all requests | Cloudflare Edge (proxied=true) | TLS termination | — |
| 4.1 | Secrets synced from secret management | `vaultwarden-secrets` ExternalSecret | ESO → K8s Secret | — |
| 4.2 | Container reads config from env vars | Vaultwarden Deployment | `env` / `envFrom` | — |
| 5.1 | Disable public user registration | `SIGNUPS_ALLOWED=false` env var | Vaultwarden config | — |
| 5.2 | Invite-only registration | Vaultwarden admin panel invites | Vaultwarden admin API | — |
| 5.3 | Admin token required for admin portal | `ADMIN_TOKEN` env var | Vaultwarden admin auth | — |
| 6.1 | Enable SSO with Authentik OIDC | `SSO_ENABLED=true`, `SSO_AUTHORITY`, Authentik Provider | Vaultwarden OIDC config | Bootstrap flow |
| 6.2 | SSO redirect and auto-login | Authentik authorization flow + callback | OIDC redirect URI | Bootstrap flow |
| 6.3 | Link SSO to existing accounts by email | `SSO_SIGNUPS_MATCH_EMAIL=true` | Vaultwarden account mapping | — |
| 6.4 | Disable password login (SSO only) | `SSO_ONLY=true` | Vaultwarden auth policy | — |
| 6.5 | SSO credentials from Infisical | `vaultwarden-secrets` ExternalSecret | ESO → K8s Secret | — |

## Components and Interfaces

| Component | Domain/Layer | Intent | Req Coverage | Key Dependencies (P0/P1) | Contracts |
|-----------|--------------|--------|--------------|--------------------------|-----------|
| vaultwarden-db | Data | PostgreSQL cluster for Vaultwarden | 2.1, 2.3 | CNPG Operator (P0), hetzner-os-credentials (P0) | State |
| vaultwarden-secrets | Secret | Infisical-synced app secrets | 4.1, 4.2, 5.3 | ESO (P0), Infisical (P0) | State |
| vaultwarden-db-credentials | Secret | Infisical-synced DB credentials | 2.1, 4.1 | ESO (P0), Infisical (P0) | State |
| vaultwarden-data | Storage | PVC for attachments/sends/icons | 2.2, 2.4 | local-path provisioner (P0), VolSync (P1) | State |
| vaultwarden-backup | Storage | VolSync ReplicationSource to B2 | 2.4 | VolSync Operator (P0), restic Secret (P0) | Batch |
| vaultwarden-app | Backend | Vaultwarden server container | 1.1-1.4, 2.1-2.2, 3.1-3.3, 4.2, 5.1-5.3, 6.1-6.5 | vaultwarden-db (P0), vaultwarden-secrets (P0), vaultwarden-data (P0), authentik (P0) | API, State |
| vaultwarden-service | Network | K8s Service for internal routing | 3.1 | vaultwarden-app (P0) | API |
| vaultwarden-dns | Infra | Cloudflare DNS CNAME record | 3.1, 3.3 | Cloudflare Tunnel (P0) | — |
| vaultwarden-tunnel | Infra | Cloudflare Tunnel ingress rule | 3.1, 3.2, 3.3 | cloudflared (P0) | API |
| vaultwarden-authentik | Infra | Authentik OIDC Provider + Application for Vaultwarden | 6.1, 6.2, 6.4 | Authentik (P0), Vaultwarden (P0) | API |

### Data Layer

#### vaultwarden-db (CloudNativePG Cluster)

| Field | Detail |
|-------|--------|
| Intent | Vaultwarden アプリケーション用の PostgreSQL クラスターを提供する |
| Requirements | 2.1, 2.3 |

**Responsibilities & Constraints**
- 単一インスタンス（`instances: 1`）。シングルノード構成のためスタンバイ不要。
- `imageName: ghcr.io/cloudnative-pg/postgresql:16.8` を明示指定（`skipEmptyWalArchiveCheck` 認識のため）。
- `storage.size: 5Gi` — Vaultwarden DB は主にメタデータのみなので小容量で十分。
- `backup.barmanObjectStore` で Hetzner Object Storage へ日次フル + WAL アーカイブ。
- `retentionPolicy: "14d"` — 2週間保持（Authentik と同一）。

**Dependencies**
- Inbound: `vaultwarden-app` — SQL 接続 (P0)
- Outbound: `hetzner-os-credentials` Secret — S3 認証情報 (P0)
- External: Hetzner Object Storage — バックアップ先 (P0)

**Contracts**: State

**State Management**
- State model: PostgreSQL データディレクトリ（PVC 上）
- Persistence: CNPG Operator が PVC を管理
- Concurrency: シングルインスタンスのため単一ライター

**Implementation Notes**
- Integration: `vaultwarden-db-rw.prod.svc.cluster.local:5432` に接続
- Validation: CNPG pod が Ready になってから Vaultwarden Deployment を起動（ArgoCD sync 順序で自然に保証される。CNPG Cluster は wave 0 で作成され Pod が Ready になるまで ArgoCD が待機する）
- Risks: なし（既存パターンの再利用）

### Secret Layer

#### vaultwarden-secrets (ExternalSecret)

| Field | Detail |
|-------|--------|
| Intent | Infisical から Vaultwarden アプリケーション設定を K8s Secret へ同期する |
| Requirements | 4.1, 4.2, 5.1, 5.3 |

**Responsibilities & Constraints**
- `refreshInterval: 1h`
- `ClusterSecretStore: infisical` から取得
- 同期するキー（Infisical key → K8s secretKey）:
  - `VAULTWARDEN_ADMIN_TOKEN` → `ADMIN_TOKEN`
  - `VAULTWARDEN_SMTP_PASSWORD` → `SMTP_PASSWORD` （SMTP 設定時に使用）

**Dependencies**
- External: Infisical — Secret Source of Truth (P0)

**Contracts**: State

**Implementation Notes**
- Integration: Deployment で `envFrom.secretRef.name: vaultwarden-secrets` として参照
- Risks: `ADMIN_TOKEN` が空の場合、admin パネルが無効化される。Infisical への事前登録が必須。

#### vaultwarden-db-credentials (ExternalSecret)

| Field | Detail |
|-------|--------|
| Intent | Infisical から DB 接続用認証情報を CNPG 形式で同期する |
| Requirements | 2.1, 4.1 |

**Responsibilities & Constraints**
- `target.template.data` で CNPG 期待形式（`username`, `password`）に整形
- username は固定値 `vaultwarden`

**Dependencies**
- External: Infisical — DB パスワード (P0)

**Contracts**: State

**Implementation Notes**
- Integration: CNPG Cluster `.spec.superuserSecret` または `.spec.bootstrap.initdb.owner` で参照
- Risks: なし（Directus/Authentik と同一パターン）

### Storage Layer

#### vaultwarden-data (PVC)

| Field | Detail |
|-------|--------|
| Intent | Vaultwarden のファイルアタッチメント・Send・組織アイコンを永続化する |
| Requirements | 2.2 |

**Responsibilities & Constraints**
- `accessModes: [ReadWriteOnce]`
- `storageClassName: local-path`（シングルノード構成）
- `resources.requests.storage: 5Gi`
- `DATA_FOLDER=/data` を Deployment の volumeMount でマウント

**Dependencies**
- Inbound: `vaultwarden-app` — ファイル読み書き (P0)
- Outbound: `vaultwarden-backup` — バックアップ対象 (P1)

**Contracts**: State

**Implementation Notes**
- Integration: Deployment で `mountPath: /data` としてマウント
- Risks: ボリューム満杯でアップロード失敗。5Gi 初期設定、監視で追跡。

#### vaultwarden-backup (VolSync ReplicationSource)

| Field | Detail |
|-------|--------|
| Intent | `vaultwarden-data` PVC の内容を Backblaze B2 へ暗号化バックアップする |
| Requirements | 2.4 |

**Responsibilities & Constraints**
- `trigger.schedule: "0 */6 * * *"` — 6時間毎（mailserver と同一）
- `restic` mover 使用
- `copyMethod: Direct`
- `retain.hourly: 12`, `retain.daily: 7`

**Dependencies**
- External: Backblaze B2 — バックアップ先 (P0)
- External: `vaultwarden-restic-secret` — restic リポジトリ認証 (P0)

**Contracts**: Batch

**Batch / Job Contract**
- Trigger: Cron schedule (6h)
- Input: `vaultwarden-data` PVC の内容
- Output: Backblaze B2 restic リポジトリ
- Idempotency: restic による重複排除・増分バックアップ

**Implementation Notes**
- Integration: `replication-source.yaml` は mailserver と同一構造。`sourcePVC` のみ変更。
- Risks: B2 Transaction Cap 超過の過去事例あり（mailserver-backup）。現状は特に問題なし。

### Backend Layer

#### vaultwarden-app (Deployment)

| Field | Detail |
|-------|--------|
| Intent | Vaultwarden サーバーコンテナを実行し、API・Web UI・WebSocket を提供する |
| Requirements | 1.1-1.4, 2.1-2.2, 3.1-3.3, 4.2, 5.1-5.3 |

**Responsibilities & Constraints**
- `replicas: 1`（シングルノード・ステートフル）
- イメージ: `vaultwarden/server:latest`（実装時に固定タグを推奨）
- ポート: `80`（Docker イメージデフォルト）
- `envFrom.secretRef` で `vaultwarden-secrets` から環境変数を読み込む
- 明示的な環境変数:
  - `DATABASE_URL` — PostgreSQL 接続文字列
  - `DATA_FOLDER` — `/data`
  - `WEBSOCKET_ENABLED` — `true`（デフォルトだが明示）
  - `DOMAIN` — `https://vault.aramakisai.com`
  - `SIGNUPS_ALLOWED` — `false`
  - `SIGNUPS_DOMAINS_WHITELIST` — 空（無効化を強制）
  - `ORG_CREATION_USERS` — 管理者メールアドレスのみ（Infisical から注入）
  - `ADMIN_TOKEN` — `vaultwarden-secrets` から注入
  - `SSO_ENABLED` — `true`
  - `SSO_AUTHORITY` — `https://idp.aramakisai.com/application/o/vaultwarden/`
  - `SSO_CLIENT_ID` — `vaultwarden-secrets` から注入
  - `SSO_CLIENT_SECRET` — `vaultwarden-secrets` から注入
  - `SSO_ONLY` — `true`（パスワードログイン無効化）
  - `SSO_SIGNUPS_MATCH_EMAIL` — `true`（既存アカウント自動紐付け）
  - `SSO_PKCE` — `true`
- Probes:
  - `livenessProbe`: HTTP GET `/alive` on port 80
  - `readinessProbe`: HTTP GET `/alive` on port 80

**Dependencies**
- Inbound: `vaultwarden-service` — トラフィック (P0)
- Inbound: `vaultwarden-secrets` — 環境変数 (P0)
- Outbound: `vaultwarden-db` — PostgreSQL (P0)
- Outbound: `vaultwarden-data` PVC — ファイルストレージ (P0)

**Contracts**: API, State

**API Contract**
| Method | Endpoint | Request | Response | Errors |
|--------|----------|---------|----------|--------|
| GET | /alive | — | 200 OK | — |
| Various | /api/* | Bitwarden API | JSON | 4xx/5xx per Bitwarden spec |
| WS | /notifications/hub | WebSocket upgrade | streaming | — |

**State Management**
- State model: Session state はクライアント側。サーバーは DB + ファイルシステムのみ。
- Persistence: PostgreSQL + PVC
- Concurrency: シングルレプリカのため競合は DB レベルで解決

**Implementation Notes**
- Integration: `env` セクションで `DATABASE_URL` を構築。`DB_PASSWORD` は `vaultwarden-db-credentials` Secret から `valueFrom.secretKeyRef` で読み込む。
- Validation: デプロイ後に `/admin` へアクセスし admin パネルが開くことを確認。`SIGNUPS_ALLOWED=false` で登録ページがアクセス不能であることを確認。
- Risks: `latest` タグのままだと意図せずバージョンアップする可能性あり。実装時に `vaultwarden/server:1.33.0` 等へピン留め。

### Network Layer

#### vaultwarden-service (Service)

| Field | Detail |
|-------|--------|
| Intent | Vaultwarden Pod への内部ルーティングを提供する |
| Requirements | 3.1 |

**Responsibilities & Constraints**
- `type: ClusterIP`
- `port: 80`, `targetPort: 80`
- Selector: `app: vaultwarden`

**Contracts**: API

#### vaultwarden-dns / vaultwarden-tunnel (Terraform)

| Field | Detail |
|-------|--------|
| Intent | `vault.aramakisai.com` への外部アクセス経路を提供する |
| Requirements | 3.1, 3.2, 3.3 |

**Responsibilities & Constraints**
- `dns.tf`: `cloudflare_record` for `vault` → CNAME to `local.tunnel_cname`
- `tunnel.tf`: `ingress_rule` for `vault.aramakisai.com` → `http://vaultwarden.prod.svc.cluster.local:80`
- `proxied = true` — Cloudflare Edge で HTTPS 終端 + WebSocket 中継

**Dependencies**
- External: Cloudflare Tunnel（既存）(P0)

**Contracts**: —

**Implementation Notes**
- Integration: Terraform apply で DNS + Tunnel 設定を同時に更新
- Risks: なし（既存サービスと同一パターン）

#### vaultwarden-authentik (Terraform)

| Field | Detail |
|-------|--------|
| Intent | Vaultwarden 用の OIDC Provider + Application を Authentik に定義する |
| Requirements | 6.1, 6.2, 6.4 |

**Responsibilities & Constraints**
- `authentik_provider_oauth2` で Vaultwarden 専用の OIDC Provider を作成
- `client_id`: 固定値 `vaultwarden`
- `client_secret`: `var.vaultwarden_oidc_client_secret`（Terraform variable、Infisical 経由で注入）
- `allowed_redirect_uris`: `https://vault.aramakisai.com/identity/connect/oidc-signin`
- `property_mappings`: `openid`, `email`, `profile`, `groups`（RBAC 用に groups claim を含める）
- `authentik_application` で Vaultwarden アプリを登録

**Dependencies**
- External: Authentik Server — OIDC Provider エンドポイント (P0)
- Inbound: `vaultwarden-app` — SSO 認証リクエスト (P0)

**Contracts**: API

**API Contract**
| Endpoint | Purpose |
|----------|---------|
| `https://idp.aramakisai.com/application/o/vaultwarden/` | OIDC discovery URL (`SSO_AUTHORITY`) |
| `.../authorize` | Authentik 認可エンドポイント |
| `.../token` | Token エンドポイント |
| `.../userinfo` | Userinfo エンドポイント |

**Implementation Notes**
- Integration: `authentik_apps.tf` に Roundcube / ArgoCD / Room Presence と同じ構造で追加
- Validation: `SSO_AUTHORITY/.well-known/openid-configuration` が HTTP 200 + 有効な JSON を返すことを確認
- Risks: Room Presence Provider と同様に `grant_types` が空リストになる可能性あり。作成後に Authentik API で `grant_types` を修正する手順が必要かもしれない（実装時に検証）。

## Data Models

### Domain Model

Vaultwarden のドメインモデルは Bitwarden 互換。本仕様はデプロイ構成のみを定義し、ビジネスロジックの実装は含まない。

- **Authentik Group**: RBAC の真の情報源（SSOT）。人の分類を定義する
- **Organization**: 共有資格情報の論理的グループ（「SNSアカウント」「口座情報」等）
- **Collection**: Organization 内のさらなるグループ化。Authentik Group 名と一致させる
- **Cipher**: 個別の資格情報アイテム（ログイン、カード、ノート等）
- **User**: 個別アカウント。Authentik 経由で SSO ログインし、Organization メンバーシップを通じて Collection にアクセス

### Logical Data Model

**Organization-Collection-User 権限マトリクス**

| 権限レベル | 閲覧 | パスワード表示 | 編集 | 管理 |
|-----------|------|---------------|------|------|
| Can View | Yes | Yes | No | No |
| Can View Except Passwords | Yes | No (auto-fill OK) | No | No |
| Can Edit | Yes | Yes | Yes | No |
| Can Manage | Yes | Yes | Yes | Yes |

- **自然キー**: Organization UUID, Collection UUID, User UUID
- **整合性**: Vaultwarden アプリケーション層で実施（外部キー制約はなし）

### Physical Data Model

**PostgreSQL (CloudNativePG)**

Vaultwarden は内部で Diesel ORM を使用。テーブルはマイグレーションで自動作成される。主要テーブル:

- `users` — ユーザーアカウント
- `organizations` — 組織
- `ciphers` — 資格情報アイテム
- `folders` — 個人フォルダ
- `collections` — 共有コレクション
- `ciphers_collections` — 多対多関連
- `users_organizations` — メンバーシップ
- `users_collections` — コレクション権限
- `attachments` — ファイルメタデータ（実データはファイルシステム）

**インデックス戦略**: Vaultwarden 内部マイグレーションで管理。運用チームは介入不要。

**ファイルシステム (PVC)**

```
/data/
  attachments/
    <uuid>/          # 個別アタッチメントファイル
  sends/
    <uuid>/          # Send 機能の一時ファイル
  icon_cache/
    <domain>.png      # サイト favicon キャッシュ
```

### Data Contracts & Integration

**API Data Transfer**
- Vaultwarden は Bitwarden API と互換。リクエスト/レスポンスは JSON。
- 認証: Bearer token (`access_token`)
- WebSocket: `/notifications/hub` でリアルタイム同期イベントを配信

**Cross-Service Data Management**
- DB バックアップ: CNPG barman → Hetzner Object Storage（別サービスと共有ストレージだが prefix 分離）
- ファイルバックアップ: VolSync restic → Backblaze B2（mailserver と独立リポジトリ）

## Error Handling

### Error Strategy

| Error Type | Pattern | Recovery |
|------------|---------|----------|
| DB connection failure on startup | Vaultwarden retries `DB_CONNECTION_RETRIES` (default 15) with 1s delay | If exceeded, container crash → K8s restart |
| Infisical sync failure | ExternalSecret enters `SecretSyncedError` state | ESO auto-retries; manual check via `kubectl describe externalsecret` |
| PVC full | Write operations fail; Vaultwarden returns 500 | Scale PVC or prune attachments |
| B2 backup failure | VolSync replication job fails | Alert via existing monitoring; manual `ReplicationSource` retry |

### Monitoring

- Vaultwarden `/alive` endpoint を `livenessProbe` / `readinessProbe` で監視
- ログ: stdout/stderr → ノードの container runtime ログ → Grafana Alloy（将来有効化時）
- 追加メトリクス: Vaultwarden は Prometheus metrics を提供しない。必要に応じて node exporter / cadvisor で間接監視。

## Testing Strategy

### Integration Tests
1. `vaultwarden-app` Pod が `vaultwarden-db-rw` に接続できること（ポート5432到達）
2. `vaultwarden-secrets` Secret が ESO によって正しく作成されること
3. Cloudflare Tunnel 経由で `https://vault.aramakisai.com` にアクセスできること
4. `/admin` へ `ADMIN_TOKEN` なしでアクセスできないこと（401/403）
5. `SIGNUPS_ALLOWED=false` 時に `/api/accounts/register` が拒否されること
6. Authentik OIDC discovery URL (`/.well-known/openid-configuration`) が HTTP 200 を返すこと

### E2E Tests
1. Authentik SSO ボタンをクリックし、Authentik 認証後に Vaultwarden に自動ログインできること
2. `SSO_ONLY=true` 時にメール/パスワードログインフォームが非表示であること
3. Organization と Collection を作成し、権限レベル（Can View / Can View Except Passwords / Can Edit）が正しく反映されること
4. アイテムの作成・編集・削除が期待通り動作すること
5. WebSocket 経由で別クライアントへの同期が行われること

### DR / Recovery Tests
1. CNPG `bootstrap.recovery` で B2 から DB を復元できること
2. VolSync `ReplicationDestination` で PVC を復元できること

## Security Considerations

### Threat Modeling

| Threat | Control |
|--------|---------|
| 管理者トークン漏洩 | `ADMIN_TOKEN` は Argon2id ハッシュ推奨。平文ではなく `vw_admin_token_hashed_*` 形式で Infisical に保存。Vaultwarden が自動検証。 |
| DB パスワード漏洩 | ESO 経由で K8s Secret へ注入。マニフェストに平文を記載しない。 |
| 中間者攻撃 | Cloudflare Edge で TLS 終端。Tunnel 内部は HTTP だがノード間は Tailscale 暗号化ネットワーク内。 |
| 不正アクセス（一般登録） | `SIGNUPS_ALLOWED=false` で無効化。`ORG_CREATION_USERS` で組織作成も制限。 |
| ファイルアクセス（他ユーザーの添付ファイル） | Vaultwarden アプリケーションレイヤーでアクセス制御。ファイルシステムは単一 Pod のみがマウント。 |

### Authentication and Authorization

- **ユーザー認証**: Authentik OIDC SSO（`SSO_ONLY=true`）。パスワードログインは無効。
- **管理者認証**: `ADMIN_TOKEN` による Bearer 認証（`/admin`）
- **Collection RBAC**: Vaultwarden Organization/Collection 権限モデルを使用（詳細は Requirements Traceability）
- **SSO**: Authentik OIDC。`SSO_ENABLED=true`、`SSO_AUTHORITY=https://idp.aramakisai.com/application/o/vaultwarden/`。Redirect URI: `https://vault.aramakisai.com/identity/connect/oidc-signin`

### Data Protection

- **暗号化（転送中）**: Cloudflare Edge TLS 1.3
- **暗号化（保存中）**:
  - DB: CNPG 管理の PostgreSQL データ。基盤ストレージは Hetzner NVMe（暗号化はプロバイダー管理）。
  - 添付ファイル: Vaultwarden はアタッチメントを暗号化して保存（Bitwarden 仕様）。
  - バックアップ: barman（gzip）+ restic（暗号化）。

## Performance & Scalability

- **対象ユーザー数**: 実行委員会メンバー数 10 人程度
- **リソース想定**:
  - Vaultwarden: requests 128Mi / 100m, limits 256Mi / 500m
  - CNPG: requests 256Mi / 250m, limits 512Mi / 500m（Directus/Authentik と同一）
- **ボトルネック**: シングルノードのため水平スケール不可。CPU/メモリ不足時はノードアップグレード（Hetzner プラン変更）が必要。
- **キャッシュ**: Vaultwarden は内部で Redis を使用しない。必要に応じて将来検討。

## Supporting References

### Infisical キー一覧（事前登録必須）

| Infisical Key | K8s Secret Key | 用途 |
|---------------|----------------|------|
| `VAULTWARDEN_ADMIN_TOKEN` | `ADMIN_TOKEN` | Admin パネル認証 |
| `VAULTWARDEN_DB_PASSWORD` | `password` (CNPG format) | DB 接続 |
| `VAULTWARDEN_SMTP_PASSWORD` | `SMTP_PASSWORD` | SMTP 送信（オプション） |
| `VAULTWARDEN_ORG_CREATION_USERS` | `ORG_CREATION_USERS` | 組織作成許可ユーザー |
| `VAULTWARDEN_OIDC_CLIENT_ID` | `SSO_CLIENT_ID` | Authentik OIDC client_id |
| `VAULTWARDEN_OIDC_CLIENT_SECRET` | `SSO_CLIENT_SECRET` | Authentik OIDC client_secret |

### Vaultwarden 環境変数リファレンス

| 環境変数 | 値 | 要件 |
|----------|-----|------|
| `DATABASE_URL` | `postgresql://vaultwarden:$(DB_PASSWORD)@vaultwarden-db-rw.prod.svc.cluster.local:5432/vaultwarden` | 2.1 |
| `DATA_FOLDER` | `/data` | 2.2 |
| `WEBSOCKET_ENABLED` | `true` | 3.2 |
| `DOMAIN` | `https://vault.aramakisai.com` | 3.1 |
| `SIGNUPS_ALLOWED` | `false` | 5.1 |
| `SIGNUPS_DOMAINS_WHITELIST` | "" | 5.1 |
| `ADMIN_TOKEN` | From `vaultwarden-secrets` | 5.3 |
| `ORG_CREATION_USERS` | From `vaultwarden-secrets` | 5.2 |

---

*詳細な調査記録は `research.md` を参照。*


