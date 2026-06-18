# Research & Design Decisions Template

## Summary
- **Feature**: `observability-v2`
- **Discovery Scope**: Complex Integration (既存GitOps基盤の拡張 + 複数の新規外部SaaS統合 + 安全性に関わるDR起動ロジックの再設計)
- **Key Findings**:
  - UptimeRobot 無料プランは Webhook/Discord 連携が使えない (Team プラン以上限定、メール通知のみ無料)。さらに 2024-12 以降の ToS で無料枠は非商用・個人利用限定と明記されており、実行委員会という団体利用での適合性はグレーゾーン。→ Requirement 1 (DR起動) のロジックを UptimeRobot に依存させるのは不可能と判明し、別コンポーネントが必要。
  - GitHub の `GITHUB_TOKEN` は `repository_dispatch` / `workflow_dispatch` の発火に関しては例外的に許可されている (2022-09-08 以降)。同一リポジトリ内なら新規 PAT を発行・保管せずに `repository_dispatch` を発火できる。→ Requirement 1 のために新規シークレットを Infisical へ追加する必要がなくなった (Grafana Cloud 時代より秘密情報の管理面が改善)。
  - Tailscale Devices API はオンライン判定に使える `connectedToControl` (bool) を返す。`lastSeen` は `connectedToControl: false` のときのみ含まれる。
  - Falco Helm chart の既定リソースは `requests.memory: 512Mi` / `limits.memory: 1024Mi` と非常に重い。実運用での実測値は概ね 50-70MiB 程度 (ワークロード依存で変動)。単一ノードの RAM 予算上、既定値のまま投入すると即座に予算超過するため明示的な override が必須。
  - Netdata Helm chart はリソース既定値を一切持たない (`resources: {}`)。Claim Token は `child.envFrom` で既存 Secret から注入できる (`claiming.token` に平文を書く必要はない)。Netdata Cloud への接続は `NETDATA_CLAIM_TOKEN` / `NETDATA_CLAIM_ROOMS` (複数形) / `NETDATA_CLAIM_URL` の環境変数で行う。
  - GitHub Actions の scheduled workflow はリポジトリに 60 日間コミット等の活動がないと自動的に無効化される。本リポジトリは活動が継続しているため現時点でのリスクは低いが、設計上のリスクとして記録する。
  - UptimeRobot (`uptimerobot/uptimerobot`, 2026-05-17公開の公式 provider)・Healthchecks.io (`kristofferahl/healthchecksio`, コミュニティ provider)・Netdata Cloud (`netdata/netdata`, 公式 provider、`netdata_room`/`netdata_node_room_member` リソース) はいずれも Terraform provider が存在することが判明。当初の「無料プランは WebUI で十分」という判断 (Non-Goal) を覆し、運用者の知識をコードベース外に分散させない方針 (Requirement 9) のもとで Terraform 管理に切り替える。
  - Discord の Webhook 作成にも `tfstack/discord` 等の provider が存在するが、`discord_webhook` リソースの利用には対象サーバーに `MANAGE_WEBHOOKS` 権限を持つ新規 Bot を追加する必要がある。ユーザーとの確認の結果、本番 Discord サーバーへの新規 Bot 権限追加という攻撃面拡大のコストが、1本の Webhook URL を IaC 化するメリットを上回ると判断し、Discord Webhook 作成は手動例外のまま維持する (Requirement 9.6)。
  - Infisical にも公式 Terraform provider (`Infisical/infisical`, `infisical_secret` リソース) が存在し、Terraform から直接 Infisical へ書き込むことも技術的に可能。しかし書き込み権限を持つ新規 Machine Identity を Terraform の実行コンテキストに追加することになり、既存の ESO 用 Machine Identity (Read 権限のみ) より秘匿性の高い認証情報を増やすことになる。本リポジトリには既に `terraform output` → 手動で Infisical/Ansible へ反映するパターンの前例 (`CLOUDFLARE_TUNNEL_TOKEN`/`CLOUDFLARE_TUNNEL_ID`) があるため、新規の書き込み用認証情報を追加せずこの既存パターンを再利用する方針とした。

## Research Log

