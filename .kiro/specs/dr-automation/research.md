# Research & Design Decisions — dr-automation

---
**Purpose**: Capture discovery findings、アーキテクチャ調査、design.md の根拠となる判断を記録する。
---

## Summary
- **Feature**: `dr-automation`
- **Discovery Scope**: Extension（既存 DR 自動化基盤の拡張）+ Complex Integration（Falco 侵入検知は新規コンポーネント）
- **Key Findings**:
  - `recovery.sh` / `docs/dr-runbook.md` は Stalwart→Docker Mailserver (DMS) 移行 (`65e1b72` 等) に追従しておらず、StatefulSet 名・PVC 名・ReplicationDestination 名が現存しないリソース (`stalwart*`) を指している。現状のまま DR が発火すると Step 7 で確実に失敗する
  - `mail-tls` を発行する cert-manager `Certificate` リソースが gitops リポジトリ全体に存在しない（`autoconfig/certificate.yaml` のみ存在し、mailserver 用は無い）。ドキュメント上は「再 apply すれば直る」想定だが、apply 対象ファイルが実在しない
  - Falcosidekick の `Webhook` 出力は Falco 独自のイベントスキーマを固定で送信し、GitHub `repository_dispatch` が要求する `{"event_type": ...}` 形式に整形できない。直接連携は技術的に不可能
  - Grafana Cloud の Webhook Contact Point は **Custom Payload (動的テンプレート) が GA でない**。既存 dr-recovery 連携も固定 body `{"event_type":"dr-recovery"}` を送るのみで、アラートのラベル値 (対象 Pod 名など) を動的に埋め込めない
  - GitHub Actions の Environment 保護ルールには「N分で承認タイムアウトし自動キャンセル」という設定項目が存在しない（既定 30日固定、変更不可）。30分タイムアウトは自前の watchdog job で実装する必要がある

## Research Log

### recovery.sh / dr-runbook.md と現行 GitOps 構成の差分
- **Context**: REQ-03 の自動化対象を正確に把握するため、`recovery.sh` Step 7 とドキュメントの記述を実際の `gitops/manifests/prod/mailserver/` と突き合わせた
- **Sources Consulted**: `.github/scripts/recovery.sh`、`docs/dr-runbook.md`、`gitops/manifests/prod/mailserver/*.yaml`、`git log --oneline -- .github/scripts/recovery.sh docs/dr-runbook.md`（最終更新がそれぞれ `ab19009`／`6b2bb53` で、`65e1b72`「Stalwart名称参照を整理」より前か無関係)
- **Findings**:
  - 現行 StatefulSet/PVC/Service 名は `mailserver`／`mailserver-data`。VolSync `ReplicationSource` 名は `mailserver-backup`、restic secret 名は `mailserver-restic-secret`
  - `recovery.sh` Step 7 は `stalwart` StatefulSet・`stalwart-data` PVC・`stalwart-restic-secret`・`ReplicationDestination/stalwart-restore` を参照しており、いずれも現存しない
  - バックアップバックエンドも B2 (Backblaze) → Hetzner Object Storage (`hetzner-os-credentials`) に移行済み（`gitops/manifests/shared/eso/b2-external-secret.yaml` は既に削除され `hetzner-os-external-secret.yaml` のみ存在）。`recovery.sh` のコメント「B2 からリストア」は古い
- **Implications**: REQ-03 の実装には、要求された新規自動化（infisical-auth チェック・mail-tls チェック・CNPG Job クリーンアップ・Directus 確認ログ）に加えて、**Step 7 の Stalwart→mailserver 名称修正が前提条件**になる。これを直さない限り新規自動化の実行確認自体ができない

### mail-tls Certificate リソースの欠落
- **Context**: REQ-03-2 (「mail-tls が存在しない場合は手動 apply する」) の対象ファイルを特定するため `kind: Certificate` を全リポジトリで検索
- **Sources Consulted**: `grep -rl "kind: Certificate"`、`gitops/manifests/prod/cert-manager/`、`.kiro/specs/mail-server-migration/design.md`
- **Findings**: `gitops/manifests/prod/autoconfig/certificate.yaml` のみ存在。mailserver 用の `Certificate` CR は gitops に一切なく、`mail-tls` Secret は過去に手動 (`kubectl apply`) で作成されたまま GitOps 化されていない可能性が高い
- **Implications**: REQ-03-2 を自動修復可能にするには、本スペックで `gitops/manifests/prod/mailserver/certificate.yaml`（`letsencrypt-prod` ClusterIssuer + DNS-01 / Cloudflare、既存 `cluster-issuer.yaml` と同方式）を新規追加し ArgoCD 管理下に置く必要がある。これにより「存在しない場合は `kubectl apply -f .../mailserver/certificate.yaml`」が実際に機能するようになる

