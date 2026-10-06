# Requirements Document

## Project Description (Input)
Zitadel にログインした実行委員 (Zitadel の project role `executive`) が最初に着地するポータルと、管理者 (project role `admin`) だけが使う運用ダッシュボードを、1 つのアプリケーションとして提供する。

- ポータル: `executive` を持つ利用者だけが開ける共通ページ。利用中のサービス (CMS・Webmail 等) へのリンク集を表示する。Zitadel に直接ログインした後の遷移先とする。この遷移先は Zitadel のインスタンス全体で共通のため、`executive` を持たない利用者 (出展団体等) もポータルに到達する。その場合はリンク集を見せず、権限がないことを説明する拒否ページを表示する。共通ページ上には、admin にだけ運用ダッシュボードへの導線を表示する。ポータルの閲覧可否、導線の表示可否、運用ダッシュボードへのアクセス可否は、ブラウザ側ではなくサーバー側で強制する。利用者数上限のある Cloudflare Access は使わず、アプリ側で Zitadel の OIDC 認証と `groups` claim による認可を行う。
- 運用ダッシュボード: 各情報源を個別に巡回しなくて済むことを目的とし、外部サービスの課金と利用枠 (プランと上限はコードで宣言)、既存監視 (UptimeRobot・Healthchecks.io) の状態、クラスタ・データ保護・外部接続・CI・メール配送の状態、Zitadel の認証イベント、prod-node-1 のリソース使用状況 (CPU・メモリ・ディスク。Netdata の簡易版にあたり、詳細は Netdata への参照で確認する)、Falco の検知統計、fail2ban の BAN 状況、DMARC 集計レポートを 1 か所で確認できるようにする。既存の監視・通知ツールは情報源としてそのまま使い、置き換えない。本仕様のコンポーネントを組み込むために必要な既存設定への追加的な変更 (除外ルールへの追記、送信先の追加、経路・ボリュームの追加等) は行う。Falco の検知統計・fail2ban・DMARC・TLS-RPT は、表示に必要な取得・集計・保存の仕組みを新たに用意する。

制約: 構成とリンク一覧はすべてコード (GitOps/IaC) で宣言し、WebUI だけに存在する設定を作らない。prod-node-1 (Hetzner CX33) はメモリに余裕がないため、追加する各コンポーネントの requests/limits を設定し、実測でメモリ予算内に収まることを確認する。引き継ぎ後の運営チームが保守できる構成とする。

## Introduction
実行委員が Zitadel にログインしても、現状は利用できるサービスの一覧を示す起点がなく、Zitadel 既定の画面に遷移する。また運用状態 (課金・監視・リソース・攻撃・メール到達性) の確認先は複数のツールに分散している。本仕様は、実行委員向けのポータルと admin 向けの運用ダッシュボードを 1 つのアプリケーションとして提供し、ログイン後の起点と日常の運用巡回先を一本化する。

## Boundary Context
- **In scope**:
  - `executive` 向けポータル (共通リンク集)、Zitadel に直接ログインした後のポータルへの遷移、`executive` を持たない利用者向けの拒否ページ
  - admin 限定の運用ダッシュボードと、ポータル上の admin 限定導線
  - 既存監視の状態・ノードリソース使用状況の表示 (既存の情報源を参照)
  - Falco の検知結果の収集・保存と検知統計の表示
  - fail2ban BAN 状況の取得と表示
  - DMARC 集計レポートの受信・パース・保存・表示
  - 外部サービスの課金・利用枠の表示と、プラン・上限値のコードによる宣言
  - クラスタ・データ保護・外部接続・CI・メール配送の状態表示 (既に通知対象の事象も現在の状態として表示)
  - Zitadel の認証イベントの表示
  - TLS-RPT レポートの取り込みと集計表示
  - 追加コンポーネントのメモリ予算の確認
  - 本仕様のコンポーネントを組み込むための既存設定への追加的な変更 (Falco の除外マクロへの本仕様のコンポーネントの追記、Falcosidekick への送信先の追加、Cloudflare Tunnel・DNS への経路の追加、mailserver のボリューム・配送先の追加、Zitadel の OIDC アプリ・machine user・遷移先の設定、ホストの状態ファイルの出力追加)
