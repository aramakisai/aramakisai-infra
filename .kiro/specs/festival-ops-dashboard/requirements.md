# Requirements Document

## Project Description (Input)
荒牧祭当日(2026-11-14〜15)に本部で常時表示する監視画面を、既存の ops-dashboard(gitops/manifests/prod/ops-dashboard、collector はサーバー側描画の Python)に `/admin/festival` として追加する。

既存画面には次の不足がある。
- ブラウザ側に自動更新が無い(手動の再読み込みボタンのみ)
- 当日に要る指標と平時の課金・DMARC 等が混在している
- 来場者影響の指標が無い

要件の骨子:
- 15〜30 秒ごとに自動で更新する。
- 来場者影響 → 機能別 → 容量 → 基盤・セキュリティの順に、赤・黄・緑のタイルで表示する。
- 赤になった項目は既存の Discord ops webhook に通知する。
- 新しいメトリクス保存基盤は置かない(.kiro/steering/tech.md の監視スタック節)。

確認済みの取得元:
- Cloudflare GraphQL(既存の読み取りトークン OPS_CLOUDFLARE_READ_TOKEN。account スコープで、1分粒度・遅延1〜2分)
  - workersInvocationsAdaptive: Worker 別の requests・errors・cpuTime 分位
  - account スコープの httpRequestsAdaptiveGroups: cms ホストの件数・ステータス・originResponseDuration 分位。requestSource は eyeball と edgeWorkerFetch に限る。edgeWorkerCacheAPI の 504 は Worker のキャッシュ照会の記録なので除外する。
  - durableObjectsPeriodicGroups: サイネージハブの activeWebsocketConnections
  - durableObjectsInvocationsAdaptiveGroups: 地震リレー cron が呼ぶ jsrpc。workersInvocationsScheduled は 2026-10-27 以降の記録が欠けているため使わない。
  - Analytics Engine aramakisai_stamp_metrics: outcome と d1Failed。スタンプ記録の失敗は HTTP 200 で返るため、ここで判定する。
- 既存の collector ソース: k8s(Pod Ready/再起動/OOM、CNPG、metrics-server、全ノードのディスク)、Tunnel の接続数、UptimeRobot、Healthchecks、CI の dr-incident / infra-alert Issue、fail2ban、Falco
- 地震リレー Worker の `/status`(Bearer `SIGNAGE_QUAKE_TOKEN`)

現時点で読み取り手段が無く、設計で扱いを決める必要があるもの:
- 駐車場 AI 判定の成否(CMS に読み取り用 API が無い)
- サイネージハブの内部状態
- DB の接続数と replica 遅延
- バスの ODPT 取得状況(ブラウザが直接取得している)

## Introduction
荒牧祭当日は、来場者に影響する異常を数分以内に把握し、対応に移る必要がある。現行の運用ダッシュボード (`/admin/`) は日常巡回向けの集約画面で、手動でしか更新されず、課金や DMARC など当日には不要な情報と混在しており、来場者影響や機能ごとの健全性を示す指標を持たない。本仕様は、当日に本部で常時表示する監視画面 (当日監視画面) を運用ダッシュボードに追加し、来場者影響・機能別の健全性・容量・基盤とセキュリティの状態を、自動更新される 3 段階の状態表示として一覧できるようにする。あわせて、異常状態に入った項目を Discord に通知する。

## Boundary Context
- **In scope**:
  - 運用ダッシュボード配下の当日監視画面 (`/admin/festival`) と、その自動更新・状態判定・表示
  - 当日監視画面のための情報源の追加 (Cloudflare GraphQL・Analytics Engine・地震リレー Worker の状態・ODPT の公開フィード・Kubernetes)
  - 現状は外部から読み取れない状態を読み取れるようにするための、情報源側への読み取り専用の口の追加 (aramakisai-web の CMS における駐車場判定の状態の記録と読み取り、サイネージハブの接続元ごとの接続数、地震リレーの定期起動の最終実行時刻と読み取り専用の認証)
  - 状態判定の閾値・対象・期待値のコードによる宣言
  - 推移の表示のための、既存の運用ダッシュボードの SQLite への 24 時間分の 1 分ごとの値の保持
  - 異常状態に入った項目と、そこから回復した項目の Discord 通知
- **Out of scope**:
  - 既存の運用ダッシュボード (`/admin/`) の表示内容・判定の変更
  - 既存の監視・通知 (UptimeRobot・Healthchecks.io・Falco・Falcosidekick・DR Trigger・Infra Health Check・駐車場カメラ停止通知・地震リレーの Discord 通知) の置き換えや停止
  - メトリクスを時系列で保存する基盤 (Prometheus・VictoriaMetrics 等) の導入
  - 異常を自動で是正する処理 (スケール・再起動・切り替え等)。当日監視画面は表示と通知だけを行う
  - 当日の体制・担当者・連絡経路の定義 (WBS で管理する)
  - 来場者向けサイト・サイネージ・CMS の機能そのものの変更