### UptimeRobot 無料プランの実際の制約
- **Context**: 当初プランでは UptimeRobot を DR 起動トリガーの監視ソースとしても転用する想定だった (Requirement 1 と Requirement 4 を一体化)。
- **Sources Consulted**: UptimeRobot 公式 Pricing / 2026年時点のレビュー記事複数。
- **Findings**:
  - 無料プラン: 50 monitors、5分間隔、通知は **メールのみ**。Webhook・Discord・Slack・PagerDuty・Zapier は Team プラン ($29/月) 以上が必要。
  - 2024-12-01 以降 ToS 強制適用: 無料プランは「個人・非商用利用」限定。商用利用・団体の業務利用は禁止されており違反時はアカウント停止リスクがある。
- **Implications**: UptimeRobot は Requirement 4 (公開ページの外形監視・メール通知) には使えるが、Requirement 1 (DR起動の複合判定・Discord通知・猶予期間付き発火) のロジックを担わせることはできない。両者を**意図的に分離**し、Requirement 1 は別の専用コンポーネントで実装する設計とした。ToS のグレーゾーンはリスクとして記録し、商用利用と見なされた場合の移行先 (有料プランまたは他SaaS) を将来検討する。

### DR起動トリガーの実装方式 (GitHub Actions vs 新規SaaS/サーバーレス)
- **Context**: Requirement 1 の複合検出 (Tailscale オフライン OR 複数エンドポイント同時ダウン) + 「通知 + 猶予期間オプトアウト」は、状態を保持し定期的に再評価する専用コンポーネントが必要。Cloudflare Workers + KV/Durable Object も代替案として検討した。
- **Sources Consulted**: GitHub Docs (Triggering a workflow / GITHUB_TOKEN)、GitHub Changelog 2022-09-08、Cloudflare Workers Cron Triggers の一般知識。
- **Findings**:
  - `GITHUB_TOKEN` は `repository_dispatch` / `workflow_dispatch` に関して再帰防止ルールの例外であり、同一リポジトリへの dispatch に使える。
  - GitHub Actions の scheduled workflow は最短 5 分間隔。高負荷時に遅延する可能性が公式に明記されているが、本件は RTO 30分という比較的緩いターゲットのため許容範囲。
  - GitHub Issues API はインシデント単位の状態保持 (作成 = 検知開始、コメント/クローズ = 中止操作) として転用できる。追加のステートストア (KV等) が不要。
- **Implications**: 新規プラットフォーム (Cloudflare Workers + TypeScript) を導入せず、既存スタック (GitHub Actions + bash、既存の `recovery.sh` と同じ言語・実行環境) で完結させる方針を採用。秘密情報の追加も最小化される (新規 PAT 不要)。リスクとして「60日非活動での自動無効化」と「schedule 遅延」を記録する (Risks & Mitigations 参照)。

### Tailscale Devices API のオンライン判定方法
- **Context**: Requirement 1 AC1 の「Tailscale上でprod-node-1がオフライン」をAPIでどう判定するか確定させる必要があった。
- **Sources Consulted**: Tailscale 公式 API ドキュメント関連の検索結果、GitHub Issue #7209 (API Device Connection Status)。
- **Findings**: `GET /api/v2/tailnet/{tailnet}/devices` のレスポンスに `connectedToControl` (bool) が含まれ、これが実質的な「オンライン」フラグとして使える。`lastSeen` は `connectedToControl: false` の場合のみ付与される。
- **Implications**: 判定ロジックは「対象デバイスが見つからない、または `connectedToControl: false`」を「オフライン」とする。誤検知debounceとして同一チェック内でリトライ (例: 2回連続) する方式を実装時に検討する (Open Question として design.md に記録)。

### Falco / Falcosidekick のリソースとカスタムルール設定
- **Context**: `.kiro/specs/dr-automation` (未承認) が想定していた Falco 技術選定を継承しつつ、単一ノードの RAM 予算内に収める override 値を決める必要があった。
- **Sources Consulted**: `falcosecurity/charts` リポジトリの `charts/falco/values.yaml` (GitHub)、Falco 公式パフォーマンスドキュメント、falcosecurity/falco Issue #3643 (Bottlerocket メモリリーク報告)。
- **Findings**:
  - Helm chart 既定値: `driver.kind: auto`、`resources.requests.memory: 512Mi` / `limits.memory: 1024Mi`。`falcosidekick.enabled: false` が既定でサブチャートとして同梱可能。
  - `customRules` という values キーでカスタムルールファイル (ConfigMap として生成される) を渡せる。デフォルトルールの一部無効化は `rules: - disable: tag: <tag>` 形式で行う。
  - 実運用での RSS は概ね 50-70MiB と報告されているが、特定 OS (Bottlerocket) の modern-bpf ドライバで既知のメモリリーク報告がある (Falco 0.41.3)。最新版での解消状況は実装時に再確認すべき。
