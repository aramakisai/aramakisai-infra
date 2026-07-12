# Implementation Plan

- [ ] 1. (P) ホストOS自動更新: Debian標準機能への設定委譲と通知スクリプト実装
- [ ] 1.1 apt/unattended-upgrades設定テンプレート作成
  - `/etc/apt/apt.conf.d/50unattended-upgrades` テンプレートで `Unattended-Upgrade::Allowed-Origins` をsecurity originのみに限定し、`Automatic-Reboot "true"` と低トラフィック帯固定時刻の `Automatic-Reboot-Time` を設定する
  - `/etc/apt/apt.conf.d/20auto-upgrades` テンプレートで `APT::Periodic::Unattended-Upgrade "1"` を設定し、Debian標準の `apt-daily-upgrade.timer` を起動させる
  - `needrestart` 設定を `$nrconf{restart} = 'a'` の無人自動再起動モードにする
  - Ansibleロール `os-auto-update` が `unattended-upgrades`/`needrestart`/`debsecan` パッケージをインストールし、上記3テンプレートを配布した時点で observable: `apt-config-package unattended-upgrades` 相当の確認コマンドで設定値が反映されていること
  - _Requirements: 1.1, 1.2_
  - _Boundary: HostAutoUpdateAgent_

- [ ] 1.2 通知スクリプトとsystemd timer実装
  - `os-update-notify.sh` が `/var/log/unattended-upgrades/unattended-upgrades.log` を読み取り、当日の適用結果(成功/失敗/適用パッケージ)を判定する
  - `debsecan` の残存脆弱性リストと `/var/run/reboot-required` の有無を通知本文に含める(判定のみで再起動操作は行わない)
  - `os-update-notify.timer` が `apt-daily-upgrade.timer` より後の時刻にオフセットして起動するsystemdユニットを配布する
  - `DISCORD_OPS_WEBHOOK_URL` をAnsibleがInfisicalから取得し `/etc/os-update-notify.env`(モード0600・root所有)へ配布する
  - observable: 手動で `systemctl start os-update-notify.service` を実行するとDiscordの `DISCORD_OPS_WEBHOOK_URL` チャンネルへ通知メッセージが1件投稿されること
  - _Requirements: 1.3, 1.4, 1.5, 8.3_
  - _Boundary: HostAutoUpdateAgent_

- [ ] 1.3 kvm-test.ymlでのロール適用・ドライラン検証
  - `os-auto-update` ロールを `ansible/inventory/kvm-test.yml` に対して適用する
  - `unattended-upgrade --dry-run -d` を実行し、Allowed-Origins/Automatic-Reboot設定が意図通り解釈されることを確認する
  - 失敗時に再起動が実行されないこと、成功時のみDiscord通知が送られることをテスト環境で確認する
  - observable: kvm-test環境で `unattended-upgrade --dry-run -d` の出力にsecurity origin以外のパッケージが含まれないこと
  - _Requirements: 1.1, 1.5, 2.1_
  - _Boundary: HostAutoUpdateAgent_

- [ ] 2. (P) K3sバージョン検知ワークフロー実装
- [ ] 2.1 バージョン比較・分類ロジックとスクリプト単体テスト実装
  - `.github/scripts/k3s-version-check.sh` が `ansible/inventory/tailscale.yml` の `k3s_version` を読み取り、`update.k3s.io/v1-release/channels`(stableチャンネル)の最新値と比較する
  - semver比較でpatchレベル差分とminor/major差分を分類するロジックを実装する
  - `scripts/test-k3s-version-check-logic.sh` でpatch差分/minor差分/差分なしの3ケースを検証する(`scripts/test-dr-trigger-logic.sh` と同形式)
  - observable: テストスクリプト実行で3ケース全てが期待通りの分類結果(patch/minor/差分なし)を返すこと
  - _Requirements: 3.1, 3.3_
  - _Boundary: K3sVersionChecker_