- **Adjacent expectations**:
  - アクセス制御は既存の運用ダッシュボードと同じ認証・認可 (Zitadel の `admin` グループ) に従う
  - シークレットは Infisical + ExternalSecret の既存規約に従う
  - aramakisai-web 側に追加する読み取り用の口は、aramakisai-web の既存の認証方式とデプロイ手順に従う

## Requirements

### Requirement 1: 当日監視画面の提供とアクセス制御
**Objective:** As a 管理者, I want 当日に必要な状態だけを集めた画面を開きたい, so that 平時の情報に埋もれずに異常を把握できる

#### Acceptance Criteria
1. The 当日監視画面 shall 運用ダッシュボード配下の `/admin/festival` で提供される
2. The 当日監視画面 shall 既存の運用ダッシュボードと同じく、利用者が `groups` claim に `admin` を持つ場合だけ内容を表示する
3. If `admin` を持たない利用者または未認証の利用者が当日監視画面にアクセスした, then the 当日監視画面 shall 既存の運用ダッシュボードと同じ方法で拒否または認証へ誘導する
4. The 当日監視画面 shall 課金・利用枠・DMARC・TLS-RPT・メール配送の集計など、当日の異常把握に用いない情報を表示しない
5. The 当日監視画面 shall 既存の運用ダッシュボード (`/admin/`) の表示と動作を変えない

### Requirement 2: 自動更新と情報の鮮度
**Objective:** As a 本部で画面を見ている運営者, I want 画面を操作しなくても最新の状態が表示されてほしい, so that 常時表示のまま異常に気づける

#### Acceptance Criteria
1. While 当日監視画面がブラウザで開かれている, the 当日監視画面 shall 利用者の操作なしに 30 秒以内の間隔で表示を更新する
2. The 当日監視画面 shall 各項目に、その値を情報源から取得した時刻を表示する
3. If ある項目の値が、その項目に宣言された鮮度の上限より古い, then the 当日監視画面 shall 当該項目を正常とも異常とも区別できる「取得遅延」の状態として表示する
4. If ある情報源から値を取得できなかった, then the 当日監視画面 shall 当該情報源に依存する項目を「取得失敗」として表示し、他の情報源の項目の表示を続ける
5. If 画面の更新そのものが失敗した (ネットワーク断・セッション切れ等), then the 当日監視画面 shall 表示中の内容が古いことを画面上で明示する

### Requirement 3: 状態判定と表示
**Objective:** As a 本部で画面を見ている運営者, I want 各項目の良し悪しが一目で分かってほしい, so that 数値を読み解かずに対応の要否を判断できる

#### Acceptance Criteria
1. The 当日監視画面 shall 各項目を「正常」「注意」「異常」の 3 段階で判定し、色と文字の両方で区別できる形で表示する
2. The 当日監視画面 shall 項目を来場者影響、機能別、容量、基盤・セキュリティの 4 区分のタブに分け、利用者の操作でタブを切り替えて表示する
3. The 当日監視画面 shall 各項目の判定に用いる閾値・対象・期待値 (サイネージの設置台数等) を、リポジトリ内のコードとして宣言された値から取得する
4. When 閾値の宣言を変更した, the 当日監視画面 shall 宣言の変更だけで新しい閾値に基づいて判定する
5. The 当日監視画面 shall タブに関わらず常に表示される画面上部の帯に、現在「異常」と「注意」の項目の件数、「異常」の項目名、最終更新時刻を表示する
6. The 当日監視画面 shall 各項目のタイルに、判定・現在値・判定の根拠となった閾値・注記・データの時刻を、項目を選択しなくても常に表示する
7. When 利用者が項目を選択した, the 当日監視画面 shall 画面を遷移せずに同じ画面内の詳細表示に当該項目の推移と内訳を表示し、参照先 (既存の運用ダッシュボードの該当節、Cloudflare ダッシュボード、Netdata 等) へのリンクは補助として添える
8. The 当日監視画面 shall どのタブも 1920×1080 の表示領域にスクロールなしで収める
9. The 当日監視画面 shall 各タブの見出しに、その区分内で最も悪い判定と「異常」の件数を表示する
10. While 自動更新が行われている, the 当日監視画面 shall 利用者が選択中のタブを維持する
11. The 当日監視画面 shall 数値を持つ項目 (Ready・定期起動・Falco など状態だけを表す項目を除く) のタイルに、直近 60 分の値の推移を小さな折れ線 (スパークライン) で表示する
12. When 利用者が数値を持つ項目を選択した, the 当日監視画面 shall 当日の値の推移を折れ線で表示し、閾値が宣言されている項目には注意と異常の閾値を線で重ねて表示する
13. If 推移の途中に値を記録できなかった期間 (取得失敗・取得遅延等) がある, then the 当日監視画面 shall その期間を線の途切れとして表示し、値を補間しない
14. When 利用者が内訳を持つ項目を選択した, the 当日監視画面 shall 情報源がすでに取得している内訳 (Pod・DB の instance・ノード・駐車場カメラ・地震リレーの上流・応答ステータス等ごとの値) を表で表示する
15. The 当日監視画面 shall 各タブのタイルの大きさを、そのタブの項目数に応じて表示領域を使い切るように決め、項目の少ないタブでは大きく、多いタブでは小さく表示する
16. While 自動更新が行われている, the 当日監視画面 shall 利用者が選択中の項目の詳細表示を維持し、推移と内訳を最新の値に更新する