- **Implications**: `driver.kind: modern_ebpf` を明示し、`resources.requests.memory: 128Mi` / `limits.memory: 300Mi` への override を採用 (chart既定より大幅に下げるリスクを取るが、検知用途であり OOMKill 時は再起動で復帰するため致命的ではないと判断)。カスタムルールは `gitops/helm-values/prod/falco.yaml` に分離し、Authentik と同じ「helm-values ファイル分離」パターンに合わせる (既存の `kube-state-metrics.yaml` 式インライン `valuesObject` は今回の値の規模には不向き)。

### Netdata Helm chart の構成方法
- **Context**: 「btop相当」の単純可視化を最小メモリで実現する具体的なチャート構成を確定させる必要があった。
- **Sources Consulted**: `netdata/helmchart` リポジトリ (GitHub) の `charts/netdata/values.yaml`。
- **Findings**:
  - chart はリソース既定値を持たない (`resources: {}`)。明示的に設定する必要がある。
  - parent/child のストリーミング構成は複数ノードを束ねる用途であり、単一ノードでは `child` のみで Netdata Cloud に直接 claim すれば十分。
  - ML 無効化は `[ml] enabled = no` の netdata.conf 相当設定で行う。
  - claim token は `child.envFrom` で既存 Secret を参照させ、values に平文を書かない構成が可能。
- **Implications**: `parent.enabled: false`、`child` のみ (DaemonSet 1台)、`k8sState` (Kubernetesオブジェクト粒度の収集) は明示的に無効化し非目標とする。リソースは `requests: 64Mi/20m`、`limits: 150Mi/200m` を明示する。

### Healthchecks.io / GitHub Actions schedule の運用上の注意点
- **Context**: Dead man's switch の grace period 設計値と、scheduled workflow の運用リスクを確認。
- **Sources Consulted**: Healthchecks.io 公式ドキュメント (Pinging API / Grace Time)、GitHub Community Discussions (60日無効化問題)。
- **Findings**:
  - Healthchecks.io の ping は単純な `curl https://hc-ping.com/<uuid>` で完了。Grace Time は「想定インターバルを超えてどれだけ待つか」を別途設定する。
  - GitHub の scheduled workflow は 60日間リポジトリに活動 (コミット等) がないと自動的に無効化される。タグ作成のみでは活動と見なされない。
- **Implications**: VolSync の 6時間毎スケジュールに対し Grace Time を 1-2時間程度確保する。dr-trigger workflow の 60日無効化リスクは本リポジトリの活動頻度から見て現時点では低いが、Risks に記録し定期的な目視確認を推奨する。

### 外部SaaS設定のTerraform化可否 (UptimeRobot / Healthchecks.io / Netdata Cloud / Discord)
- **Context**: ユーザーから「管理者がコードベース外に出る必要をなくし、知識を分散させない。引き継ぎ後の運営チームは現在の運用者ほどの知識を持つ保証がないため、全情報をコードベースで管理できるようにすべき」という方針が明示された。当初 design.md の Non-Goals では「UptimeRobot の Terraform 化はコストに見合わない」としていたが、この前提を再検証する必要が生じた。
- **Sources Consulted**: Terraform Registry (`uptimerobot/uptimerobot`, `kristofferahl/healthchecksio`, `netdata/netdata`, `tfstack/discord`, `Infisical/infisical`)。
- **Findings**:
  - UptimeRobot: 2026-05-17 公開の **公式** provider `uptimerobot/uptimerobot` (v1.7.1, ダウンロード20万超) が存在し、`uptimerobot_monitor` リソースで Monitor を宣言的に管理できる。API キーは無料プランでも発行可能。
  - Healthchecks.io: `kristofferahl/healthchecksio` (コミュニティ provider) が check の作成・更新・削除をサポート。
  - Netdata Cloud: **公式** provider `netdata/netdata` が `netdata_room` (Room作成) と `netdata_node_room_member` (ノードのRoom割当) リソースを提供。認証は `scope:all` の API トークンが必要。ただし公式ドキュメント上 "Netdata Cloud is not currently exposing a stable API" との注記があり、将来の破壊的変更リスクは残る。
  - Discord: `tfstack/discord` 等に `discord_webhook` リソースがあるが、利用には `MANAGE_WEBHOOKS` 権限を持つ Bot をサーバーに追加する必要がある。
  - Infisical: 公式 provider `Infisical/infisical` に `infisical_secret` リソースがあり、Terraform から直接 Infisical へ書き込み可能。
