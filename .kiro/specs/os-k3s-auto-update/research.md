# Research & Design Decisions

## Summary
- **Feature**: `os-k3s-auto-update`
- **Discovery Scope**: Complex Integration(既存3層アーキテクチャ[Terraform→Ansible→GitOps]へ横断的に検知・通知・PR自動化レイヤーを追加)
- **Key Findings**:
  - `prod-node-1`の実OSはDebian 13(`terraform/variables.tf`の`hcloud_image`デフォルト値 `debian-13`、override無し)。Debianは公式パッケージ`unattended-upgrades`を持ち、`Allowed-Origins`をsecurity originのみに絞ることで「セキュリティパッチのみ」の自動適用が標準機能で実現できる(Archのような「フルスコープ更新しか選べない」制約は存在しない)。
  - `unattended-upgrades`はkernel等の更新でreboot要と判定すると`/var/run/reboot-required`(および対象パッケージ一覧`/var/run/reboot-required.pkgs`)を生成する。これがreboot要否判定の標準インターフェースであり、独自のkernel差分検知ロジックを実装する必要はない。
  - `needrestart`パッケージを併用すると、reboot不要でも更新により再起動が必要なデーモン(古い`.so`をmapしたままのプロセス)をdpkgフック経由で検出できる。
  - Renovateの`kubernetes`マネージャは生のKubernetesマニフェスト(`image:`フィールド)を直接走査できる。ただし`latest`等のfloatingタグは追従不能なため、対象イメージは事前に明示バージョンへpinする必要がある(`cloudflared`が該当)。
  - K3sのバージョン情報は `https://update.k3s.io/v1-release/channels` (channel API) または GitHub Releases API (`api.github.com/repos/k3s-io/k3s/releases/latest`) から取得可能。追加認証不要。
  - GitHub Dependabot alertsはRenovateとは独立した機能(脆弱性DBスキャンのみ、PR作成なし)。リポジトリ設定のトグル、または `gh api -X PUT repos/<owner>/<repo>/vulnerability-alerts` で有効化。現状Terraformでリポジトリ設定自体を管理していないため、Terraform化はオーバーヘッドが大きく本機能のスコープでは見送り、手動ブートストラップ項目として扱う。
  - 既存の `dr-trigger.yml`(5分毎cron)+ 猶予期間(既定10分)の複合検出により、再起動を伴う計画的作業は合計最大15分程度の許容窓を持つ。ホスト再起動・K3sローリング再起動はいずれもこの窓内(実測目安5分以内)に収まる設計とすることで、`dr-trigger.sh`自体の改修なしにDR誤検知を回避できる。

## Research Log