### Requirement 4: 来場者影響の監視
**Objective:** As a 管理者, I want 来場者がエラーを受けているかを把握したい, so that 来場者影響のある異常を最優先で対応できる

#### Acceptance Criteria
1. The 当日監視画面 shall 来場者向けサイトの Worker (`aramakisai-web`) について、直近の時間窓におけるリクエスト数・エラー率・リソース超過の件数・CPU 時間の上位分位を表示し判定する
2. The 当日監視画面 shall CMS (`cms.aramakisai.com`) について、来場者からの直接要求と Worker から CMS への取得要求に限った、直近の時間窓におけるリクエスト数・5xx の比率・オリジン応答時間の上位分位を表示し判定する
3. The 当日監視画面 shall CMS の 5xx の集計から、Worker のキャッシュ照会の記録 (オリジンへの要求ではないもの) を除外する
4. The 当日監視画面 shall CMS メディア配信 (`/api/media/`) について、直近の時間窓における 4xx・5xx の件数を表示し判定する
5. The 当日監視画面 shall UptimeRobot の各モニターの現在の UP/DOWN 状態を表示し判定する
6. The 当日監視画面 shall 外部集計の反映遅延によって直近の値が過小に見えることを、判定に用いる時間窓の取り方で避ける

### Requirement 5: 機能別の健全性の監視
**Objective:** As a 管理者, I want 当日稼働する機能ごとにデータが更新され続けているかを把握したい, so that HTTP の成否に現れない機能停止にも気づける

#### Acceptance Criteria
1. The 当日監視画面 shall 駐車場カメラごとに、最後に画像を受け取った時刻を表示し、宣言された経過時間を超えたら判定に反映する
2. The 当日監視画面 shall 駐車場カメラごとに、混雑判定 (生成 AI による判定) が最後に成功した時刻、最後に失敗した時刻、直近のエラー、連続失敗回数を表示し判定する
3. The 当日監視画面 shall サイネージハブへの WebSocket 接続数のうち本番サイトからの接続数を表示し、宣言されたサイネージ設置台数を下回ったら判定に反映する
4. The 当日監視画面 shall 地震リレーの定期起動が宣言された間隔で継続しているかを表示し判定する
5. The 当日監視画面 shall 地震リレーの上流ごとの接続状態と最終受信時刻、および Discord 通知の最終成否を表示し判定する
6. The 当日監視画面 shall スタンプラリーについて、直近の時間窓における記録失敗・D1 失敗・レート制限の件数を、HTTP ステータスではなく処理結果に基づいて表示し判定する
7. The 当日監視画面 shall バスのリアルタイム情報の公開フィードについて、取得の成否とフィードの生成時刻を表示し判定する
8. While 宣言された開催期間・監視時間の外である, the 当日監視画面 shall 時間帯によって停止するのが正常な項目 (駐車場の監視時間外等) を異常と判定しない

### Requirement 6: 容量の監視
**Objective:** As a 管理者, I want 資源の逼迫を障害になる前に把握したい, so that スケールや切り替えを間に合うタイミングで判断できる