- **Out of scope**:
  - 既存の監視・通知ツール (Netdata・UptimeRobot・Healthchecks.io・Falco・Falcosidekick・fail2ban・Discord 通知) の置き換え
  - 既存のワークロードに対する検知・通知・遮断の方針の変更 (Falco の検知ルールの判定内容、fail2ban の jail 定義・BAN ポリシー、通知の対象や送信先の付け替え)
  - DMARC・SPF・DKIM・TLS-RPT のポリシー値の変更
  - Grafana Cloud (解約済み) 等の外部メトリクス基盤の導入
  - Backblaze B2 (使用していない)
  - Kubernetes の監査ログの表示
  - 凍結中のサービス (Vaultwarden・room-presence) の稼働再開と、その状態表示
- **Adjacent expectations**:
  - 各リンク先サービスは、引き続き自身の OIDC 認可で権限を判定する。ポータルのリンク表示はリンク先の権限判定を代替しない
  - Zitadel の OIDC アプリ・claim 設定は `zitadel-bootstrap` ロールの既存の管理方法に従う
  - シークレットは Infisical + ExternalSecret の既存規約に従う

## Requirements

### Requirement 1: ポータルの認証とログイン後の遷移
**Objective:** As a 実行委員, I want Zitadel にログインしたら利用できるサービスの起点ページに着地したい, so that どこから各サービスに入ればよいか迷わない

#### Acceptance Criteria
1. When 利用者が OIDC のリクエストを伴わずに Zitadel へ直接ログインした, the Zitadel shall ログイン完了後の遷移先としてポータルを表示する
2. When 未認証の利用者がポータルにアクセスした, the ポータル shall Zitadel の OIDC 認証へ誘導し、認証完了後に元のページへ戻す
3. While 利用者が Zitadel の SSO セッションを保持している, when ポータルにアクセスした, the ポータル shall 認証情報の再入力を求めずに表示する
4. While 利用者が Zitadel の `groups` claim に `executive` を持つ, the ポータル shall 共通ページを表示する
5. If `executive` を持たない認証済み利用者がポータルにアクセスした, then the ポータル shall リンク一覧を含む共通ページの内容を返さず、ポータルの閲覧権限がないことを説明する拒否ページを表示する
6. The ポータル shall 閲覧可否をサーバー側で判定し、`executive` を持たない利用者がリンク一覧等の設定データを取得できる経路を持たない
7. If 未認証のリクエストがポータルの設定データ (リンク一覧等) を取得しようとした, then the ポータル shall 内容を返さずに拒否する
8. The ポータル shall 利用者数の上限によって閲覧可能な利用者数が制限されない認証方式で保護される

### Requirement 2: 共通リンク集
**Objective:** As a 実行委員, I want 委員会の活動で使うサービスへのリンクを 1 ページで見たい, so that 各サービスの URL を覚えなくてよい

#### Acceptance Criteria
1. While 利用者が `executive` を持つ, the ポータル shall 次のリンクを含む共通リンク集を表示する: CMS、Webmail、Zitadel のアカウント設定 (パスワード・MFA)、公開サイト (aramakisai.com)、Notion、Google Drive、公式 SNS アカウント (X・Instagram・YouTube)
2. The ポータル shall ArgoCD、開発用・ステージング用のサイト、メールクライアントの設定手順、凍結中 (稼働を停止している) のサービスへのリンクを共通リンク集に表示しない
3. The ポータル shall リンク以外の内容 (お知らせ・手順書等) を表示しない
4. The リンク一覧 shall リポジトリ内のコードとして宣言され、WebUI 上の操作だけで変更される設定を持たない
5. The リンク一覧 shall 内部向けの URL (Notion・Google Drive) をリポジトリに記載せず、シークレット管理 (Infisical) から注入し、リポジトリにはどのシークレットのキーがどのリンクに対応するかだけを宣言する
6. If 内部向けの URL を取得できなかった, then the ポータル shall 該当するリンクを表示しないか利用不可として表示し、他のリンクを含むページ全体の表示を妨げない
7. When リンク一覧の宣言またはシークレットの値が変更され反映された, the ポータル shall 手作業の追加操作なしに変更後のリンク一覧を表示する

### Requirement 3: admin 限定導線の出し分け
**Objective:** As a 管理者, I want 共通ページから運用ダッシュボードへ直接移動したい, so that 起点を 1 つに保てる

