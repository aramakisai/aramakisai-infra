# Implementation Plan

- [x] 1. 陳腐化コンポーネントの削除とTerraform基盤の整備
- [x] 1.1 停止済みAlloyスタブと孤立kube-state-metricsを削除する
  - `gitops/apps/prod/monitoring.yaml`, `gitops/manifests/shared/monitoring/alloy.yaml`, `gitops/manifests/shared/monitoring/alloy-cluster.yaml`, `gitops/manifests/shared/eso/alloy-external-secret.yaml`, `gitops/apps/prod/kube-state-metrics.yaml` をリポジトリから削除する
  - 削除前に各ArgoCD Applicationの管理対象リソースを確認し、`prune: true` による意図しない削除がないことを確認する
  - main へ push し ArgoCD sync後、`make kubectl ARGS="get applications -n argocd"` に該当Applicationが存在しないこと
  - `make kubectl ARGS="top node"` で削除前との比較によりメモリ使用率が下がっていることを確認する (解放分の実測)
  - _Requirements: 6.1, 6.2, 6.3, 8.1_
  - _Boundary: LegacyMonitoringCleanup_

- [x] 1.2 (P) UptimeRobot/Healthchecks.io/Netdata Cloud用のTerraformプロバイダーとブートストラップ変数を追加する
  - `terraform/providers.tf` に `uptimerobot`/`healthchecksio`/`netdata` の provider block を追加する
  - `terraform/variables.tf` に `uptimerobot_api_key`/`healthchecksio_api_key`/`netdata_api_token` 変数を追加し、`infisical run` 経由の `TF_VAR_*` で注入できる構成にする
  - 各APIキーの初回発行は各SaaSコンソールでの手動操作とし、発行した値はInfisicalに記録する (Requirement 7.3の例外パターン)
  - `infisical run --env=prod -- terraform init` および `terraform plan` がエラーなく完了し、3つの新規providerが認識されること
  - _Requirements: 9.4, 7.3_
  - _Boundary: SaaSTerraformResources_

- [x] 2. Netdataによる軽量メトリクス可視化を導入する
- [x] 2.1 (P) Netdata Cloud Room/ノード割当のTerraformリソースを定義する
  - `terraform/netdata.tf` に `netdata_room`/`netdata_node_room_member` リソースを定義する
  - Room名・対象ノード(prod-node-1)の割当を宣言的に記述する
  - `terraform plan` でリソース作成計画が表示されること
  - _Depends: 1.2_
  - _Requirements: 9.3_
  - _Boundary: SaaSTerraformResources_

- [x] 2.2 (P) ML無効化・メモリ予算内のNetdata childエージェントをHelm Applicationでデプロイする
  - `gitops/apps/prod/netdata.yaml` (sync-wave 0) で `netdata/helmchart` Helm Applicationを作成し、`child`のみ有効化、`parent`/`k8sState`を無効化する
  - `[ml] enabled = no` を明示し、`resources.requests.memory: 64Mi` / `limits.memory: 150Mi` を設定する
  - `gitops/manifests/shared/eso/netdata-external-secret.yaml` で `NETDATA_CLAIM_TOKEN`/`NETDATA_CLAIM_ROOMS` をInfisicalから取得し、`child.envFrom` で注入する (valuesへの平文記載なし)
  - main へ push し ArgoCD sync後、`make kubectl ARGS="top pod -n monitoring"` でNetdata Podのメモリが予算内であること、Netdata Cloud UIでprod-node-1がオンライン表示されること
  - _Depends: 1.1_
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 7.1, 7.2, 8.1_
  - _Boundary: NetdataAgent_

- [x] 3. バックアップのDead Man's Switchを構築する
- [x] 3.1 (P) Healthchecks.io checkのTerraformリソースを定義する
  - `terraform/healthchecksio.tf` に `healthchecksio_check` リソースを定義し、6時間インターバル+grace 1-2時間を設定する
  - Discord連携を設定する場合はHealthchecks.ioコンソールへ `DISCORD_OPS_WEBHOOK_URL` を直接入力し、同値をInfisicalにも記録する (Requirement 7.3の例外)
  - `terraform plan` でcheckリソースの作成計画が表示されること
  - _Depends: 1.2_
  - _Requirements: 5.1, 5.2, 9.2, 9.4, 7.3_
  - _Boundary: HealthchecksIoCheck_