#### Acceptance Criteria
1. The 当日監視画面 shall CMS の Pod について、Ready 数と宣言数の対比、直近の再起動・OOM の有無、CPU 使用量と limit の対比を表示し判定する
2. The 当日監視画面 shall CMS の DB (CNPG `directus-db`) の primary について、CPU 使用量と limit の対比、接続数と上限の対比を表示し判定する
3. The 当日監視画面 shall CMS の DB の replica について、primary からの遅延を表示し判定する
4. The 当日監視画面 shall 全ノードについて、Ready 状態、CPU・メモリ・ルートディスクの使用率を表示し判定する
5. The 当日監視画面 shall Cloudflare Tunnel の状態と接続数を表示し判定する
6. The 当日監視画面 shall CMS の DB の各 instance について、ページキャッシュを含まないメモリ使用量と memory limit の対比を表示し判定する
7. The 当日監視画面 shall CMS の DB の各 instance について、直近の再起動・OOM・再起動の繰り返しの有無を表示し判定する
8. The 当日監視画面 shall CMS の DB の各 instance について、クライアントの接続数を表示し判定する
9. The 当日監視画面 shall Cloudflare Tunnel のコネクタ (cloudflared) の Pod について、CPU 使用量と limit の対比を表示し判定する
10. The 当日監視画面 shall CMS と cloudflared の Pod について、CPU のスロットリングの比率を表示し判定する
11. The 当日監視画面 shall Cloudflare から CMS に届いたリクエストのうち、来場者からの直接要求 (メディア配信を除く) と Worker から CMS への取得要求の、直近の時間窓における毎秒のレートを表示し判定する
12. The 当日監視画面 shall Worker のキャッシュ照会 (CMS ホストへの GET) のうち、キャッシュにヒットした比率を表示する
13. If 項目の閾値が宣言されていない (CMS のキャッシュのヒット率), then the 当日監視画面 shall 当該項目を判定せず、判定していないことが分かる形で値だけを表示し、通知しない
14. The 当日監視画面 shall CMS の DB の primary について、統計情報が一度も取得されていない表の数を表示し判定する

### Requirement 7: 基盤とセキュリティの監視
**Objective:** As a 管理者, I want 既存の監視が発報している事象も同じ画面で把握したい, so that 通知を見落としても画面から気づける

#### Acceptance Criteria
1. The 当日監視画面 shall 対応中の `dr-incident` および `infra-alert` の Issue の有無と一覧を表示し判定する
2. The 当日監視画面 shall 直近の時間窓における Falco の検知件数を優先度別に表示し、宣言された優先度以上の検知を判定に反映する
3. The 当日監視画面 shall IdP (Zitadel) と CMS の DB を経由する応答の可否を表示し判定する
4. The 当日監視画面 shall Healthchecks.io の各チェックの状態を表示し判定する
5. The 当日監視画面 shall fail2ban の BAN のうち、クラスタ内部またはノードのアドレスに対するものを判定に反映する

### Requirement 8: Discord 通知
**Objective:** As a 管理者, I want 画面を見ていない時にも異常を知りたい, so that 本部を離れていても対応に移れる

#### Acceptance Criteria
1. When ある項目が「異常」に入った, the 当日監視画面 shall 既存の Discord ops webhook に、項目名・現在値・閾値・当日監視画面への参照を通知する
2. When 「異常」だった項目が「正常」に戻った, the 当日監視画面 shall 回復したことを同じ送信先に通知する
3. While ある項目が「異常」のままである, the 当日監視画面 shall 宣言された再通知間隔より短い間隔で同じ項目を再通知しない
4. If 情報源の取得失敗によって多数の項目が同時に判定できなくなった, then the 当日監視画面 shall 項目ごとではなく情報源単位で 1 件にまとめて通知する
5. If Discord への送信に失敗した, then the 当日監視画面 shall 送信失敗を当日監視画面に表示し、表示と判定を続ける
6. The 当日監視画面 shall 既存の監視ツールの通知の対象・送信先・内容を変更しない
7. While 当日監視画面の判定処理が動作している, the 当日監視画面 shall 当日監視画面に依存しない外部の監視サービス (Healthchecks.io) に定期的に生存を通知し、判定処理が停止したときにその外部の監視サービスから通知されるようにする

### Requirement 9: 運用上の制約
**Objective:** As a インフラ担当者, I want 監視の追加が本番の負荷や秘密情報の扱いを損なわないようにしたい, so that 当日の監視自体が障害の原因にならない

#### Acceptance Criteria
1. The 当日監視画面 shall メトリクスを時系列で保存する基盤 (Prometheus・VictoriaMetrics 等) を新たに置かず、推移の表示に用いる値は既存の運用ダッシュボードの SQLite に 1 分ごとに保持し、24 時間を過ぎた値を削除する
2. The 当日監視画面 shall 外部 API (Cloudflare GraphQL・Analytics Engine・ODPT 等) への問い合わせを、各 API の公開されたレート制限の範囲内に収める
3. The 当日監視画面 shall 情報源への問い合わせを読み取りに限り、監視対象の状態を変更しない
4. The 当日監視画面 shall トークン等のシークレット値を画面・通知・ログに出力しない
5. The 当日監視画面 shall 来場者の個人を識別できる情報 (IP アドレス・端末識別子等) を画面・通知に表示しない
6. The 当日監視画面 shall 追加する処理のメモリ使用量を、既存の運用ダッシュボードのリソース予算の見直しとあわせて確認できる形で示す
7. If 当日監視画面の処理が停止した, then the 当日監視画面 shall 既存の運用ダッシュボードと既存の監視・通知の動作に影響を与えない