#### Acceptance Criteria
1. While 利用者が共通ページを閲覧でき、かつ `groups` claim に `admin` を持つ, the ポータル shall 共通リンク集に加えて運用ダッシュボードへの導線を表示する
2. While 利用者が `groups` claim に `admin` を持たない, the ポータル shall 運用ダッシュボードへの導線を表示しない
3. The ポータル shall 導線の表示可否をサーバー側で判定し、admin 以外の利用者が admin 向けの表示内容を取得できる経路を持たない
4. The ポータル shall `executive`・`admin` との完全一致でグループを判定し、それらを部分文字列として含む別名のグループを該当ロールとして扱わない
5. The ポータル shall 利用者ごとに内容が異なる応答を、他の利用者が閲覧できる形でキャッシュさせない

### Requirement 4: 運用ダッシュボードのアクセス制御
**Objective:** As a 管理者, I want 運用情報を admin だけが閲覧できるようにしたい, so that 攻撃元 IP や送信元情報などの運用情報が一般メンバーに露出しない

#### Acceptance Criteria
1. While 利用者が `groups` claim に `admin` を持つ, the 運用ダッシュボード shall 運用情報を表示する
2. The 運用ダッシュボード shall アクセス可否を `admin` の有無だけで判定する
3. If `admin` を持たない認証済み利用者が運用ダッシュボードまたはその配下のデータにアクセスした, then the 運用ダッシュボード shall 内容を返さずに拒否する
4. If 未認証の利用者が運用ダッシュボードにアクセスした, then the 運用ダッシュボード shall 内容を返さずに Zitadel の認証へ誘導する
5. When Zitadel 上で利用者から `admin` ロールが外された, the 運用ダッシュボード shall 当該利用者のセッション更新以降にアクセスを拒否する

### Requirement 5: 課金・利用枠の表示
**Objective:** As a 管理者, I want 外部サービスの課金状況と利用枠の消費状況を 1 か所で確認したい, so that 各サービスの管理画面を巡回せずに想定外の課金や利用枠超過に気づける

#### Acceptance Criteria
1. The 運用ダッシュボード shall 外部サービスごとに、現在有効なプランをその適用期間とあわせて表示する
2. The 運用ダッシュボード shall API から取得できないプラン・利用上限・警告閾値・適用期間を、リポジトリ内のコードとして宣言された値から取得する
3. When あるサービスのプランを切り替えた, the 運用ダッシュボード shall プランの宣言を変更するだけで、切替後のプランと上限値に基づいて表示する
4. Where サービスが請求額を取得できる API を提供する (Cloudflare・GitHub organization), the 運用ダッシュボード shall 当期の請求額を表示する
5. The 運用ダッシュボード shall Hetzner Cloud で稼働中のサーバーの一覧と、公開料金と通信量 (送信量と無料枠の対比) から算出した当期の見込み費用を表示する
6. If Hetzner Cloud で稼働中のサーバーがコードで定義されたサーバーと一致しない (定義にない一時ノードの残存等), then the 運用ダッシュボード shall 差分となるサーバーを強調表示する
7. The 運用ダッシュボード shall 次の利用枠について、現在の使用量と上限を対比して表示する: Cloudflare Workers のリクエスト数、R2 の保存容量と Class A/B 操作数、Cloudflare Zero Trust の利用者数と上限、HCP Terraform の管理リソース数、Infisical の identity 数、Tailscale のユーザー数とデバイス数、Netdata Cloud の接続ノード数、Healthchecks.io のチェック数、UptimeRobot のモニター数、GitHub Actions と GitHub Packages の使用量。ただし使用量を取得する API がない項目 (Infisical の identity 数) は、宣言された上限と、使用量を取得できない旨の注記を表示する
8. The 運用ダッシュボード shall Hetzner Object Storage の現在の使用容量を合計とバケットごとに表示し、契約の基本料金に含まれる容量 (コードで宣言された値) と対比して表示する
9. While Cloudflare Workers が無料プランである, the 運用ダッシュボード shall リクエスト数を日次の上限と対比して表示する
10. While Cloudflare Workers が有料プランである, the 運用ダッシュボード shall リクエスト数を月次の含有量と対比して表示する
11. When 利用枠または基本料金に含まれる容量に対する使用量が、宣言された警告閾値を超えた, the 運用ダッシュボード shall 該当する項目を強調表示する
12. If 宣言された有料プランの適用期間を過ぎても、宣言または実際のプランが有料のままである, then the 運用ダッシュボード shall プランの戻し忘れとして強調表示する
13. If 課金・利用枠の情報源から取得できなかった, then the 運用ダッシュボード shall 取得失敗であることを正常状態と区別できる形で表示する