- [x] 3.2 (P) VolSync ReplicationSourceを確認しpingを送信するCronJobを実装する
  - `gitops/manifests/prod/mailserver/backup-healthcheck-rbac.yaml` で専用ServiceAccount+Role(`get`/`list` on `replicationsources.volsync.backube`, namespace `prod`)を作成する
  - `gitops/manifests/prod/mailserver/backup-healthcheck-cronjob.yaml` でVolSyncのバックアップスケジュール完了後にずらして実行するCronJobを定義し、`status.lastSyncTime`/`status.conditions`(Synchronized)が想定インターバル内なら正常時のみHealthchecks.ioへpingする
  - `gitops/manifests/prod/mailserver/healthchecks-external-secret.yaml` で `HEALTHCHECKS_MAILSERVER_BACKUP_PING_URL` をInfisicalから取得しCronJob envへ注入する
  - VolSyncのバックアップ処理自体(ReplicationSource定義)は変更しないこと
  - main へ push し ArgoCD sync後、CronJobの手動Job実行で正常完了し、Healthchecks.io管理画面でping受信が確認できること
  - _Requirements: 5.1, 5.3, 7.1, 7.2, 8.1_
  - _Boundary: MailserverBackupHealthcheck_

- [x] 4. (P) UptimeRobotで公開エンドポイントのMonitorをTerraformリソースとして定義する
  - `terraform/uptimerobot.tf` に `uptimerobot_monitor` リソースを定義し、本体サイト・staging・ArgoCD管理画面・Webmail・Authentik IdP・autoconfigエンドポイント・mail.aramakisai.comのTCP到達性(443)を監視対象に含める
  - `terraform plan` で全Monitorの作成計画が表示されること
  - apply後、UptimeRobot管理画面で各Monitorが登録され `UP` 表示されること
  - _Depends: 1.2_
  - _Requirements: 4.1, 4.2, 4.3, 4.4, 9.1, 9.4_
  - _Boundary: UptimeRobotMonitors_

- [ ] 5. DR自動復旧トリガーをクラスター外部のGitHub Actionsワークフローへ引き継ぐ
- [ ] 5.1 (P) 5分毎スケジュール実行のワークフローを用意し、Tailscale認証情報をGitHub Actions Secretsへミラーする
  - `.github/workflows/dr-trigger.yml` を作成し、`schedule: cron: */5 * * * *` と手動テスト用 `workflow_dispatch` を設定する
  - `permissions: { actions: write, issues: write }` に限定する (不要な権限を付与しない)
  - 既存Infisicalキー `TAILSCALE_API_KEY`/`TAILSCALE_TAILNET` をGitHub Actions Secretsにもミラー登録する
  - `workflow_dispatch` での手動実行が成功し、ワークフローがスクリプト実行まで到達すること
  - _Requirements: 1.8, 1.9, 1.11_
  - _Boundary: DrTriggerWorkflow_

- [ ] 5.2 Tailscaleデバイス状態と複数サービスエンドポイントの複合検出ロジックを実装する
  - `.github/scripts/dr-trigger.sh` にTailscale Devices API (`connectedToControl`)でのprod-node-1オフライン判定を実装する
  - idp/argocd/webmailへのHTTPS到達性確認(タイムアウト+リトライ込み)を実装する
  - (a) Tailscaleオフライン、または(b) 2つ以上のエンドポイントが同時に応答なし、のいずれかでノード障害判定とする複合ロジックを実装する
  - 1エンドポイントのみ応答なしでTailscaleオンラインの場合はノード障害と判定しない分岐を実装する
  - Tailscaleオフラインのモック入力で`NodeFailureSuspected`、1エンドポイントのみダウンのモック入力で`SingleEndpointDown`と判定されることを確認する
  - _Requirements: 1.1, 1.2, 1.3_
  - _Boundary: DrTriggerWorkflow_

- [ ] 5.3 単体障害時とノード障害疑い時のDiscord通知を実装する
  - `SingleEndpointDown`判定時は`repository_dispatch`へ進まずDiscord通知のみ送信する処理を実装する
  - `NodeFailureSuspected`判定時は即座にDiscordへ障害検知通知を送信する処理を実装する
  - 両ケースでDiscord Webhookへの送信が成功することを確認する
  - _Requirements: 1.3, 1.4_
  - _Boundary: DrTriggerWorkflow_

- [ ] 5.4 GitHub Issueによる猶予期間の状態管理(単一インシデント保証・自動発火)を実装する
  - ラベル`dr-incident`のGitHub Issueを使い、既にopenなインシデントが存在する場合は新規作成せず既存Issueの経過時間を評価する処理を実装する
  - 猶予期間(既定10分)内は`repository_dispatch`の発火を保留する処理を実装する
  - 猶予期間経過かつ中止コメントがない場合に自動で`repository_dispatch`(event_type: `dr-recovery`)を発火し、Issueをクローズする処理を実装する
  - 猶予期間経過後は無人でも自動発火が継続することを確認する(夜間想定のモック実行)
  - _Requirements: 1.5, 1.7, 1.8_
  - _Boundary: DrTriggerWorkflow_