- [ ] 2.2 週次cron GitHub Actions workflow実装・Discord通知配線
  - `.github/workflows/k3s-version-check.yml` を `schedule`(週次cron)+ `workflow_dispatch` トリガーで実装する
  - 新バージョン検知時のみ `DISCORD_OPS_WEBHOOK_URL` へ通知を送り、差分なしの場合は無通知とする
  - `ansible/inventory/tailscale.yml` やクラスタへのいかなる変更も行わないことをワークフロー内容として保証する
  - observable: `workflow_dispatch` を手動実行し、現在ピン済みバージョンと異なる値を用意した状態でDiscordに新バージョン通知が1件届くこと
  - _Requirements: 3.2, 3.4, 8.3_
  - _Boundary: K3sVersionChecker_
  - _Depends: 2.1_

- [ ] 3. (P) K3s安全アップグレード実行ワークフロー実装
- [ ] 3.1 workflow_dispatchワークフロー実装
  - `.github/workflows/k3s-upgrade.yml` を入力パラメータなしの `workflow_dispatch` で実装し、`dr-recovery.yml` と同じTailscale(`tag:ci`)参加+Infisical CLIセットアップ手順を再利用する
  - `infisical run` 経由で `ansible-playbook -i ansible/inventory/tailscale.yml ansible/playbooks/k3s-bootstrap.yml -e "k3s_version=<mainブランチの値>"` を実行する
  - mainブランチにチェックアウトした時点の `ansible/inventory/tailscale.yml` の値をそのまま使うことで、Git先行コミット・PRマージ後の実行を前提とする設計を担保する
  - observable: `kvm-test.yml` に対して手動でこの手順を実行し、指定した `k3s_version` でK3sサービスが再起動・正常稼働すること
  - _Requirements: 4.1, 4.2, 4.4_
  - _Boundary: K3sUpgradeWorkflow_

- [ ] 3.2 失敗時Discord通知実装とkvm-test.ymlでの動作確認
  - playbook実行が非ゼロ終了した場合に、ジョブログURLを含む失敗詳細を `DISCORD_OPS_WEBHOOK_URL` へ通知するステップを追加する
  - playbook失敗時にクラスタが診断可能な状態のまま残ること(既存playbookの冪等性に依存し、追加のロールバック処理は実装しない)を確認する
  - observable: `kvm-test.yml` に対して意図的に失敗するバージョン値を指定して実行し、Discordに失敗通知が1件届くこと
  - _Requirements: 4.3, 8.3_
  - _Boundary: K3sUpgradeWorkflow_
  - _Depends: 3.1_

- [ ] 4. (P) cloudflaredイメージの明示バージョンpin
  - `gitops/manifests/prod/cloudflared/deployment.yaml` の `image: cloudflare/cloudflared:latest` を実装時点で稼働中と同等以上の安定版semverタグへ変更する
  - 変更をGitOpsフロー(PRマージ→ArgoCD sync)経由で適用し、直接クラスタ操作は行わない
  - observable: `make kubectl ARGS="get pods -n prod -l app=cloudflared"` でPodが新タグのイメージで正常稼働(Running)していること
  - _Requirements: 6.1, 6.2_
  - _Boundary: CloudflaredImagePin_

- [ ] 5. Renovate導入とバージョン追従PR自動化設定
- [ ] 5.1 renovate.json作成
  - リポジトリルートに `renovate.json` を作成し、`terraform`・`github-actions`・`kubernetes` の3マネージャを有効化する
  - `kubernetes` マネージャの対象ファイルパターンを `gitops/manifests/**/*.yaml` に明示指定する
  - `ansible/inventory/**` を `ignorePaths` に追加し `k3s_version` への干渉を防ぐ
  - リポジトリ全体で `automerge: false` を明示設定する
  - observable: `renovate.json` の構文がRenovateのconfig validatorでエラーなく解釈されること
  - _Requirements: 5.1, 5.3, 5.6_
  - _Boundary: RenovateConfig_

- [ ] 5.2 Renovate GitHub Appインストールと初回検出確認
  - リポジトリ管理者がRenovate GitHub Appを本リポジトリへインストールする(一度きりの手動操作)
  - 初回実行後、Dependency Dashboard IssueでTerraform provider・GitHub Actions・コンテナイメージ(pin済みのcloudflaredを含む)3種すべてが検出対象として認識されていることを確認する
  - `authentik` (goauthentik/authentik) provider向けPRの本文にRenovate標準機能でリリースノートリンクが含まれることを確認する
  - observable: Dependency Dashboard Issueに3種の対象ファイルからの検出項目が一覧表示されること
  - _Requirements: 5.2, 5.4, 5.5, 6.3_
  - _Boundary: RenovateConfig_
  - _Depends: 4, 5.1_