### Requirement 6: 既存監視の統合表示
**Objective:** As a 管理者, I want 既存監視の状態を各ツールに個別にログインせずに確認したい, so that 日常巡回の手間を減らせる

#### Acceptance Criteria
1. The 運用ダッシュボード shall UptimeRobot で監視している各モニターの現在の UP/DOWN 状態を表示する
2. The 運用ダッシュボード shall Healthchecks.io の各チェックの現在の状態を表示する
3. The 運用ダッシュボード shall ノードの詳細なメトリクスを確認するための参照先として Netdata へのリンクを提供する
4. The 運用ダッシュボード shall 既存監視を情報源として参照し、既存監視の監視対象・判定・通知を置き換えたり止めたりしない
5. If 既存監視の情報源から状態を取得できなかった, then the 運用ダッシュボード shall 取得失敗であることを正常状態と区別できる形で表示する

### Requirement 7: ノードのリソースと保守状態の表示
**Objective:** As a 管理者, I want prod-node-1 のリソース使用状況と保守状態をすぐ確認したい, so that メモリ逼迫・容量不足や、更新・再起動の積み残しに早く気づける

#### Acceptance Criteria
1. The 運用ダッシュボード shall prod-node-1 の CPU 使用率・メモリ使用率・ディスク使用率の現在値を、Netdata の詳細画面を開かずに確認できる簡易表示として表示する
2. The 運用ダッシュボード shall リソース使用状況を、クラスタ内または既存監視の既存の情報源から取得する
3. The 運用ダッシュボード shall 既に通知の対象となっている事象 (ディスク使用率 85% 超過等) についても、現在の状態として表示する
4. The 運用ダッシュボード shall 稼働中の K3s のバージョンと、利用可能な最新バージョンを表示する
5. The 運用ダッシュボード shall ノードの OS の未適用パッチの有無と、再起動が必要な状態かどうかを表示する
6. If リソース使用状況またはノードの保守状態を取得できなかった, then the 運用ダッシュボード shall 取得失敗であることを正常状態と区別できる形で表示する

### Requirement 8: クラスタとワークロードの状態表示
**Objective:** As a 管理者, I want クラスタ上のアプリケーションと基盤コンポーネントの状態を kubectl や ArgoCD の画面を開かずに確認したい, so that 同期漏れや異常の兆候に早く気づける

#### Acceptance Criteria
1. The 運用ダッシュボード shall ArgoCD の各 Application の同期状態とヘルス状態を表示する
2. The 運用ダッシュボード shall 再起動回数の多い Pod と、CrashLoopBackOff または OOMKilled の状態にある Pod を表示する
3. The 運用ダッシュボード shall cert-manager が管理する各証明書の有効期限を表示する
4. The 運用ダッシュボード shall 各 ExternalSecret と ClusterSecretStore の同期の準備状態を表示する
5. If クラスタの状態を取得できなかった, then the 運用ダッシュボード shall 取得失敗であることを正常状態と区別できる形で表示する

### Requirement 9: データ保護の状態表示
**Objective:** As a 管理者, I want データベースとメールデータのバックアップ状態を確認したい, so that バックアップが止まっていることに復旧が必要になる前に気づける

#### Acceptance Criteria
1. The 運用ダッシュボード shall 各 CNPG クラスタのヘルス状態、WAL アーカイブの状態、最後に成功した定期バックアップの日時を表示する
2. The 運用ダッシュボード shall VolSync による各バックアップの最終同期日時と結果を表示する
3. The 運用ダッシュボード shall 既に通知の対象となっている事象 (WAL アーカイブ失敗等) についても、現在の状態として表示する
4. If データ保護の状態を取得できなかった, then the 運用ダッシュボード shall 取得失敗であることを正常状態と区別できる形で表示する

### Requirement 10: 外部接続と CI の状態表示
**Objective:** As a 管理者, I want 外部との接続経路と CI の状態を確認したい, so that 公開経路の劣化や見落とされたワークフロー失敗に気づける