- [ ] 5.5 author_associationに基づく運用者の中止操作を実装する
  - Issueへの中止コメントまたはクローズ操作について、`author_association`が`OWNER`/`MEMBER`/`COLLABORATOR`の場合のみ有効とする処理を実装する
  - 中止操作が有効な場合は`repository_dispatch`を発火させずIssueを中止コメント付きでクローズする処理を実装する
  - `author_association`が`NONE`のコメントでは中止操作が無効であることを確認する
  - _Requirements: 1.6_
  - _Boundary: DrTriggerWorkflow_

- [ ] 5.6 dr-runbook.mdとsteering/dr.mdを新しいトリガー方式に合わせて更新する
  - `docs/dr-runbook.md`の「自動復旧フロー」節をGrafana Cloud起点から`dr-trigger.yml`起点の複合検出・猶予期間方式に書き換える
  - `.kiro/steering/dr.md`の「DRの基本方針」内のGrafana Cloud依存記述を新構成に合わせて更新する
  - 更新後の文書にGrafana Cloud Synthetic Monitoringへの依存記述が残っていないこと
  - _Requirements: 1.10_
  - _Boundary: DrTriggerWorkflow_

- [ ] 6. Falcoによるランタイム侵入検知を導入する
- [ ] 6.1 modern_eBPFドライバとメモリ予算override済みのFalco Helm Applicationをデプロイする
  - `gitops/apps/prod/falco.yaml` (sync-wave 0) で `falcosecurity/falco` Helm Applicationを作成する
  - `gitops/helm-values/prod/falco.yaml` で `driver.kind: modern_ebpf` を明示し、`resources.requests: 128Mi/50m`、`limits: 300Mi/500m` にoverrideする
  - 投入前に`make kubectl ARGS="top node"`で200-300Mi以上の空きメモリがあることを確認する
  - main へ push し ArgoCD sync後、起動ログでドライバ種別(modern_ebpf)を確認する
  - _Depends: 1.1, 2.2_
  - _Requirements: 3.1, 3.2, 3.3, 3.6, 3.7, 8.1_
  - _Boundary: FalcoAgent_

- [ ] 6.2 メールサーバー/CNPG/cert-managerの正常処理を除外するカスタムルールを追加する
  - `gitops/helm-values/prod/falco.yaml`の`customRules`値に、`Write below etc`系・`Run shell untrusted`系ルールをnamespace/image/`proc.pname`単位で除外する定義を追加する
  - Terminal shell in container/Privilege Escalation/センシティブファイル書き込みの高確度ルールは除外対象に含めない
  - 投入後1-2日分のアラートで、バックアップ・証明書更新等の正常処理が誤検知されていないことを確認する
  - _Requirements: 3.5_
  - _Boundary: FalcoCustomRules_

- [ ] 6.3 FalcosidekickによるDiscordへのアラート転送を設定する
  - `gitops/helm-values/prod/falco.yaml`で`falcosidekick.enabled: true`にし、`discord.webhookurl`をSecret参照で注入する
  - `gitops/manifests/shared/eso/falcosidekick-external-secret.yaml`で`DISCORD_OPS_WEBHOOK_URL`をInfisicalから取得する(Discord Webhook自体は手動作成、Requirement 9.6の例外)
  - `minimumpriority`をnotice以上に設定しノイズを抑制する
  - `kubectl exec`でのシェル起動が`Terminal shell in container`相当の検知としてDiscordに通知されることを確認する
  - _Requirements: 3.4, 7.1, 7.2, 9.6_
  - _Boundary: FalcosidekickForwarder_

- [ ] 7. Healthchecks.io ping URLとNetdata Room IDのTerraform outputを追加する
  - `terraform/outputs.tf`にHealthchecks.io checkのping URL、Netdata CloudのRoom IDのoutputを追加する
  - `terraform output -raw <name>`で各値が取得できることを確認する
  - 取得した値を`scripts/push-secrets-to-infisical.sh`経由で`HEALTHCHECKS_MAILSERVER_BACKUP_PING_URL`/`NETDATA_CLAIM_ROOMS`としてInfisicalへ反映する運用手順を確認する
  - _Depends: 2.1, 3.1_
  - _Requirements: 9.5_
  - _Boundary: SaaSTerraformResources_

- [ ] 8. ロールアウト全体のメモリ予算とアップサイズ判断基準を検証する
  - 全コンポーネント投入後、`make kubectl ARGS="top node"`でprod-node-1のメモリ使用率を確認する
  - requests合計(kube-state-metrics削除分を含む)がCX33(8GB)容量内に収まっていることを確認する
  - メモリ使用率が90%を超えた場合はHetzner CX33→CX43へのアップサイズ検討を運用者へ提案する判断基準が適用可能な状態になっていることを確認する(アップサイズの実行自体は対象外)
  - _Depends: 1.1, 2.2, 3.2, 6.1_
  - _Requirements: 8.2, 8.3_