### Debianの無人セキュリティアップデート手法
- **Context**: R1でホストOSパッケージの自動セキュリティパッチ適用が求められる。`prod-node-1`の実OSはDebian 13(当初Arch Linuxと誤認していたが`terraform/variables.tf`の`hcloud_image`デフォルト`debian-13`で確認済み)。
- **Sources Consulted**: [Setup auto-updates in Debian and Ubuntu with Unattended-Upgrades and NeedRestart](https://fullmetalbrackets.com/blog/setup-unattended-upgrades), [Unattended security upgrades on Debian and Ubuntu](https://stackharbor.com/en/knowledge-base/unattended-upgrades-debian/), [mvo5/unattended-upgrades README](https://github.com/mvo5/unattended-upgrades/blob/master/README.md), [Debian Wiki: PeriodicUpdates](https://wiki.debian.org/PeriodicUpdates)
- **Findings**:
  - `unattended-upgrades`パッケージが公式に提供されており、`/etc/apt/apt.conf.d/50unattended-upgrades`の`Unattended-Upgrade::Allowed-Origins`にsecurity origin(`origin=Debian,codename=${distro_codename},label=Debian-Security`)のみを指定することで、セキュリティパッチのみへスコープを限定した自動適用が標準機能で実現できる。
  - `Unattended-Upgrade::Automatic-Reboot`を`true`にすると、`/var/run/reboot-required`が存在する場合のみ無確認で自動再起動する。この判定ファイルはkernel/systemd等の更新時にAPT自身が生成する標準インターフェース。
  - `needrestart`パッケージを併用すると、reboot不要な場合でも再起動が必要なデーモンをdpkgフック経由で検出し、自動再起動(`$nrconf{restart} = 'a';`)も可能。
- **Implications**: Archで想定していた「フルスコープ更新+kernel差分の自前検知」という妥協は不要。`Allowed-Origins`をsecurity originのみに絞ることでR1の「セキュリティパッチのみ」という要件を標準機能でそのまま満たせ、reboot要否判定も`/var/run/reboot-required`を読むだけで済む(自前ロジック不要)。CVE可視化の補助には`debsecan`(Debian公式パッケージ)を利用する。

### Renovateのkubernetesマネージャとfloatingタグの制約
- **Context**: R5でgitops/manifests配下のコンテナイメージをRenovateで追従したいが、`cloudflared:latest`のようなfloatingタグは追従できるか確認が必要だった。
- **Sources Consulted**: [Renovate Docs: Kubernetes manager](https://docs.renovatebot.com/modules/manager/kubernetes/)
- **Findings**: kubernetesマネージャは生マニフェストの `image:` フィールドを検出し、単一imageタグ更新PRを生成する。ただし明示的なバージョンタグが前提であり、`:latest` の場合は比較対象がないため検出不能。
- **Implications**: R6(cloudflaredのpin化)はR5のRenovate導入における前提条件であり、実装順序として「pin化→Renovate導入」の順で行う必要がある。

### K3sバージョン情報の取得方法
- **Context**: R3でk3s_versionの新版検知を機械化するための情報源を確認。
- **Sources Consulted**: [update.k3s.io channels API](https://update.k3s.io/v1-release/channels), [k3s-io/k3s Releases](https://github.com/k3s-io/k3s/releases), [K3s Docs: Automated Upgrades](https://docs.k3s.io/upgrades/automated)
- **Findings**: `update.k3s.io/v1-release/channels` はチャンネル(stable/latest/testing等)ごとの最新バージョンをJSONで返す。GitHub Releases API (`/repos/k3s-io/k3s/releases/latest`) でも同等情報が取得可能で追加認証不要。
- **Implications**: 追加のSaaS導入なしに、既存の `dr-trigger.yml` と同じ「GitHub Actions cron + bashスクリプト + Discord webhook」パターンで検知を実装できる。K3s公式の`system-upgrade-controller`(クラスタ内Job型)は自動適用寄りの設計であり、R3.4(人間承認必須)の要件と相性が悪いため不採用。

### GitHub Dependabot alertsの有効化方法
- **Context**: R7で既存のRenovateと役割の異なる脆弱性検知レイヤーとしてDependabot alertsを併用する。
- **Sources Consulted**: [GitHub Docs: Configuring Dependabot alerts](https://docs.github.com/en/code-security/dependabot/dependabot-alerts/configuring-dependabot-alerts), [terraform-provider-github: github_repository](https://registry.terraform.io/providers/integrations/github/latest/docs/resources/repository)
- **Findings**: リポジトリの `vulnerability_alerts` フラグ(Terraform経由でも設定可能だが非推奨扱いに移行中、`security_and_analysis`ブロックが現行推奨)。本リポジトリはGitHubリポジトリ自体をTerraform管理していない(hcloud/cloudflare/tailscale/authentikのみ)ため、この1機能のためだけに新規providerを導入するのはコスト超過と判断。
- **Implications**: リポジトリ管理者によるGitHub Web UIまたは `gh api -X PUT repos/<owner>/<repo>/vulnerability-alerts` を用いた一度きりの手動ブートストラップ項目として扱う(既存の `infisical-auth` Secret 直接作成や GitHub Deploy Key 登録と同種の「例外的直接操作」パターン)。

### DR誤検知(dr-trigger.yml)との時間整合性
- **Context**: R2で計画的な再起動・K3sローリング再起動がDR自動復旧を誤発火させないことを保証する必要がある。
- **Sources Consulted**: リポジトリ内 `.kiro/steering/dr.md`、`.github/workflows/dr-trigger.yml`
- **Findings**: `dr-trigger.yml` は5分毎cronで、Tailscaleオフラインまたはidp/argocd/webmailのうち2つ以上同時応答なしを検出した場合に既定10分の猶予期間を経て復旧をdispatchする。猶予期間中は `dr-incident` ラベルIssueへの `abort` コメントで人手中止可能。
- **Implications**: `dr-trigger.sh` 自体の改修は行わず、(a) 再起動を伴う操作を合計5分以内で完了する設計とする、(b) 想定を超えて長引く場合の手動中止手順を運用ドキュメント(`docs/dr-runbook.md`)に追記する、の2点で対応する。新規のメンテナンスモード信号機構は本機能のスコープでは導入しない(過剰設計と判断)。

## Architecture Pattern Evaluation

| Option | Description | Strengths | Risks / Limitations | Notes |
|--------|-------------|-----------|---------------------|-------|
| GitHub Actions cron + bashスクリプト(既存dr-trigger.ymlパターン踏襲) | K3sバージョン検知・K3sアップグレード実行ワークフローを既存パターンで実装 | 既存パターン踏襲でレビュー負荷低、追加SaaS不要 | GitHub Actions実行時間に依存(遅延はDR許容窓に影響しない設計のため許容) | 採用 |
| k3s system-upgrade-controller(クラスタ内蔵) | K3s公式のクラスタ内自動アップグレード機構 | K3sエコシステム標準 | 自動適用前提の設計でR3.4(人間承認必須)と相性が悪い、シングルノードでの安全性実績が薄い | 不採用 |
| Renovate(SaaS/GitHub App) | Terraform provider・GitHub Actions・コンテナイメージのバージョン追従PR化 | 3対象を単一ツールでカバー、regex managerで拡張可能 | GitHub App権限の外部委譲が発生 | 採用(R5/R6) |
| Dependabot version updates(Renovateの代替) | terraform/github-actionsエコシステムのみネイティブ対応 | GitHub純正、追加App不要 | dockerエコシステムはDockerfileのみ対応、`gitops/manifests/`配下の生k8sマニフェストは追従不能 | 不採用(alertsのみ採用、version updatesはRenovateに統一) |

## Design Decisions

### Decision: ホストOS更新は`unattended-upgrades`の`Allowed-Origins`をsecurity originのみに絞って適用する
- **Context**: R1はセキュリティパッチのみの自動適用を要求する。DebianはDebian-Security originを個別に指定できるため、非セキュリティ更新を含めるかどうかを選択できる。
- **Alternatives Considered**:
  1. `Allowed-Origins`をデフォルト(security originのみ)から拡張し、通常のstable更新も含めてフル追従する — 非セキュリティ変更による予期しない破壊的変更のリスクが増える。
  2. `Allowed-Origins`をsecurity originのみに限定する(Debian公式ドキュメントの標準的な運用) — 更新スコープが必要最小限に留まる。
- **Selected Approach**: 2を採用。`50unattended-upgrades`設定でsecurity originのみを許可し、週次でスケジュール実行する。reboot要否は`/var/run/reboot-required`を参照し、`Unattended-Upgrade::Automatic-Reboot "true"`+`Automatic-Reboot-Time`でメンテナンスウィンドウ内の自動再起動を標準機能に任せる。
- **Rationale**: R1が要求する「セキュリティパッチの自動適用」に対し、Debian標準機能でスコープを過不足なく一致させられる。自前でCVEフィルタリングロジックを実装する必要がない。
- **Trade-offs**: セキュリティ修正以外の不具合修正・機能更新は自動適用対象外のまま残る(手動`apt upgrade`が別途必要になる場合がある)。この点はR1のスコープ外として許容する。
- **Follow-up**: 実装時に `kvm-test.yml` インベントリで最低1回のドライラン検証(`unattended-upgrade --dry-run -d`)を行う。

### Decision: 再起動はメンテナンスウィンドウ内で自動実行し、`dr-trigger.sh`は改修しない
- **Context**: R2はDR誤検知回避を求めるが、`dr-trigger.sh`への変更はスコープ拡大(GitOps外の外部監視ロジック改修)を招く。
- **Alternatives Considered**:
  1. `dr-trigger.sh`にメンテナンスフラグ確認ロジックを追加 — 実装コストと既存DRロジックへの影響範囲が大きい。
  2. 再起動を5分以内で完了する設計とし、既存の猶予期間(10分)・abortコメント機構をそのまま活用 — 追加改修不要。
- **Selected Approach**: 2を採用。`Unattended-Upgrade::Automatic-Reboot-Time`で低トラフィック帯の固定時刻を指定し(Debian標準機能、自作タイマーではない)、reboot-required時のみその時刻に自動再起動させる。パッケージ適用自体は`apt-daily-upgrade.timer`(Debian標準、通常は日次)に任せ、再起動発生時のboot〜Tailscale再接続を5分以内に収まる想定とする。
- **Rationale**: 既存の複合検出+猶予期間が実測所要時間に対して十分な余裕(15分 vs 目標5分)を持つため、新規の抑止機構を導入するコストに見合わない。
- **Trade-offs**: 万一更新+再起動が5分を大幅に超えた場合はDR誤検知のリスクが残る。その場合の人手対応(猶予期間中のabortコメント)を運用ドキュメントに明記する。
- **Follow-up**: 実装後、実際の再起動所要時間を計測しdocs/dr-runbook.mdに実測値を記録する。

### Decision: K3sバージョン変更適用は「Git先行コミット→ワークフロー実行」の順序を強制する
- **Context**: R4.2はk3s_version変更をGitコミットとして記録することを求める。
- **Alternatives Considered**:
  1. `workflow_dispatch`の入力パラメータとして新バージョンを受け取り、ワークフロー内でファイルを書き換えてコミットする — 承認プロセスがワークフロー実行の裏に隠れ、通常のPRレビューを経由しない。
  2. 運用者が先に `ansible/inventory/tailscale.yml` を編集・コミット・マージし、その後 `workflow_dispatch` (入力なし)で最新mainの値を使ってアップグレードを実行する — 通常のGitOps的レビューフローに合致。
- **Selected Approach**: 2を採用。
- **Rationale**: 既存のGitOps原則(Git manifest修正が正、直接操作は例外)と一貫性を保つため。
- **Trade-offs**: ワークフロー実行前に必ずPRマージという1ステップが増えるが、変更履歴の一貫性を優先する。

### Decision: Dependabot alertsとRenovateは併用し、役割を明確に分離する
- **Context**: R7は多層防御としてのCVE検知を求める。
- **Alternatives Considered**:
  1. Renovateのみ導入し、Dependabot alertsは有効化しない — 単一障害点(Renovate設定漏れ・対象外エコシステム)のリスクが残る。
  2. Dependabot alerts(脆弱性検知のみ)を併用し、バージョン追従PRはRenovateに一本化 — 責務分離が明確。
- **Selected Approach**: 2を採用。
- **Rationale**: Dependabot alertsは設定ゼロで有効化でき、Renovateの設定ミスや対象外ファイルパターンを補完するセーフティネットとして低コストで機能する。
- **Trade-offs**: 2つの通知経路(Renovate PR / GitHub native alert)が並存するため、運用者はどちらの通知か区別する必要がある(通知本文に発信元を明記することで軽減)。

## Risks & Mitigations
- security origin限定でも稀に依存パッケージの競合で更新が失敗するリスク — 週次実行・失敗時はDiscord即時通知・既存DR(ノード再作成)を最終フォールバックとして許容
- 再起動所要時間がDR猶予期間(15分)を超過するリスク — 実測に基づきメンテナンスウィンドウの時間帯・所要時間上限を運用ドキュメントに明記し、超過が予見される場合は事前にabortコメントで抑止する運用手順を用意
- RenovateのGitHub App権限が外部SaaSに委譲されるリスク — リポジトリ単位のインストールに限定し、書き込み権限はPRオープンのみに絞る(Renovate標準権限セット)
- K3sバージョン検知の通知が頻発しノイズ化するリスク(運用者が追従を先送りし続けた場合) — 週次頻度に留め、通知本文に前回検知からの経過情報は持たせない(状態ファイル管理は過剰設計と判断し見送り)
- `cloudflared` pin化時に選定バージョンが現行稼働版より古くなる回帰リスク — 実装時に稼働中のイメージダイジェスト/バージョンを確認してから同等以上のバージョンを選定する

## References
- [Setup auto-updates in Debian and Ubuntu with Unattended-Upgrades and NeedRestart](https://fullmetalbrackets.com/blog/setup-unattended-upgrades) — unattended-upgrades + needrestartの設定パターン
- [Unattended security upgrades on Debian and Ubuntu](https://stackharbor.com/en/knowledge-base/unattended-upgrades-debian/) — Allowed-Origins / reboot-requiredの仕様
- [mvo5/unattended-upgrades README](https://github.com/mvo5/unattended-upgrades/blob/master/README.md) — 公式リポジトリの設定リファレンス
- [Debian Wiki: PeriodicUpdates](https://wiki.debian.org/PeriodicUpdates) — Debian公式の定期更新運用ガイド
- [Renovate Docs: Kubernetes manager](https://docs.renovatebot.com/modules/manager/kubernetes/) — 生マニフェストのimageタグ追従の仕様と制約
- [update.k3s.io channels API](https://update.k3s.io/v1-release/channels) — K3sリリースチャンネル情報の取得元
- [K3s Docs: Automated Upgrades](https://docs.k3s.io/upgrades/automated) — system-upgrade-controllerの動作仕様(不採用理由の裏付け)
- [GitHub Docs: Configuring Dependabot alerts](https://docs.github.com/en/code-security/dependabot/dependabot-alerts/configuring-dependabot-alerts) — Dependabot alertsの有効化手順
