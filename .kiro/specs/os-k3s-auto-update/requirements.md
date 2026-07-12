# Requirements Document

## Project Description (Input)
ホストOS(Debian 13/Hetzner)およびK3sクラスタの自動アップデート機構整備。現状、cloud-initはpackage_update: falseで初回起動時のみ実行、以降パッケージ更新の自動化なし(unattended-upgrades等未導入)。K3sはansible/inventory/tailscale.ymlにk3s_versionを固定ピンし、バージョンアップは完全手動(ansible-playbook実行)。Renovate/Dependabot等の依存関係自動追従も未導入。CVE対応やマイナーバージョン追従が属人化している課題を解消する。対策候補: ホストのunattended-upgrades相当導入、k3sのsystem-upgrade-controller導入または定期通知のCI化、Renovateによるterraformプロバイダー/GitHub Actionsバージョン追従の自動PR化。

## Introduction
本機能は、`prod-node-1`(Hetzner CX33 シングルノード、Debian 13 + K3s)におけるOSパッケージ・K3sバージョン・Terraform provider/GitHub Actions・GitOps管理下のコンテナイメージの追従を、現状の完全属人的な手動運用から、検知・通知・適用判断を体系化した運用へ移行する。CVEなど脆弱性情報を別途追跡する仕組みが存在しないため、機械的な検知(Renovateによるバージョン追従PR化、GitHub Dependabot alertsによる脆弱性DB照合)を導入し、属人的な見落としを構造的に防ぐ。シングルノード構成(HAなし、[[ha-improvement]]で別途計画中)のため、自動適用は可用性リスクを伴う。本機能は「セキュリティパッチ・脆弱性の取りこぼし」と「無制御な自動再起動・自動反映によるサービス断・DR誤検知」の両方を避けることを目的とし、既存のDR検知(`dr-trigger.yml`、5分毎cron・複合検出)や通知基盤(`DISCORD_OPS_WEBHOOK_URL`)、GitOps原則(Git manifest修正→ArgoCD sync以外の直接クラスタ操作禁止)と整合させる。

## Boundary Context
- **In scope**: ホストOSパッケージの更新検知・適用方式の定義、K3sバージョンの追従(検知・通知・適用)方式の定義、Terraform provider / GitHub Actions / GitOps管理下のコンテナイメージ(`gitops/manifests/`配下)のバージョン追従自動化(Renovate等によるPR化)、`cloudflare/cloudflared:latest` の明示的バージョンpin化とその後の追従、GitHub Dependabot alerts(脆弱性データベース照合)の有効化、更新作業とDR誤検知防止の整合、更新結果の通知・記録
- **Out of scope**: マルチノード化・HA構成([[ha-improvement]]で別途対応)、K3sマイナーバージョン間の非互換対応そのものの自動修復、コンテナイメージ更新PRのマージ後のアプリケーション互換性検証そのもの(既存のPRレビュー・staging検証フローの範囲)
- **Adjacent expectations**: GitOpsマニフェスト(`gitops/`配下)へのクラスタ直接操作禁止原則(CLAUDE.md)はホストOS/K3sレイヤーには直接適用されないが、Ansible経由の変更はGit管理下に置く既存方針を踏襲する。コンテナイメージのバージョン追従はGitOps原則(Git manifest修正→ArgoCD sync)に完全準拠し、Renovate等が生成したPRのマージが唯一の適用経路となる(直接クラスタ操作は行わない)。DR自動復旧([[dr.md]])が誤発火しないよう、計画的な更新作業とノード障害を区別できる設計とする。

## Requirements

### Requirement 1: ホストOSセキュリティパッチの自動適用
**Objective:** インフラ担当者として、ホストOS(Debian)のセキュリティパッチが定期的に自動適用されてほしい。CVE対応が個人の記憶や手動作業に依存する現状の属人化を解消するため。

#### Acceptance Criteria
1. The host update mechanism shall 手動での `ssh` ログインなしに、定義された定期スケジュールで利用可能なOSセキュリティパッチを適用する。
2. When OSパッケージの更新が適用された場合、the host update mechanism shall 更新結果(成功/失敗、更新されたパッケージ一覧)を事後確認可能な場所(ログファイルまたは通知メッセージ)に記録する。
3. If OSの更新がkernel更新等により再起動を要する場合, then the host update mechanism shall 即座に再起動せず、制御されたウィンドウへ再起動を延期するか、明示的な確認を要求する。
4. While パッケージ更新に伴う再起動が保留中である間、the host update mechanism shall 保留中の再起動状態をインフラ担当者に可視化する(通知または確認コマンドで把握可能)。
5. If OSパッケージ更新が失敗した場合, then the host update mechanism shall 既存のDiscord opsチャンネル(`DISCORD_OPS_WEBHOOK_URL`)経由でインフラ担当者に通知する。

