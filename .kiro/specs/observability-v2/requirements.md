# Requirements Document

## Project Description (Input)
Grafana Cloud廃止(無料枠超過$977によりアカウント削除依頼中)に伴う、単一ノードK3sクラスタ(Hetzner CX33, 2vCPU/8GB)向けの軽量監視・侵入検知・外形監視の再構築。要件: (1) メトリクスはGrafanaフル機能ではなくbtop程度の単純な可視化で十分、Netdata(ML無効)+Netdata Cloud無料枠で実現。(2) Falco(modern eBPF driver)によるランタイム侵入検知、Falcosidekick経由でDiscord Webhookに通知(自動破棄ワークフローは別スペックdr-automation側のスコープとして分離、今回は検知・通知のみ)。(3) クラスター外部のSaaSによる外形監視: UptimeRobot(無料枠、公開ページのHTTP/TCP死活監視)とHealthchecks.io(無料枠、VolSyncバックアップのdead man's switch)を導入し、クラスター自体が落ちても監視が共倒れしない構成にする。ログ収集は低優先度のため今回は対象外(将来VictoriaLogs検討)。既存の孤立コンポーネント(kube-state-metrics)と陳腐化したAlloyスタブ(gitops/manifests/shared/monitoring/alloy.yaml, alloy-cluster.yaml, gitops/apps/prod/monitoring.yaml, kube-state-metrics.yaml, alloy-external-secret.yaml)は削除する。新規シークレット(NETDATA_CLAIM_TOKEN, NETDATA_CLAIM_ROOM, FALCOSIDEKICK_DISCORD_WEBHOOK, HEALTHCHECKS_MAILSERVER_BACKUP_PING_URL)はInfisical経由ExternalSecretパターンに従う。RAM予算がタイト(現状82%使用済み)なため、kube-state-metrics削除→Netdata→Healthchecks CronJob→UptimeRobot→Falcoの順で段階投入し、都度top nodeで確認する。

## Introduction

Grafana Cloud は無料枠超過によりアカウント削除手続き中で、Grafana Alloy (メトリクス/ログ収集) は既に DaemonSet/Deployment 本体を停止済み。現在クラスターには稼働中の監視・アラート・侵入検知が一切存在しない。

**調査で判明した重大な副次的影響**: `docs/dr-runbook.md` および `.github/workflows/dr-recovery.yml` によれば、単一ノード障害時の無人自動復旧フロー (DR) は **Grafana Cloud Synthetic Monitoring が `idp.aramakisai.com` の応答なしを検知し、Contact Point Webhook が GitHub Actions の `repository_dispatch` (`dr-recovery`) をトリガーする** ことを起点としている。Grafana Cloud 解約に伴い、**この自動復旧トリガーは現在機能していない (サイレントに壊れている)**。したがって本スペックは単なる「監視の好みの置き換え」ではなく、**DR自動復旧の生命線を引き継ぐ復旧作業を含む**。

本スペックでは、Grafana (フル機能ダッシュボード) を必要としない軽量なメトリクス可視化、Falco によるランタイム侵入検知、クラスター外部 SaaS による外形監視・dead man's switch (DR自動復旧トリガーの引き継ぎを含む) を構築し、既存の陳腐化済みコンポーネントを整理する。

## Boundary Context

- **In scope**: 軽量メトリクス可視化、ランタイム侵入検知 (検知・通知のみ)、外部SaaSによる外形監視 (DR自動復旧トリガーの引き継ぎを含む)、バックアップのdead man's switch、陳腐化コンポーネント (Alloy stub, 孤立 kube-state-metrics) の削除、新規シークレットの Infisical/ESO パターンへの統合、外部SaaS設定 (UptimeRobot/Healthchecks.io/Netdata Cloud Room) のTerraformによるコード化 (運用者の知識をコードベース外に分散させず、引き継ぎ後の運営チームが再現できる状態を保つため)
- **Out of scope**: ログ収集基盤の本格導入 (VictoriaLogs等は将来検討)、侵入検知から自動破棄・再構築への自動化ワークフロー (`.kiro/specs/dr-automation` の REQ-04-2/REQ-04-3 のスコープのまま)、Hetzner ノードのアップサイズ実行そのもの (判断基準のみ本スペックで定義し、実行判断は運用時に行う)
- **Adjacent expectations**: `.kiro/specs/dr-automation` (未承認, Falco導入を既に構想済みだが3ノード前提・Grafana Cloud前提で陳腐化) とは技術選定 (Falco+falcosidekick) を共有するため後日整合を取る必要がある。`.kiro/specs/monitoring` (Alloy構成, completed) は本スペックの実質的な後継であり、本スペック完了後に状態が食い違ったまま残る点を別途整理する必要がある。

## Requirements

### Requirement 1: DR自動復旧トリガーの引き継ぎ
**Objective:** As 運用者, I want クラスター外部の死活監視サービスが既存の GitHub Actions DR復旧ワークフローを誤検知に強い形で引き継ぐこと, so that Grafana Cloud解約後もコールドスタンバイ自動復旧が機能し続け、かつ単体サービス障害で無意味なノード再構築を起こさない

**設計判断1 (検出ロジックの誤り修正)**: 旧構成は `idp.aramakisai.com` 単体の応答なしのみを判定条件としていた。これはAuthentikアプリ単体のクラッシュ (ノード自体は健全) でも誤って「ノード障害」と判定し、不要なノード再作成を引き起こす欠陥がある。本スペックでは **(a) Tailscale上でprod-node-1がオフライン (VM/OSレベルの障害を示す高確信度シグナル) または (b) 複数の独立したサービス (idp/argocd/webmail等、別namespace・別Podで稼働) が同時に応答なし (K3s基盤レベルの障害を示すシグナル)** のいずれかを満たす場合のみ「ノード障害」と判定する複合検出ロジックを採用する。idp単体のみの障害はノード障害と判定せず、Discord通知のみ行い人間の判断 (アプリ再起動等) に委ねる。

**設計判断2 (誤検知時の安全弁)**: 上記の複合検出でも誤検知の可能性は残るため、検知から即時に破壊的操作 (`repository_dispatch`) を発火するのではなく、**「通知 + 猶予期間オプトアウト」方式** を採用する。障害検知時はまずDiscordに即時通知し、一定の猶予期間 (目安10分、RTO 30分目標を圧迫しない範囲) 内に運用者が中止しない限り、猶予期間終了後に自動で発火する。無人時 (夜間等) はRTO目標を維持し、運用者が在席している場合は誤検知を止める手段を持つ。

**既知のリスク (本要件のスコープ外だが記録)**: `recovery.sh` / `dr-recovery.yml` 自体は一度も実行された実績がない (2026-06-17 ユーザー確認)。設計上RTO 30分を目指しているが、実際の障害時に想定外の失敗が起こる可能性は未検証。本スペックの完了後、実運用開始前に一度通しでの検証実行を行うことを推奨する。

#### Acceptance Criteria
1. While Tailscale上でprod-node-1がオフラインと判定される場合, DR復旧トリガー連携コンポーネントはノード障害と判定しなければならない
2. While Tailscale上でprod-node-1がオンラインのまま, 複数の独立したサービスエンドポイント (idp/argocd/webmail等) が同時に応答なしの場合, DR復旧トリガー連携コンポーネントはノード障害と判定しなければならない
3. While いずれか1つのサービスエンドポインドのみが応答なしで、Tailscale上でprod-node-1がオンラインの場合, DR復旧トリガー連携コンポーネントはノード障害と判定してはならず、Discord通知のみを送信しなければならない
4. When ノード障害と判定した場合, DR復旧トリガー連携コンポーネントは即座にDiscordへ障害検知通知を送信しなければならない
5. When 障害検知通知が送信された場合, DR復旧トリガー連携コンポーネントは猶予期間 (目安10分、設計フェーズで確定) を設けて `repository_dispatch` (event_type: `dr-recovery`) の発火を保留しなければならない
6. If 猶予期間中に運用者が中止操作を行った場合, DR復旧トリガー連携コンポーネントは `repository_dispatch` を発火させてはならない
7. If 猶予期間が経過しても運用者からの中止操作がない場合, DR復旧トリガー連携コンポーネントは自動で `repository_dispatch` (event_type: `dr-recovery`) を発火させなければならない
8. While 運用者が長時間不在 (夜間等) の場合でも, DR復旧トリガー連携コンポーネントは猶予期間終了後に人手を介さず自動復旧を継続できなければならない
9. The DR復旧トリガー連携コンポーネントおよび外形監視サービスはGrafana Cloudの機能やアカウントに依存してはならない
10. When 外形監視サービスの設定変更 (監視対象・閾値・通知先・猶予期間) が必要になった場合, 運用者は `docs/dr-runbook.md` の「自動復旧フロー」記述を新しい構成に合わせて更新できなければならない
11. If GitHubへのdispatchに使う認証情報 (Fine-Grained PAT等) が外部SaaSまたは中継コンポーネントの管理画面に直接入力される場合, その認証情報はリポジトリ内のいかなるファイルにも平文で含まれてはならない

### Requirement 2: 軽量メトリクス可視化
**Objective:** As 運用者, I want btop相当の即時ノード/コンテナリソース状況をクラスター外から確認できること, so that Grafanaのような重量級ダッシュボードを自前運用せずに状態を把握できる

#### Acceptance Criteria
1. The メトリクス収集エージェントはノードのCPU・メモリ・ディスク・ネットワーク使用率を収集しなければならない
2. The メトリクス収集エージェントが収集したデータは、自前でダッシュボードサーバーを運用せずにクラスター外のSaaS経由で閲覧できなければならない
3. The メトリクス収集エージェントはMLベースの異常検知機能を無効化した状態で動作しなければならない
4. While prod-node-1のメモリ使用率が既存ワークロードにより既に高水準 (実測82%程度) にある場合, メトリクス収集エージェントは requests 64Mi / limits 150Mi 以内のメモリ予算で動作しなければならない
5. If メトリクス収集エージェントがメモリlimitを超過した場合, システムは当該エージェントのみを再起動させ、既存のprodワークロードには影響を与えてはならない

### Requirement 3: ランタイム侵入検知
**Objective:** As 運用者, I want コンテナ/ホスト上の異常な振る舞いを検知して通知を受け取ること, so that 侵入や異常動作に早期対応できる

#### Acceptance Criteria
1. When コンテナ内で想定外のシェルが起動された場合, 侵入検知システムはアラートを発生させなければならない
2. When 特権昇格に相当する操作が検知された場合, 侵入検知システムはアラートを発生させなければならない
3. When センシティブファイルへの書き込みアクセスが検知された場合, 侵入検知システムはアラートを発生させなければならない
4. When 侵入検知システムがアラートを発生させた場合, 通知転送コンポーネントはDiscord Webhook経由で運用者に通知を送信しなければならない
5. While メールサーバーおよびデータベース運用上の正常な定期処理 (バックアップ・証明書更新等) が実行されている場合, 侵入検知システムはそれらを誤検知としてアラートしてはならない
6. The 侵入検知システムが稼働するノードのカーネルはeBPFベースの検知方式に対応していなければならない
7. 侵入検知から自動破棄・再構築への連携は本要件のスコープ外とする (`.kiro/specs/dr-automation` のスコープに残す)

### Requirement 4: 外部死活監視 (公開ページ)
**Objective:** As 運用者, I want 公開サービスの到達性をクラスター外部から定期的に確認すること, so that クラスター障害時にも気づける (共倒れを避ける)

#### Acceptance Criteria
1. The 外形監視サービスは荒牧祭情報基盤の主要な公開エンドポイント (本体サイト、staging、ArgoCD管理画面、Webmail、Authentik IdP、autoconfigエンドポイント、メールサーバーのTCP到達性) を定期的に監視しなければならない
2. While クラスター自体が完全に停止している場合でも, 外形監視サービスは正常に監視・通知を継続できなければならない
3. When 監視対象エンドポイントが一定時間応答しない状態になった場合, 外形監視サービスは運用者に通知を送信しなければならない
4. The 外形監視サービスはクラスター内のいかなるコンポーネントにも実行を依存してはならない

### Requirement 5: バックアップのDead Man's Switch
**Objective:** As 運用者, I want VolSyncによるmailserverバックアップが定期的に正常完了していることを確認できること, so that バックアップ失敗に気づかずデータロスリスクを抱え続ける事態を防ぐ

#### Acceptance Criteria
1. When `mailserver-backup` ReplicationSourceのバックアップが正常完了した場合, 確認用ジョブはdead man's switchサービスへ生存通知を送信しなければならない
2. While `mailserver-backup` の最終同期時刻が想定インターバルとgrace期間を超えて更新されていない場合, dead man's switchサービスはアラートを発生させなければならない
3. The 確認用ジョブはVolSyncのバックアップ処理自体を変更してはならない (疎結合な監視のみを追加する)

### Requirement 6: 陳腐化コンポーネントの整理
**Objective:** As 運用者, I want 使われていないAlloy関連リソースと孤立したkube-state-metricsを削除すること, so that クラスターリソースを浪費せず構成をシンプルに保てる

#### Acceptance Criteria
1. The システムはGrafana Alloyの停止済みリソース (ConfigMap・RBAC・ArgoCD Application・対応するExternalSecretを含む) を削除しなければならない
2. The システムは利用者のいなくなったkube-state-metricsコンポーネントを削除しなければならない
3. When 上記コンポーネントを削除した場合, システムは解放されたメモリ予算を新規コンポーネントの投入判断に利用できなければならない

### Requirement 7: シークレット管理の整合性
**Objective:** As 運用者, I want 新規導入する全コンポーネントの認証情報が既存のInfisical/ESOパターンに従うこと, so that ゼロ秘密漏洩アーキテクチャ方針を維持できる

#### Acceptance Criteria
1. The 新規コンポーネントのマニフェストはいかなる認証情報も平文で含んではならない
2. The 新規コンポーネントの認証情報はExternalSecret経由でInfisicalから取得されなければならない
3. Where 認証情報が外部SaaSの管理画面に直接入力される構成である場合 (Requirement 1のGitHub認証情報、Requirement 9のSaaS初回API鍵発行・Discord Webhook作成), その認証情報はInfisicalにも記録され、ローテーション時に参照できなければならない

### Requirement 8: リソース予算とロールアウト安全性
**Objective:** As 運用者, I want 新規監視コンポーネント群が既存ノードのメモリ予算を超えないこと, so that prod-node-1上の既存prodワークロード (Authentik/Directus/Mailserver等) に影響を与えない

#### Acceptance Criteria
1. While prod-node-1のメモリ使用率が既に高水準 (実測82%程度) にある場合, 新規コンポーネントは1つずつ投入し、投入後にメモリ使用率を確認できなければならない
2. If いずれかのコンポーネント投入後にメモリ使用率が90%を超えた場合, 運用者は次のコンポーネントの投入を保留し、ノードアップサイズの検討を判断できなければならない
3. The 新規コンポーネント群のメモリrequest合計は、既存コンポーネント削除 (Requirement 6) による解放分を含めて現行ノード容量 (8GB) に収まらなければならない

### Requirement 9: 外部SaaS設定のコード化
**Objective:** As 運用者, I want UptimeRobot/Healthchecks.io/Netdata Cloudの設定 (監視対象・check定義・Room割当) がコードベース内で宣言的に管理されること, so that 運用者の知識がコードベース外に分散せず、引き継ぎ後の運営チームが現在の運用者と同等の知識を持たなくても構成を再現・変更できる

**設計判断 (手動運用からの転換)**: 当初は無料プランの設定はWebUIで十分という判断だったが、各SaaSに公式または継続的にメンテされたTerraform providerが存在することが判明したため、運用者がコードベース外 (各SaaSの管理画面) で行う必要のある操作を最小化する方針に転換する。SaaS側API鍵の初回発行のみ手動操作の例外として残す (Requirement 7.3のパターンに従う)。

#### Acceptance Criteria
1. The UptimeRobotの監視対象 (Monitor) 定義はTerraformリソースとして管理されなければならない
2. The Healthchecks.ioのcheck定義はTerraformリソースとして管理されなければならない
3. The Netdata CloudのRoom定義および対象ノードの割当はTerraformリソースとして管理されなければならない
4. Where 各SaaSの初回API鍵発行など、サービス側コンソールでの手動操作が技術的に不可避な場合 (ブートストラップ認証情報), その認証情報はRequirement 7.3に従いInfisicalに記録されなければならない
5. When Terraformが生成した値 (Healthchecks.ioのping URL、Netdata CloudのRoom ID等) をクラスター内コンポーネントが必要とする場合, 運用者は既存の `terraform output` から Infisical へ反映する運用パターン (Cloudflare Tunnel Token/IDと同様の既存パターン) に従えなければならない
6. The Discord Webhook URLの作成は、Webhook管理権限を持つ新規Botをサーバーに追加することによる攻撃面拡大を避けるため、手動作成の例外として扱う (Requirement 7.3の例外パターンを適用する)