- [ ] 6. (P) GitHub Dependabot alerts有効化
  - リポジトリ管理者がGitHub Web UI(Settings → Code security)または `gh api -X PUT repos/<owner>/<repo>/vulnerability-alerts` でDependabot alertsを有効化する
  - `.github/dependabot.yml`(version updates)は追加しないことを確認し、Renovateとの役割重複を避ける
  - observable: GitHubリポジトリの「Security」タブでDependabot alertsのステータスが有効(Enabled)と表示されること
  - _Requirements: 7.1, 7.2, 7.3_
  - _Boundary: DependabotAlertsBootstrap_

- [ ] 7. Integration: playbook組込みとドキュメント同期
- [ ] 7.1 os-auto-updateロールをk3s-bootstrap.ymlへ組込み
  - `ansible/playbooks/k3s-bootstrap.yml` のPlay 0(`hosts: all`)に `os-auto-update` ロールを追加し、既存の `swap` ロールと同じタイミングで適用対象に含める
  - observable: `ansible-playbook` の構文チェック(`--syntax-check`)がエラーなく通ること
  - _Requirements: 1.1_
  - _Depends: 1.3_

- [ ] 7.2 dr-runbook.mdへ計画的メンテナンス手順を追記
  - `docs/dr-runbook.md` に、再起動を伴う計画的メンテナンスが `dr-trigger.yml` の猶予期間を超過しそうな場合の、`dr-incident` ラベルIssueへの `abort` コメントによる抑止手順を明記する
  - observable: `docs/dr-runbook.md` に該当セクションが追加されていること
  - _Requirements: 2.3_
  - _Depends: 1.3_

- [ ] 7.3 README.mdへブートストラップ手順を追記
  - README.mdの手動ブートストラップ手順セクションに、Renovate GitHub Appインストールおよび GitHub Dependabot alerts有効化を、既存のGitHub Deploy Key登録等と並ぶ項目として追記する
  - observable: README.mdの該当セクションに2項目が追加されていること
  - _Requirements: 5.2, 7.1_
  - _Depends: 5.2, 6_

- [ ] 7.4 CLAUDE.md/tech.mdへの完了時ドキュメント同期
  - `.kiro/steering/tech.md` に本機能で新規導入したコマンド(K3sバージョン確認・アップグレードworkflow起動手順)とシークレット(`DISCORD_OPS_WEBHOOK_URL` の新規利用箇所)を追記する
  - `CLAUDE.md` の該当セクションに、本機能で追加された運用手順があれば反映する
  - すべての変更(ホストOS更新・K3sバージョン変更・provider/Actions/コンテナイメージ更新)がログ・Discord通知履歴・Gitコミット履歴のいずれかで追跡可能であることを最終確認する
  - observable: `.kiro/steering/tech.md` に本機能のコマンド・シークレット一覧が反映されていること
  - _Requirements: 8.1, 8.2_
  - _Depends: 1.3, 2.2, 3.2, 4, 5.2, 6_

- [ ] 8. Validation: 再起動タイミングのDR整合性実測確認
  - `prod-node-1` への実環境展開後、実際のメンテナンスウィンドウでの再起動所要時間(パッケージ適用完了からTailscale再接続まで)を計測する
  - 計測値が `dr-trigger.yml` の検出+猶予期間(合計最大15分)に対して十分な余裕を持つことを確認し、結果を `docs/dr-runbook.md` に実測値として記録する
  - 計測中に `dr-trigger.yml` が誤発火しなかったことをGitHub Actions実行履歴で確認する
  - observable: `docs/dr-runbook.md` に実測所要時間の記録が追加され、該当日時の `dr-trigger.yml` 実行履歴に誤発火がないこと
  - _Requirements: 2.1, 2.2_
  - _Depends: 7.1, 7.2_