- **Implications**: UptimeRobot/Healthchecks.io/Netdata Cloud Room は Terraform 管理に切り替える (Requirement 9.1-9.3)。Discord Webhook 作成は Bot権限拡大のコストがメリットを上回るとユーザーと確認の上、手動例外として維持 (Requirement 9.6)。Infisicalへの書き込みは新規Terraform providerを追加せず、既存の `terraform output` → 手動反映パターンを再利用する (Requirement 9.5)。

## Architecture Pattern Evaluation

| Option | Description | Strengths | Risks / Limitations | Notes |
|--------|-------------|-----------|---------------------|-------|
| GitHub Actions scheduled workflow + GitHub Issues (採用) | cron で定期チェックし、Issue を状態機械として使う | 既存スタックのみで完結、新規秘密情報・新規プラットフォーム不要、`recovery.sh` と同じ運用パターン | 60日非活動時の自動無効化、schedule遅延の可能性、Issueがpublicリポジトリ上で可視 | 採用 |
| Cloudflare Workers + KV/Durable Object | Cron Trigger で定期チェックし、KVで猶予期間状態を保持 | 高精度なタイマー、既存Cloudflareアカウントの延長 | 新規TypeScript/Wranglerツールチェイン導入、新規デプロイ経路、本リポジトリに前例なし | 不採用 (複雑性増加が要件に対して過大) |
| UptimeRobot Webhook 経由で外部関数を駆動 | UptimeRobotのアラートをWebhookで中継 | 監視ロジック自体は外部SaaSに委譲 | 無料プランでWebhook不可と判明、有料化が必要 | 不採用 (調査で判明した制約により except) |

## Design Decisions

### Decision: DR起動トリガーは GitHub Actions の新規 scheduled workflow として実装する
- **Context**: Requirement 1 の複合検出・通知・猶予期間オプトアウトには状態保持と定期実行が必要。
- **Alternatives Considered**:
  1. Cloudflare Workers + KV — 新規プラットフォーム導入のコストが大きい
  2. UptimeRobot Webhook 中継 — 無料プランでは技術的に不可能と判明
- **Selected Approach**: `.github/workflows/dr-trigger.yml` (5分毎 cron) + `.github/scripts/dr-trigger.sh` が Tailscale API・公開エンドポイント・GitHub Issues・Discord Webhook・`repository_dispatch` を直接操作する。
- **Rationale**: 既存の `recovery.sh` と同じ言語・実行環境・秘密情報管理パターンを再利用でき、レビュー・運用コストが最小。`GITHUB_TOKEN` の例外的な `repository_dispatch` 許可により新規 PAT 管理も不要。
- **Trade-offs**: schedule 遅延・60日無効化という GitHub Actions 固有のリスクを受け入れる。Issue が public リポジトリ上で可視になる (低リスクと判断)。
- **Follow-up**: 実装時に `author_association` チェック (中止操作の権限制限) を必ず入れること。60日無効化は定期的な目視確認で対応。

### Decision: UptimeRobot (Requirement 4) と DR起動トリガー (Requirement 1) を完全に分離する
- **Context**: 当初案は両者を一体化する想定だったが、UptimeRobot 無料プランの制約調査により技術的に不可能と判明。
- **Selected Approach**: UptimeRobot は人間向けの一般的な外形監視 (メール通知) のみを担当。DR起動の判定ロジックは dr-trigger workflow が独自に idp/argocd/webmail へ直接アクセスして判定する (UptimeRobotの結果に依存しない)。
- **Rationale**: 安全性に関わる自動復旧トリガーを、信頼性の保証がない無料SaaSのWebhook配送に依存させないほうが堅牢。責務も明確に分かれる。
- **Trade-offs**: 監視対象の重複 (idp/argocd/webmailは両方からチェックされる) が発生するが、無料・低コストであり問題にならない。

