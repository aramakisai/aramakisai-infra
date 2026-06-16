# Requirements Document

## Project Description (Input)

荒牧祭実行委員会インフラの DR（障害復旧）完全自動化。  
Hetzner Cloud 上の single-node K3s クラスター（prod-node-1）が障害になった場合に、  
**人手なし・30分以内**でサービスを復旧する仕組みを構築・整備する。

### 背景

- クラスター構成: cp-node / prod-node-1 / prod-node-2 の3ノード（全ノードがワークロードを受ける）
- DR 対象: prod-node-1 の完全障害（ノード消失）を想定したコールドスタンバイ復旧
- GitOps (ArgoCD) + ESO (Infisical) + CNPG (Hetzner OS WAL) + VolSync (Hetzner OS restic) で状態を外部に保持しているため、ノードを作り直すだけで復旧できる

### 実装済みの内容（2026-06-04 時点）

以下はすでに実装・マージ済みであり、本スペックの対象外：

| 項目 | 状態 | 場所 |
|------|------|------|
| GitHub Actions DR ワークフロー | 実装済み | `.github/workflows/dr-recovery.yml` |
| 復旧スクリプト (recovery.sh) | 実装済み | `.github/scripts/recovery.sh` |
| Raspberry Pi の廃止 | 完了 | `raspberry-pi/` ディレクトリ削除済み |
| DR ランブック | 実装済み | `docs/dr-runbook.md` |
| ArgoCD Authentik OIDC 認証 | 実装済み | `gitops/manifests/prod/argocd/` |
| admin アカウント無効化 | 実装済み | `argocd-cm.yaml` |

### 未実装・残タスク（本スペックの対象）

1. Grafana Cloud アラート設定（2シグナル AND 条件）
2. GitHub Actions Secrets の登録
3. Tailscale tag:ci の設定
4. recovery.sh の手動介入点の自動化（Stalwart / CNPG / infisical-auth）
5. 侵入検知 → 自動破棄・再構築フロー（Falco + intrusion-response ワークフロー）

---

## Requirements

### REQ-01: アラート検知条件

**概要**: Grafana Cloud が障害を検知して GitHub Actions DR ワークフローをトリガーする条件を定義する。

#### REQ-01-1: 2シグナル AND 条件

- **チェック A**: HTTP チェック — `https://idp.aramakisai.com`（Cloudflare Tunnel 経由）
  - 頻度: 1分
  - プローブ拠点: 3拠点以上（Frankfurt / Amsterdam / London 等）
  - アラート条件: `probe_success < 0.5` が **5分間** 継続（過半数の拠点で失敗）
- **チェック B**: TCP チェック — `mail.aramakisai.com:443`（直接 AAAA、CF Tunnel を経由しない）
  - 頻度: 1分
  - プローブ拠点: 同上
  - アラート条件: 同上
- **発報条件**: チェック A AND チェック B の両方がアラート状態になった時点で即時発報

**理由**: チェック A のみでは Cloudflare Tunnel 一時切断・単一アプリクラッシュで偽陽性が発生する。  
`mail.aramakisai.com:443` は proxied=false の直接 AAAA のため、ノードが死んだ場合のみ両方同時に落ちる。

#### REQ-01-2: GitHub Actions トリガー

Grafana Cloud の Contact Point (Webhook) で以下の設定を行う：

| 項目 | 値 |
|------|-----|
| URL | `https://api.github.com/repos/aramakisai/aramakisai-infra/dispatches` |
| HTTP Method | POST |
| Header: Authorization | `Bearer <GITHUB_PAT>` |
| Header: Accept | `application/vnd.github+json` |
| Header: X-GitHub-Api-Version | `2022-11-28` |
| Body | `{"event_type":"dr-recovery"}` |

GITHUB_PAT は Fine-Grained PAT（Actions: write 権限のみ）を使用する。

#### REQ-01-3: 重複発報防止

- GitHub Actions ワークフローの `concurrency: group: dr-recovery` で二重実行を防止（実装済み）
- DR 完了後の再発報防止: ワークフロー完了後に Grafana Cloud でサービス復旧を自動検知することで自然に解消される

---

### REQ-02: GitHub Actions / Tailscale セットアップ

**概要**: DR ワークフローが動作するために必要な外部設定を定義する。

#### REQ-02-1: GitHub Actions Secrets

以下の Secrets をリポジトリの Settings → Secrets and variables → Actions に登録する：

| Secret 名 | 内容 |
|-----------|------|
| `INFISICAL_CLIENT_ID` | Infisical Machine Identity Client ID |
| `INFISICAL_CLIENT_SECRET` | Infisical Machine Identity Client Secret |
| `INFISICAL_PROJECT_ID` | Infisical プロジェクト ID |
| `TS_OAUTH_CLIENT_ID` | Tailscale OAuth Client ID（tag:ci 用・新規作成） |
| `TS_OAUTH_SECRET` | Tailscale OAuth Client Secret（tag:ci 用） |

残りのシークレット（K3S_TOKEN / ARGOCD_GITHUB_DEPLOY_KEY 等）は `infisical run --env=prod --` 経由で自動注入される。

#### REQ-02-2: Tailscale tag:ci

- Tailscale ACL に `tag:ci` タグを追加する
- `tag:ci` 用の OAuth Client（Machines の Create 権限）を新規作成する
- GitHub Actions ランナーはジョブ実行中のみ ephemeral でtailnetに参加し、終了時に自動削除される