#### Acceptance Criteria
1. The 運用ダッシュボード shall cloudflared のトンネル接続の状態を表示する
2. The 運用ダッシュボード shall Tailscale の各デバイスのオンライン状態を表示する
3. The 運用ダッシュボード shall GitHub Actions のワークフローの直近の失敗を、Discord への通知を持たないワークフローも含めて表示する
4. The 運用ダッシュボード shall オープン中の Renovate のプルリクエストを表示する
5. The 運用ダッシュボード shall 既に通知の対象となっている事象 (DR トリガー等) についても、現在の状態として表示する
6. If 外部接続または CI の状態を取得できなかった, then the 運用ダッシュボード shall 取得失敗であることを正常状態と区別できる形で表示する

### Requirement 11: メール配送の状態表示
**Objective:** As a 管理者, I want メールサーバーの配送状況と TLS の配送報告を確認したい, so that 配送の滞留や失敗、TLS 配送の問題に早く気づける

#### Acceptance Criteria
1. The 運用ダッシュボード shall mailserver のメールキューの滞留状況と、配送に失敗したメールの状況を表示する
2. The TLS-RPT 集計機能 shall DMARC 集計レポートと同じ受信先に届く TLS-RPT レポートを取り込み、報告元・対象期間・成功/失敗の件数を保存する
3. The 運用ダッシュボード shall 直近期間の TLS-RPT の集計結果を表示する
4. If 保存済みの TLS-RPT の集計データが失われた, then the TLS-RPT 集計機能 shall 受信先に残っているレポートから再取り込みできる
5. If メール配送の状態または TLS-RPT の集計を取得できなかった, then the 運用ダッシュボード shall 取得失敗であることを正常状態と区別できる形で表示する

### Requirement 12: Zitadel 認証イベントの表示
**Objective:** As a 管理者, I want Zitadel の認証イベントを確認したい, so that 認証基盤への攻撃や異常なログイン失敗に気づける

#### Acceptance Criteria
1. The 運用ダッシュボード shall Zitadel の直近の認証イベント (ログイン失敗等) の一覧と、期間ごとの件数を表示する
2. The 運用ダッシュボード shall 認証イベントを Zitadel の認証ポリシー (パスワード・ロック・MFA の判定) を変更せずに取得する
3. If 認証イベントを取得できなかった, then the 運用ダッシュボード shall 取得失敗であることを正常状態と区別できる形で表示する

### Requirement 13: Falco 検知統計の表示
**Objective:** As a 管理者, I want Falco の検知状況を統計として確認したい, so that Discord の通知を遡らずに侵入検知の傾向や直近の異常に気づける

#### Acceptance Criteria
1. The Falco 統計機能 shall Falco が出力する検知イベントを収集して保存し、既存の通知先 (Discord) への通知を止めたり置き換えたりしない
2. The 運用ダッシュボード shall 期間ごとの検知件数を、ルール別と優先度 (priority) 別に表示する
3. The 運用ダッシュボード shall 直近の検知イベントの一覧 (発生時刻・ルール・優先度・対象の Pod/コンテナ・概要) を表示する
4. The Falco 統計機能 shall 保存する検知イベントの保持期間を定め、保持期間を過ぎたデータを削除する
5. If Falco 統計機能が停止していた, then the Falco 統計機能 shall 停止中に発生した検知イベントが統計から欠落し得ることとし、既存の通知先 (Discord) への通知は影響を受けない
6. If 保存済みの検知データが失われた, then the Falco 統計機能 shall 失われた期間の統計を復元せず、以降の検知イベントから収集を再開する
7. If 検知イベントの収集・保存に失敗した, then the Falco 統計機能 shall 失敗したことを管理者が確認できる形で記録し、Falco 本体の検知と既存の通知を妨げない

### Requirement 14: fail2ban BAN 状況の表示
**Objective:** As a 管理者, I want fail2ban の BAN 状況を Pod 内でコマンドを実行せずに確認したい, so that ブルートフォース攻撃の発生と対処状況をすぐ把握できる

#### Acceptance Criteria
1. The 運用ダッシュボード shall mailserver の fail2ban の jail ごとに、現在 BAN されている IP の一覧と件数を表示する
2. The fail2ban 状況取得機能 shall BAN の状態を読み取るだけで、BAN の追加・解除を行わない
3. If fail2ban の状態を取得できなかった, then the 運用ダッシュボード shall 取得失敗であることを正常状態と区別できる形で表示する