### Decision: Discord Webhook を用途別に分けず1本の運用通知用Webhookに集約する
- **Context**: Falcosidekick・Healthchecks.io・dr-trigger workflow のいずれもDiscord通知が必要。
- **Selected Approach**: `DISCORD_OPS_WEBHOOK_URL` という単一のInfisicalキー/Discord Webhookを共有し、各コンポーネントから利用する。
- **Rationale**: 秘密情報の数を最小化 (Requirement 7 のゼロ秘密漏洩方針に整合)。チャンネル分離が必要になった場合は容易に分割できる。
- **Trade-offs**: 通知の発生源がチャンネル上で混在する。メッセージ本文に発生源 (Falco/Healthchecks/DR Trigger) を明記することで運用上は十分判別可能。

### Decision: Falco/Falcosidekick の Helm values は `gitops/helm-values/prod/falco.yaml` に分離する
- **Context**: カスタムルールを含む values は行数が多くなり、`kube-state-metrics.yaml` 式のインライン `valuesObject` には不向き。
- **Selected Approach**: Authentik と同じ「Helm chart + 別ファイルの helm-values + 複数 source の ArgoCD Application」パターンを採用。
- **Rationale**: 既存パターンの再利用。レビュー時の可読性確保。

### Decision: UptimeRobot / Healthchecks.io / Netdata Cloud Room の設定を Terraform 管理に切り替える
- **Context**: 「運用者の知識をコードベース外に分散させない」というユーザーの方針に基づき、各SaaSの公式/メンテ済み Terraform provider の存在を確認した。
- **Alternatives Considered**:
  1. 当初案 (WebUI 手動設定) — 設定知識が各SaaSの管理画面操作という形で運用者の頭の中とWebUI上に閉じ、コードベースだけでは再現できない
  2. Terraform管理 + Infisical provider による自動書き込み — 新規の書き込み可能なInfisical認証情報が必要になり、秘匿性の高い認証情報が増える
- **Selected Approach**: `terraform/uptimerobot.tf` / `terraform/healthchecksio.tf` / `terraform/netdata.tf` を新規作成し、`uptimerobot_monitor` / `healthchecksio_check` / `netdata_room` + `netdata_node_room_member` リソースで宣言的に管理する。各SaaSのAPIキー (`UPTIMEROBOT_API_KEY`, `HEALTHCHECKSIO_API_KEY`, `NETDATA_CLOUD_API_TOKEN`) はInfisicalにブートストラップ認証情報として記録し、`TF_VAR_*`で注入する。Terraformが生成する値 (Healthchecks.ioのping URL、NetdataのRoom ID) は既存の `terraform output` → Infisical手動反映パターン (`CLOUDFLARE_TUNNEL_TOKEN`/`ID`と同様) で連携する。
- **Rationale**: 既存のTerraformプロバイダー管理パターン (Hetzner/Cloudflare/Tailscale/Authentik) と一貫する。新規の書き込み可能Infisical認証情報を追加せずに済む。
- **Trade-offs**: Netdata Cloud providerは「stable APIではない」という公式の注記があり、将来のprovider側破壊的変更リスクを受け入れる。Discord Webhookのみ手動例外として残る (Bot権限拡大のコストが上回るため)。

## Risks & Mitigations
- GitHub Actions scheduled workflow が 60日間のリポジトリ非活動で自動無効化される — 本リポジトリは継続的に開発中のため現状リスクは低いが、無効化された場合はDR起動トリガーがサイレントに停止する。定期的に Actions タブで scheduled workflow の有効性を確認する運用を推奨 (本スペックでは自動化しない)。
- Falco の modern eBPF ドライバにメモリリーク報告例がある (特定OS向け、本クラスタのDebian 13条件下での再現性は未確認) — limits 到達時は Pod 再起動で復帰する設計のため検知の一時欠落は許容するが、頻発する場合は limits 引き上げまたはバージョン固定を検討する。
- UptimeRobot 無料プランの「個人・非商用利用限定」ToSと実行委員会という団体利用の適合性が不明瞭 — 違反と判定された場合はアカウント停止リスクがある。低コストでの継続利用を優先しつつ、ToS変更や指摘があれば有料プランまたは他SaaSへの移行を検討する。
- DR起動トリガーの Issue ベース中止操作は public リポジトリ上で誰でもコメント可能 — `author_association` (OWNER/MEMBER/COLLABORATOR) によるフィルタを実装で必須とする。フィルタなしで実装された場合、第三者が中止操作を偽装できてしまう。
- `recovery.sh` / `dr-recovery.yml` 自体は一度も実行実績がない (既知のプロジェクトメモリ事項) — 本スペックの完了後、実運用開始前に一度通しでの検証実行を推奨する。
- Netdata Cloud provider は公式ドキュメント上「stable APIではない」と明記されている — provider側の将来の破壊的変更でterraform applyが失敗する可能性がある。発生時はRoom/ノード割当のみ一時的に手動運用に戻すフォールバックを検討する。
- Healthchecks.io provider (`kristofferahl/healthchecksio`) はコミュニティメンテのため、公式providerに比べ更新頻度・対応の保証が低い — 致命的な不具合があれば手動運用への切り戻しが可能な設計 (Terraformリソース削除してもHealthchecks.io側のcheck自体は残る) であることを実装時に確認する。