---

### REQ-03: recovery.sh の手動介入点の自動化

**概要**: `docs/dr-runbook.md` に記録されている既知の手動介入点を `recovery.sh` 内で自動処理する。

#### REQ-03-1: infisical-auth / Deploy Key の空チェック

- **背景**: 2026-06-02 インシデントで `infisical-auth` と `aramakisai-infra-repo` Secret が空になり、ESO 全停止・ArgoCD git 接続不可になった
- **対応**: recovery.sh のステップ 6（ArgoCD 待機）完了後、以下を自動チェックする
  - `infisical-auth` の `clientId` が空でないこと
  - `aramakisai-infra-repo` の `sshPrivateKey` が空でないこと
- **修復**: いずれかが空の場合は Infisical から再取得して `kubectl apply` で修復し、ESO の ExternalSecret を force-sync する

#### REQ-03-2: Docker Mailserver (DMS) TLS 証明書タイミング問題

- **背景**: DR 後に `cert-manager` の `mail-tls` Certificate が ArgoCD sync タイミングにより未適用になる場合がある
- **対応**: mailserver Pod が `ContainerCreating` のまま 2分以上停止している場合、`mail-tls` の存在を確認し、存在しない場合は手動 apply する
- **対象ファイル**: `certificate.yaml` / `external-secret.yaml` / `restic-external-secret.yaml`

#### REQ-03-3: CNPG Job 残存クリーンアップ

- **背景**: CNPG クラスター削除・再作成を繰り返すと古い `full-recovery` Job が残り、PVC が `initializing` のまま stuck する
- **対応**: recovery.sh の冒頭（Ansible 実行前）に `cnpg.io/cluster` ラベル付きの古い Job を削除する

#### REQ-03-4: Directus DB リストア確認

- **背景**: Directus は Hetzner Object Storage から WAL リストアされる設計になっている
- **対応**: recovery.sh の完了時に Directus の DB レプリカが正常に復元されていることを確認するログを出力する

---

### REQ-04: 侵入検知 → 自動破棄・再構築

**概要**: K8s ランタイム上の異常を検知し、環境を破棄して再構築する "immutable incident response" を実装する。

#### REQ-04-1: Falco によるランタイム検知

- Falco を GitOps (ArgoCD) で `prod` namespace に DaemonSet としてデプロイする
- `falcosidekick` で Falco アラートを GitHub Actions webhook に転送する
- 対象ルール（自動ワークフロートリガー対象）:
  - `Terminal shell in container`（コンテナ内でのシェル起動）
  - `Privilege Escalation`（特権昇格）
  - センシティブファイルへの書き込み（`/etc/passwd` 等）
- 上記以外のルールは Loki アラートのみ（ワークフロートリガーなし）

#### REQ-04-2: intrusion-response ワークフロー

`.github/workflows/intrusion-response.yml` を新規作成する。フロー:

1. **フォレンジック取得**: Pod ログ・Events・NetworkPolicy の状態を GitHub Actions Artifacts に保存（90日保持）
2. **ネットワーク遮断**: 対象 Pod に `NetworkPolicy` を適用して外部通信を即時遮断
3. **承認ゲート**: GitHub Actions Environment `intrusion-response`（Required reviewers 設定）で人間の確認を要求
4. **再構築**: 承認後、`recovery.sh` と同じフローでノードを再構築する

#### REQ-04-3: GitHub Actions Environment 設定

- `intrusion-response` Environment を Settings → Environments に作成する
- Required reviewers にインフラ担当者を設定する
- 承認タイムアウト: 30分（タイムアウト後は自動キャンセル）

#### REQ-04-4: Loki アラートルール

以下を Grafana Cloud の Alert Rules として設定する（Falco 高確度ルール以外の監視）：

| アラート | ログソース | 条件 |
|---------|-----------|------|
| SMTP ブルートフォース | DMS ログ | 1分間に同一 IP から認証失敗 10件以上 |
| Authentik 連続ログイン失敗 | Authentik ログ | 5分間に失敗 5件以上 |
| ArgoCD 不正アクセス試行 | ArgoCD ログ | `invalid session` 連発 |
| NetworkPolicy deny 急増 | Cilium ログ | 直前5分比 300% 増 |

---

### REQ-05: RTO / RPO 目標

| サービス | RPO | RTO | 備考 |
|---------|-----|-----|------|
| Authentik (IdP) | 直前まで | 30分 | CNPG WAL 連続アーカイブ |
| Docker Mailserver DMS (メール) | 最大6時間 | 30分 | VolSync スナップショット間隔 (6h) |
| Directus | 直前まで | 30分 | CNPG WAL 連続アーカイブ |
| Roundcube | ステートレス | 30分 | PVC なし |

---

### 制約・前提条件

- リポジトリは **public** のため、シークレットをマニフェストに直接書かない
- GitHub Actions はパブリックリポジトリのため無料枠（復旧1回≈30分）
- DR は prod-node-1 **単独障害**を対象とする（cp-node / prod-node-2 同時障害はスコープ外）
- infisical-auth Secret は ESO を経由しない唯一の Secret であり、この管理方針は変えない
- Falco / falcosidekick は ArgoCD App of Apps で wave: 0 としてデプロイする
- intrusion-response ワークフローの再構築ステップは `recovery.sh` を直接再利用する（DRY）