### Requirement 15: DMARC 集計レポートの表示
**Objective:** As a 管理者, I want aramakisai.com 宛ての DMARC 集計レポートを継続的に確認したい, so that なりすましメールの発生や SPF/DKIM 整合率の悪化に早く気づける

#### Acceptance Criteria
1. The DMARC 集計機能 shall aramakisai.com の DMARC レコードで指定された集計レポート (RUA) の受信先に届くレポートを取り込む
2. When 新しい DMARC 集計レポートを取り込んだ, the DMARC 集計機能 shall 報告元・対象期間・送信元 IP・件数・SPF/DKIM/DMARC の評価結果を解析して保存する
3. When 同じ集計レポートを再度取り込んだ, the DMARC 集計機能 shall 重複して計上しない
4. When 管理者が運用ダッシュボードで DMARC の情報を確認した, the 運用ダッシュボード shall 直近期間の集計結果 (送信元別の pass/fail 件数等) を表示する
5. If 集計レポートの取り込みまたは解析に失敗した, then the DMARC 集計機能 shall 失敗したことを管理者が確認できる形で記録し、他のレポートの取り込みを継続する
6. If 保存済みの集計データが失われた, then the DMARC 集計機能 shall 受信先に残っているレポートから再取り込みできる
7. The DMARC 集計機能 shall DMARC・SPF・DKIM のポリシー値を変更しない

### Requirement 16: 宣言的な構成管理と保守性
**Objective:** As a インフラ担当者, I want ポータルとダッシュボードの構成がすべてリポジトリから再現できるようにしたい, so that 引き継ぎ後の運営チームでも保守・再構築できる

#### Acceptance Criteria
1. The ポータルと運用ダッシュボード shall デプロイ構成・認証設定・表示内容をすべてリポジトリ内のコード (GitOps/IaC) として宣言する
2. The ポータルと運用ダッシュボード shall WebUI 上の操作でしか再現できない設定を持たない
3. The ポータルと運用ダッシュボード shall マニフェストに平文のシークレットを含まず、必要な認証情報を Infisical から ExternalSecret 経由で取得する
4. The ポータルと運用ダッシュボード shall 使用するコンテナイメージ・配布物のバージョンを固定する
5. When クラスタを再構築した, the ポータルと運用ダッシュボード shall 手作業の設定なしに ArgoCD の同期だけで同じ表示内容を復元する (DMARC・TLS-RPT・Falco の保存済みデータを除く)

### Requirement 17: メモリ予算内での運用
**Objective:** As a インフラ担当者, I want 追加するコンポーネントが prod-node-1 のメモリ予算内に収まることを確認したい, so that 既存サービスの OOM や性能劣化を起こさずに導入できる

#### Acceptance Criteria
1. The 本仕様で追加する各コンポーネント shall Kubernetes の `resources.requests` と `resources.limits` (メモリ) を設定する
2. When 全コンポーネントを投入した, the インフラ担当者 shall 各コンポーネントの実メモリ使用量を実測し、設定した requests/limits と整合することを確認する
3. When 全コンポーネント投入後に prod-node-1 全体のメモリ使用率を確認した, the インフラ担当者 shall 既存のメモリ予算の判断基準に照らして許容範囲内であることを確認する
4. If 実測値が設定値や予算を超えた, then the インフラ担当者 shall 上限を引き上げる前に増加要因を特定する

### Requirement 18: ドキュメント同期
**Objective:** As a インフラ担当者, I want 導入した構成が主要ドキュメントに反映されるようにしたい, so that 将来の運用者が現在の構成を正しく把握できる

#### Acceptance Criteria
1. When 実装が完了した, the インフラ担当者 shall README のデプロイされるサービス一覧と `.kiro/steering/structure.md` に新規サービスとサブドメインを反映する
2. When 新規のシークレット (内部向けリンクの URL を含む) を追加した, the インフラ担当者 shall `.kiro/steering/tech.md` のシークレット一覧にキー名を追記する (値は含めない)
3. When 実装が完了した, the インフラ担当者 shall `.kiro/steering/tech.md` の監視スタックの節に運用ダッシュボードの位置付けを反映する
4. The インフラ担当者 shall リンク一覧の追加・変更手順と、`executive`・`admin` の付与・剥奪がポータルとダッシュボードの閲覧可否に反映される仕組みを運用者向けに文書化する