## References
- [falco 3.1.1 · helm/falcosecurity](https://artifacthub.io/packages/helm/falcosecurity/falco/3.1.1) — Falco Helm chart の基本情報
- [charts/charts/falco/values.yaml at master · falcosecurity/charts](https://github.com/falcosecurity/charts/blob/master/charts/falco/values.yaml) — driver.kind / resources / customRules / falcosidekick subchart の既定値
- [falcosidekick/docs/outputs/discord.md](https://github.com/falcosecurity/falcosidekick/blob/master/docs/outputs/discord.md) — Discord output の webhookurl / minimumpriority
- [Falco Performance docs](https://falco.org/docs/troubleshooting/performance/) — 実運用メモリ使用量の目安
- [Memory leak in modern-bpf driver on Bottlerocket OS · Issue #3643](https://github.com/falcosecurity/falco/issues/3643) — modern eBPF ドライバの既知のメモリ問題
- [netdata/helmchart values.yaml](https://github.com/netdata/helmchart/blob/master/charts/netdata/values.yaml) — child.envFrom によるClaim Token注入、ml.enabled設定、resources既定値なし
- [healthchecks.io Pinging API](https://healthchecks.io/docs/http_api/) — ping APIの利用方法
- [healthchecks.io Monitoring Cron Jobs](https://healthchecks.io/docs/monitoring_cron_jobs/) — Grace Timeの考え方
- [UptimeRobot Free Plan Limits in 2026](https://stillup.org/blog/uptimerobot-free-plan-limits) — 無料プランの監視数・間隔
- [UptimeRobot Free Plan in 2026: The Limits That'll Actually Bite You](https://dev.to/r0tten0x/uptimerobot-free-plan-in-2026-the-limits-thatll-actually-bite-you-445g) — Webhook/Discordが有料プラン限定、ToS非商用限定の指摘
- [GitHub Actions: Use the GITHUB_TOKEN with workflow_dispatch and repository_dispatch](https://github.blog/changelog/2022-09-08-github-actions-use-github_token-with-workflow_dispatch-and-repository_dispatch/) — GITHUB_TOKENでのdispatch許可
- [Triggering a workflow - GitHub Docs](https://docs.github.com/actions/using-workflows/triggering-a-workflow) — トリガー全般の仕様
- [Workflows disabled after 60 days of inactivity discussion](https://github.com/orgs/community/discussions/57858) — scheduled workflowの自動無効化条件
- [uptimerobot Provider - Terraform Registry](https://registry.terraform.io/providers/uptimerobot/uptimerobot/latest/docs) — 公式UptimeRobot provider、`uptimerobot_monitor`リソース
- [Introducing UptimeRobot's official Terraform provider](https://uptimerobot.com/blog/uptimerobot-terraform-provider-release/) — 公式provider発表記事
- [kristofferahl/terraform-provider-healthchecksio](https://github.com/kristofferahl/terraform-provider-healthchecksio) — Healthchecks.io コミュニティprovider
- [netdata Provider - Terraform Registry](https://registry.terraform.io/providers/netdata/netdata/latest/docs) — 公式Netdata Cloud provider、`netdata_room`/`netdata_node_room_member`リソース、API tokenはscope:all必要
- [tfstack/discord - Terraform Registry](https://github.com/tfstack/terraform-provider-discord) — Discord webhook管理provider (Bot権限が必要なため本スペックでは不採用)
- [Infisical/infisical - Terraform Registry](https://registry.terraform.io/providers/Infisical/infisical/latest/docs/resources/secret) — `infisical_secret`リソース (書き込み認証情報増加のため本スペックでは不採用、既存のterraform output→手動反映パターンを維持)