### Falco → GitHub Actions 連携方式
- **Context**: REQ-04-1 は「falcosidekick で Falco アラートを GitHub Actions webhook に転送する」と記述するが、実現可能性を検証
- **Sources Consulted**: [falcosidekick webhook output docs](https://github.com/falcosecurity/falcosidekick/blob/master/docs/outputs/webhook.md)、[falcosidekick Loki output docs](https://github.com/falcosecurity/falcosidekick/blob/master/docs/outputs/loki.md)
- **Findings**:
  - Webhook 出力の設定項目は `address` / `method` / `customheaders` / `mutualtls` / `checkcert` / `minimumpriority` のみで、送信 JSON の構造は Falco イベントスキーマ固定。GitHub の `repository_dispatch` が要求する `{"event_type": "..."}` 形式へのテンプレート変換機能はない
  - Loki 出力は `rule` / `source` / `priority` / `tags` / `custom_fields` をデフォルトでラベル化し、`loki.extralabels` で追加フィールド（k8s pod/namespace 等）もラベルに昇格できる。ラベルベースの LogQL ストリームセレクタ (`{rule="..."}`) は安価でコスト超過リスクが低い
- **Implications**: Falco → GitHub Actions の経路は、**falcosidekick の Webhook 直叩きではなく、Loki 出力 → Grafana Cloud Loki ベースの Alert Rule → 既存の GitHub dispatches Contact Point（event_type で workflow を判別）** を経由する設計に変更する。これは REQ-04-4 が他のログソース向けに想定している経路と統一でき、実装も既存パターンの再利用で済む

### Grafana Cloud Webhook Contact Point の Custom Payload 非対応
- **Context**: intrusion-response.yml が「どの Pod を遮断・再構築するか」を知る手段を検討
- **Sources Consulted**: [Grafana Cloud webhook notifier docs](https://grafana.com/docs/grafana-cloud/alerting-and-irm/alerting/configure-notifications/manage-contact-points/integrations/webhook-notifier/)、[grafana/grafana#112578](https://github.com/grafana/grafana/issues/112578)
- **Findings**: Custom Payload (動的テンプレートによる body 生成) は Grafana Cloud では GA でない。既存の dr-recovery 連携も固定 body のみを送っている実績と整合する
- **Implications**: GitHub dispatch のペイロードに対象 Pod 名・namespace を動的に含めることは前提にできない。intrusion-response.yml は **トリガー後に自力で Grafana Cloud Loki HTTP API (`/loki/api/v1/query_range`) を再クエリし、直近の高確度 Falco イベントから `k8s.pod.name` / `k8s.ns.name` を抽出する** 設計にする

### GitHub Actions Environment 承認タイムアウトの制約
- **Context**: REQ-04-3「承認タイムアウト: 30分（タイムアウト後は自動キャンセル）」の実現方法を検証
- **Sources Consulted**: GitHub Docs "Reviewing deployments"、community discussions (#5673, #29000, #173147)
- **Findings**: Environment の Required reviewers には既定 30日固定のタイムアウトしかなく、分単位のカスタムタイムアウトは存在しない。`timeout-minutes` は承認待ち時間を含まない（ジョブが実際に実行を開始してから計測される）
- **Implications**: 30分タイムアウトは GitHub 標準機能では実現不可。**承認待ちジョブと並行する watchdog job** を追加し、30分経過後も Pending Deployment が残っていれば `gh api .../actions/runs/{run_id}/cancel` でワークフロー自体をキャンセルする自前実装が必要

### ArgoCD multi-source Helm パターン（既存実装の確認）
- **Context**: Falco を追加する際のマニフェスト構成を既存サービスに揃えるため `authentik.yaml` を確認
- **Sources Consulted**: `gitops/apps/prod/authentik.yaml`、`gitops/apps/prod/cloudnativepg.yaml`
- **Findings**: 外部 Helm chart + 自リポジトリの `helm-values/` + 自リポジトリの `manifests/` を `sources:` で合成する multi-source Application パターンが確立済み（`ref: values` でリポジトリを共有）
- **Implications**: Falco も同パターンで `gitops/apps/prod/falco.yaml`（chart: `falco` from `https://falcosecurity.github.io/charts`）+ `gitops/helm-values/prod/falco.yaml` + `gitops/manifests/prod/falco/`（ExternalSecret 等）として追加する

## Architecture Pattern Evaluation

| Option | Description | Strengths | Risks / Limitations | Notes |
|--------|-------------|-----------|---------------------|-------|
| falcosidekick Webhook 直叩き → GitHub dispatches | falcosidekick の Webhook 出力で直接 GitHub API を呼ぶ | 構成要素が最少 | ペイロード形式が固定で GitHub API 契約を満たせない（技術的に不可能と判明） | 不採用 |
| 専用 relay サービスを新設 | falcosidekick → 自前の小さな変換サービス → GitHub dispatches | ペイロード整形を完全制御できる | 新規常駐サービスの運用・監視・DR 対象が増える（インシデント対応系自体の可用性リスク） | 不採用。低コスト運用方針 (`product.md`) に反する |
| falcosidekick → Loki → Grafana Cloud Alerting → 既存 GitHub dispatches Contact Point | Falco アラートをログとして Loki に送り、ラベルベースの Alert Rule で既存通知経路を再利用 | 既存パターン (REQ-01) の再利用、追加の常駐コンポーネント不要、REQ-04-4 の他ログソースと経路を統一 | Pod/namespace 等の詳細はペイロードに乗らないため workflow 側で再クエリが必要 | **採用** |

## Design Decisions

### Decision: Falco 高確度ルールの通知経路を Loki + Grafana Cloud Alerting に統一する
- **Context**: REQ-04-1 は falcosidekick から GitHub Actions webhook への直接転送を想定していたが、falcosidekick Webhook 出力のペイロードは GitHub API 契約を満たせないことが判明
- **Alternatives Considered**:
  1. falcosidekick Webhook 直叩き — ペイロード不整合のため不可能
  2. 変換用 relay サービスを新設 — 運用負荷増・DR 対象の自己増殖
- **Selected Approach**: falcosidekick の Loki 出力（`rule` ラベル付き）→ Grafana Cloud Loki ベース Alert Rule（高確度3ルールに一致するものは GitHub dispatches Contact Point、それ以外は通知のみ）
- **Rationale**: REQ-01 で確立済みの「Grafana Cloud Alert → 固定 body webhook → GitHub dispatches」パターンをそのまま再利用でき、新規の常駐コンポーネントが不要
- **Trade-offs**: Pod 単位の詳細情報は通知に乗らない（→ 別決定で対応）
- **Follow-up**: `loki.minimumpriority` と `loki.extralabels` の値は実装時に Falco デフォルトルールのノイズ量を見ながら調整する

### Decision: intrusion-response.yml はトリガー後に Loki を再クエリして対象 Pod を特定する
- **Context**: Grafana Cloud Webhook の Custom Payload が GA でないため、対象 Pod 名を GitHub dispatch のペイロードに動的に含められない
- **Alternatives Considered**:
  1. ペイロードに対象 Pod 名を埋め込む — Grafana Cloud の制約上不可能
  2. Falco イベントを別の経路（例: S3/GCS 経由）で先行保存し workflow が読む — 過剰に複雑
- **Selected Approach**: workflow 起動直後に Grafana Cloud Loki HTTP API (`/loki/api/v1/query_range`) を高確度ルール名でクエリし、直近イベントの `k8s.pod.name` / `k8s.ns.name` を抽出してフォレンジック・NetworkPolicy 適用ステップで使う
- **Rationale**: 既存の LOKI_URL/LOKI_USERNAME/LOKI_PASSWORD (Infisical) を読み取り用途に転用でき、新規の常駐コンポーネントが不要
- **Trade-offs**: Loki クエリ用トークンが書き込み専用スコープの場合は読み取りができない可能性がある（Risks 参照）
- **Follow-up**: 実装時に既存 Grafana Cloud Access Policy のスコープに `logs:read` が含まれるか確認する。含まれない場合は新規トークンを発行し `GRAFANA_LOKI_QUERY_TOKEN` として GitHub Actions Secrets に追加する

### Decision: Environment 承認タイムアウトは watchdog job で自前実装する
- **Context**: GitHub Actions の Environment 保護ルールには分単位の承認タイムアウト機能がない
- **Alternatives Considered**:
  1. タイムアウトなしで放置（承認されるまで無期限に Pending）— RTO/セキュリティ対応の趣旨に反する
  2. 外部スケジューラ（cron）で別途キャンセル — 余分な外部システム依存が増える
- **Selected Approach**: 承認待ちジョブと並行して走る `approval-watchdog` ジョブを追加し、30分経過後も対象ジョブが Pending のままであれば `gh api repos/{owner}/{repo}/actions/runs/{run_id}/cancel` でワークフロー run をキャンセルする
- **Rationale**: GitHub Actions の標準機能のみで実現でき、外部依存を増やさない
- **Trade-offs**: watchdog 自体のジョブが30分間 runner を専有する（パブリックリポジトリの無料枠消費が増えるが、侵入対応は低頻度のため許容範囲）
- **Follow-up**: `permissions: actions: write` の付与が必要。watchdog のポーリング間隔・キャンセル条件の正確なロジックはタスク実装時に確定する

### Decision: recovery.sh の Stalwart 参照を mailserver に修正する（新規自動化の前提作業）
- **Context**: REQ-03 で要求された新規自動化を追加する前に、Step 7 が現存しないリソース名を参照しているバグを修正する必要がある
- **Alternatives Considered**:
  1. 新規自動化のみ追加し Stalwart 参照はそのまま残す — Step 7 が必ず失敗するため DR が完結しない
- **Selected Approach**: `stalwart` → `mailserver`、`stalwart-data` → `mailserver-data`、`stalwart-restic-secret` → `mailserver-restic-secret`、`ReplicationDestination/stalwart-restore` → `mailserver-restore` に一括修正し、コメントの「B2 から」も「Hetzner Object Storage から」に修正する
- **Rationale**: REQ-03 の自動化は Step 7 以降が正常に動くことが前提のため、スコープ内の修正として扱う
- **Trade-offs**: なし（既存の不整合の是正）
- **Follow-up**: なし

### Decision: mail-tls Certificate リソースを新規に GitOps 管理下へ追加する
- **Context**: REQ-03-2 が想定する「mail-tls が存在しない場合は手動 apply する」対象ファイルが実在しない
- **Alternatives Considered**:
  1. 対象ファイルなしのまま「存在しなければ警告ログのみ出す」設計にする — RTO 30分以内の自動復旧という目的を満たせない
- **Selected Approach**: `gitops/manifests/prod/mailserver/certificate.yaml` を新規追加し、既存 `letsencrypt-prod` ClusterIssuer + Cloudflare DNS-01 方式で `mail-tls` Secret を発行する `Certificate` CR を定義する
- **Rationale**: recovery.sh の自動 apply が実際に機能するための前提を満たす
- **Trade-offs**: なし
- **Follow-up**: なし

## Risks & Mitigations
- Loki クエリ用トークンのスコープが書き込み専用で読み取りができない — 実装時に Grafana Cloud Access Policy を確認し、不足時は読み取りスコープのトークンを新規発行する
- Falco デフォルトルールのログ量が多く Grafana Cloud Loki のコスト超過を再発させる（過去に Grafana Cloud cardinality 超過でアカウント削除に至った経緯あり） — `loki.minimumpriority` をデフォルトより絞った値に設定し、ラベルベースの安価なクエリのみを Alert Rule に使う
- 単一ノード構成の完全ノード損失時、CNPG Job クリーンアップ (REQ-03-3) を実行する時点で対象クラスター自体が存在せず no-op になる — 失敗を致命的エラーにせず best-effort（非0終了を許容）として実装し、再実行（部分復旧後の再トリガー）時にのみ実効性を持たせる
- NetworkPolicy によるラベル単位の遮断は、将来同じラベルを持つレプリカが複数になった場合に全レプリカを遮断する — 現状すべて単一レプリカ構成のため許容。複数レプリカ化する場合は本スペックの再検証トリガーとする
- intrusion-response の承認 watchdog ジョブのロジック誤りで誤キャンセル/キャンセル漏れが起きる可能性 — タスク実装時にドライランで動作確認する

## References
- [Falcosidekick Webhook output docs](https://github.com/falcosecurity/falcosidekick/blob/master/docs/outputs/webhook.md) — Webhook 出力のペイロードが固定形式であることの根拠
- [Falcosidekick Loki output docs](https://github.com/falcosecurity/falcosidekick/blob/master/docs/outputs/loki.md) — ラベル設計 (`rule`, `extralabels`) の根拠
- [Grafana Cloud webhook notifier docs](https://grafana.com/docs/grafana-cloud/alerting-and-irm/alerting/configure-notifications/manage-contact-points/integrations/webhook-notifier/) — Custom Payload が Cloud で GA でないことの根拠
- [grafana/grafana issue #112578](https://github.com/grafana/grafana/issues/112578) — Custom Payload テンプレートの既知の制約
- GitHub Docs "Reviewing deployments" / community discussions #5673, #29000, #173147 — Environment承認に分単位タイムアウトが存在しないことの根拠