### Requirement 2: 計画的な再起動とDR誤検知の回避
**Objective:** インフラ担当者として、ホスト再起動を伴う更新作業がDR自動復旧の誤発火を引き起こさないでほしい。シングルノード構成でノード再作成が走ると不要なダウンタイムと復旧作業が発生するため。

#### Acceptance Criteria
1. While 更新目的の計画的なホスト再起動が進行中である間、the system shall `dr-trigger.yml` の複合検出条件(Tailscaleオフライン、またはidp/argocd/webmailのうち2つ以上同時応答なし)による誤ったノード再作成のトリガーを回避する。
2. When 計画的な再起動が必要な場合、the update mechanism shall Tailscale・主要サービスの復帰が `dr-trigger.yml` の検出猶予期間(既定10分)を超えない範囲の時間枠内で再起動を実行する、または明示的な猶予延長・一時停止手段を提供する。
3. If 計画的な再起動がDR誤検知の懸念を伴う場合, then the infrastructure operator shall 再起動開始前に `dr-trigger` を抑止・確認するための文書化された手動手順を有する。

### Requirement 3: K3sバージョン追従の検知と通知
**Objective:** インフラ担当者として、K3sの新バージョン(特にセキュリティ修正を含むパッチリリース)が存在することを能動的に把握したい。現状`k3s_version`が`ansible/inventory/tailscale.yml`に固定ピンされ、追従が完全手動であるため。

#### Acceptance Criteria
1. The K3s version tracking mechanism shall 現在固定ピンされている `k3s_version` を最新の利用可能なK3sリリースと定期的に照合する。
2. When より新しいK3sバージョン(特にセキュリティ修正を含むリリース)が利用可能な場合、the K3s version tracking mechanism shall 既存のDiscord opsチャンネル経由でインフラ担当者に通知する。
3. The K3s version tracking mechanism shall 非互換の可能性が異なるため、通知内容においてパッチレベルの更新とマイナー/メジャーバージョンの更新を区別する。
4. The K3s version tracking mechanism shall シングルノード構成でのアップグレード失敗時のロールバック手段が限定的であるため、明示的な人間の承認なしに `ansible/inventory/tailscale.yml` を自動変更したり `ansible-playbook` を自動実行したりしない。

### Requirement 4: K3sアップグレード適用の安全な実行経路
**Objective:** インフラ担当者として、K3sバージョンアップを承認した後は、既存の手動コマンド体系(`ansible-playbook ... -e "k3s_version=..."`)に沿った一貫した手順で安全に適用したい。

#### Acceptance Criteria
1. When インフラ担当者がK3sバージョンアップを承認した場合、the upgrade procedure shall 既存の `ansible-playbook -i ansible/inventory/tailscale.yml ansible/playbooks/k3s-bootstrap.yml -e "k3s_version=<version>"` の実行経路を用いる。
2. The upgrade procedure shall `ansible/inventory/tailscale.yml` の `k3s_version` 値の変更をGitコミットとして記録し、変更履歴を残す。
3. If K3sアップグレードのplaybook実行が失敗した場合, then the upgrade procedure shall クラスタを診断可能な状態のまま維持し、既存のDiscord opsチャンネル経由で失敗詳細をインフラ担当者に通知する。
4. While K3sアップグレードが適用されている間、the upgrade procedure shall Requirement 2と同様のDR誤検知回避策(猶予期間考慮)を適用する。

### Requirement 5: Terraform Provider / GitHub Actions / コンテナイメージのバージョン追従自動化
**Objective:** インフラ担当者として、Terraform provider・GitHub Actions・GitOps管理下のコンテナイメージのバージョン更新をRenovate等で自動検知・PR化したい。CVE等の脆弱性情報を他の手段で追っていないため、機械的な検知に頼らざるを得ないため。

#### Acceptance Criteria
1. The dependency update mechanism shall `terraform/` 配下のprovider定義、`.github/workflows/` 配下のGitHub Actionsバージョン指定、および `gitops/manifests/` 配下のKubernetesマニフェスト内コンテナイメージタグ(例: `directus/directus`, `mailserver/docker-mailserver`, `roundcube/roundcubemail`, `vaultwarden/server`, `ghcr.io/goauthentik/ldap`, `redis`, `nginx`, `cloudflare/cloudflared` 等)を対象に定期的に追従する。
2. When 追従対象のTerraform provider、GitHub Action、またはコンテナイメージの新バージョンが利用可能な場合、the dependency update mechanism shall バージョン更新を含むpull requestを本リポジトリに対してオープンする。
3. The dependency update mechanism shall `authentik` provider アップグレードで過去に破壊的変更(grant_types・redirect_uri_type等)が実際に発生した実績があり、コンテナイメージも同様にアプリケーション互換性検証なしの自動反映はリスクがあるため、自身がオープンしたいかなるpull request(provider・Actions・コンテナイメージいずれも対象)も自動マージせず、人的レビュー・マージを必須とする。
4. Where providerが `authentik` (goauthentik/authentik) である場合、the dependency update mechanism shall 破壊的変更の手動レビューを助けるため、providerのchangelogまたはリリースノートへのリンクをpull request内に表示する。
5. When コンテナイメージのバージョン更新pull requestがマージされた場合、the change shall `CLAUDE.md` の直接クラスタ操作禁止原則に沿って、既存のGitOpsフロー(ArgoCD sync)のみを経由してクラスタへ反映される。
6. The dependency update mechanism shall Requirement 3/4で別途扱うため対象外とし、`ansible/inventory/tailscale.yml` の `k3s_version` を変更しない。

### Requirement 6: cloudflared イメージの明示的バージョンpin化
**Objective:** インフラ担当者として、`gitops/manifests/prod/cloudflared/deployment.yaml` の `cloudflare/cloudflared:latest`(floatingタグ)を明示的なバージョンにpinしたい。`latest`タグのままではRequirement 5のバージョン追従機構が新バージョンの有無を検知できず、かつPod再作成のたびに意図しないバージョンが引き込まれるリスクがあるため。

#### Acceptance Criteria
1. The `cloudflared` deployment manifest shall `latest` の代わりに明示的なsemver形式のimageタグを指定する。
2. When `cloudflared` のimageタグをpinする場合、the pinned version shall 既存挙動からの意図しない後退を避けるため、変更時点で稼働中/安定版の `cloudflared` リリースから選定する。
3. Where pin化が完了した後, the `cloudflared` のその後のバージョン追従 shall Requirement 5の依存関係更新機構(Renovate等)の対象に含まれる。

### Requirement 7: GitHub Dependabot alerts によるCVE検知の併用
**Objective:** インフラ担当者として、Renovate等のバージョン追従PRとは別に、既知の脆弱性(CVE)をGitHubネイティブの仕組みでも検知したい。単一の追従機構の設定漏れ・対象外エコシステムを補完する多層防御とするため。

#### Acceptance Criteria
1. The repository shall 本リポジトリでサポートされているエコシステムに対し、GitHub Dependabot alerts(GitHub Advisory Databaseに対する脆弱性データベース照合)を有効化する。
2. When 本リポジトリ内の依存関係に対しDependabot alertが発報された場合、GitHub shall GitHubネイティブのアラート機構を通じてリポジトリ管理者に通知する(既存のGitHub通知設定に依存し、本機能で新規通知経路は構築しない)。
3. The Dependabot alerts feature shall Requirement 5で定義されたRenovateベースのバージョン更新機構と独立して、競合することなく動作する(両者は役割が異なる: alertsは脆弱性検知のみ、Renovateはバージョン追従PR)。

### Requirement 8: 更新運用の可視性とドキュメント同期
**Objective:** インフラ担当者として、自動更新機構がいつ・何を変更したかを事後に追跡できるようにしたい。障害発生時の切り分けや、引き継ぎ後の運営チームへの説明可能性を確保するため。

#### Acceptance Criteria
1. The update mechanism shall 適用されたすべての変更(OSパッケージ更新、K3sバージョン変更、provider/Actions/コンテナイメージのバージョン更新)を既存システム(ログ、Discord通知履歴、またはGitコミット履歴のいずれか)経由で追跡可能にする。
2. When 本機能が完了(`phase: completed`前)した場合、the documentation sync process shall 既存の[[プロジェクトメモリ同期プロセス]]に従い、新規導入した自動更新の仕組み・コマンド・シークレットを `CLAUDE.md` および `.kiro/steering/tech.md` に反映する。
3. The update mechanism shall 別途文書化された理由がない限り、新規通知チャンネルを設けず既存の `DISCORD_OPS_WEBHOOK_URL` シークレットを通知に再利用する。
