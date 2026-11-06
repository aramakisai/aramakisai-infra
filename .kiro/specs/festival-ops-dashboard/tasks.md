# Implementation Plan

- [ ] 1. 外部の前提: Access のサービストークン・生存確認のチェック・シークレット・権限・DB のカスタムクエリ
- [x] 1.1 サイネージの接続数の読み取りに限った Cloudflare Access の経路を用意し、優先の挙動を実測する
  - サイトの `/api/signage/stats` だけを宛先にした専用の Access アプリと、collector 用のサービストークン、そのトークンだけを許可するポリシーを Terraform に足す
  - サービストークンの有効期間・更新の余裕・作り直しの順序は、既存のサイネージ通知用トークンと同じにし、ID と secret を既存と同じ手順で取り出せる出力にする
  - 対象のリソースに限って適用したあと、実装の最初の検証として、collector のトークンで `/api/signage/stats` の Access を通過でき、同じトークンで `/api/signage/notify` と `/api/signage/ws` は通過できないことを実測する
  - 公開 HTTP アプリ同士の重なりで専用アプリが優先されず、上記の分離が成り立たない場合は、原因を添えてユーザーに諮り、回答が出るまで `festival.signage` を無効の宣言で止めて他の作業を進める
  - 完了時: 実測で stats だけに届き、notify・ws には届かないことが確認され、トークンの ID と secret が Infisical の所定のキーに登録されている
  - _Requirements: 5.3, 9.3, 9.4_

- [x] 1.2 当日監視の生存確認のチェックを Healthchecks.io に作る
  - 既存の mailserver バックアップのチェックと同じ形で、チェック `festival-monitor` (初期値は仮の 1 分周期・3 分猶予) と、その ping URL の sensitive な出力を足す
  - 対象のリソースに限って適用し、ping URL を Infisical の所定のキーに手動で登録する
  - Healthchecks.io からの通知先は、既存の前例どおりコンソールで手動設定する (Terraform では管理しない)
  - 完了時: チェックが Healthchecks.io に存在し、通知先が設定済みで、ping URL が Infisical に登録されている
  - _Requirements: 8.7, 9.4_

- [x] 1.3 collector と CMS に渡すシークレットと、collector の読み取り権限を足す
  - 当日専用の Discord webhook、生存確認の ping URL、地震リレーの読み取り専用トークン、駐車場の状態の読み取りトークン、Access のサービストークンの ID と secret を、collector-secrets の ExternalSecret に足す
  - 駐車場の状態の読み取りトークンを、cms-secrets の ExternalSecret にも足す (正本は Infisical の同じキー)
  - 地震リレーと駐車場のトークンはランダム値で生成し、web 側の本番デプロイより前に Infisical prod に登録されていることを確かめる
  - collector の ClusterRole に、Pod のメトリクスの get・list、Deployment の get、kubelet のメトリクス (`nodes/metrics`) の get を足す
  - 完了時: 両 ExternalSecret のマニフェストに新しいキーが列挙され、ClusterRole に 3 つの権限が加わっている。シークレット値はリポジトリに含まれない
  - _Requirements: 6.1, 6.2, 6.9, 6.10, 9.3, 9.4_

- [ ] 1.4 (P) CMS の DB に、統計情報の無い表を数えるカスタムクエリを足す
  - design の「統計情報の無い表のカスタムクエリ」どおりの ConfigMap を、CMS の DB と同じ namespace に、自動で読み直させるラベル付きで足し、CMS の Application の対象に加える
  - CMS の DB の Cluster の監視の設定から、その ConfigMap のキーを参照する。対象のデータベースは CMS のデータベースに限り、primary だけで実行する
  - 完了時: `kustomize build` が通り、本番への反映後に primary の 9187 番に統計情報の無い表の数の指標が出て、replica には出ないことを確かめている
  - _Requirements: 6.14_
  - _Boundary: festival.cnpg_

- [x] 2. collector の共通基盤: HTTP・宣言・保存・既存画面との分離
- [x] 2.1 HTTP クライアントに、応答のメタ情報の取得と、URL を伏せたエラー文を足す
  - 応答の状態コード・ヘッダ・本文を返す取得手段を足す (Discord のレート制限ヘッダの読み取りに使う)
  - 429 の応答で、`Retry-After` の秒数をエラーに持たせる
  - URL を含めないエラー文を生成できるようにし、webhook や ping URL のトークンがログ・画面・例外のメッセージに残らないようにする
  - 完了時: 429 の `Retry-After` がエラーから読めること、URL を伏せる指定でエラー文に URL が残らないことを単体テストで確かめている
  - _Requirements: 8.3, 9.2, 9.4_

- [x] 2.2 当日監視の宣言 `[festival]` の読み込みと検証を足す
  - 画面の URL・再通知の間隔・情報源の失敗の猶予・情報源ごとの鮮度の上限・閾値・比率の判定に要る最低要求数・各取得先の対象 (cloudflared の namespace と Pod のラベルセレクタを含む)・CMS と DB の Pod の直近の再起動を見る窓 (`restart_window`)・UptimeRobot のモニター名・Falco の窓と優先度・無効にする情報源を、型付きの値として読み込む
  - 宣言が無い場合は「未設定」として扱い、当日監視の取得・評価・通知を何もしない状態にする
  - 未知のキー、閾値名の欠け、注意と異常の大小の逆転、未知の source_id を、起動の失敗にする。省略できる閾値名 (Cache API のヒット率) だけは欠けてよい
  - `dashboard.toml` に `[festival]` 一式を design の初期値 (仮) で足す。取得先の URL・Pod のラベルセレクタ・CMS Service の基底 URL は、既存のマニフェスト (cms Deployment の env、web 側の ODPT の基底 URL) の値と一致させる
  - `[[credentials]]` に collector 用の Access サービストークンの期限を足す
  - 完了時: 欠け・逆転・未知のキー・未知の source_id が起動の失敗になり、省略できる閾値名の欠けは失敗にならないことを単体テストで確かめ、リポジトリの `dashboard.toml` が検証を通る
  - _Requirements: 3.3, 3.4, 6.13_

- [x] 2.3 当日監視の評価結果と通知の状態を保存する表を SQLite に足す
  - 評価結果 (項目ごとの最新値。表示のみの項目かどうかを含む)、通知の状態 (項目または情報源ごとの異常の期間)、評価器・通知器・生存確認の状態 (1 行) の 3 表を、既存の表と同じく起動時に作る
  - 評価結果の置き換えは 1 トランザクションで行い、評価時刻を更新する。通知の状態の更新・削除と通知器の状態の更新を 1 回の書き込みで行う
  - 生存確認の成否 (成功時刻・失敗時刻・失敗理由) を状態の行に書くメソッド (`save_festival_heartbeat`) を用意する
  - 3 表は最新値だけを持ち、既存の保持期間の削除の対象にしない
  - 完了時: 評価結果の置き換え・読み出し、通知の状態の保存・復元、評価の失敗理由の記録が、一時 SQLite に対する単体テストで確かめられている
  - _Requirements: 2.2, 6.13, 8.5, 9.1_

- [x] 2.4 既存の運用ダッシュボードから当日監視の結果を切り離す
  - 描画を伴わない取得結果の写しを返す手段を Collector に足す (評価器が使う)
  - `/admin/` に渡す結果から `festival.` で始まる情報源を除く。既存の画面の描画と要確認の集計の処理そのものは変えない
  - 完了時: `festival.*` の結果を異常で入れても `/admin/` の要確認に現れないことをテストで確かめている
  - _Requirements: 1.5, 9.7_

- [x] 3. aramakisai-web の読み取りの口 (別リポジトリの PR、base は dev)
- [x] 3.1 (P) 地震リレーに、定期起動の最終実行時刻と読み取り専用トークンを足す
  - 任意の設定として読み取り専用トークンを足し、`GET /status` は既存のトークンと読み取り専用トークンのどちらでも認可する。`/dismiss` と開発用の注入の経路は既存のトークンだけで認可する
  - 定期起動の経路だけが最終実行時刻を DO の保存領域に記録してから接続の維持を行うようにし、alarm の経路は時刻を更新しない
  - `/status` の応答に定期起動の最終実行時刻を足す
  - frontend-ci のリレーの secrets-file に読み取り専用トークンを足す
  - 完了時: vitest で、`/status` が両方のトークンで 200、`/dismiss` が読み取り専用トークンで 401、定期起動だけが最終実行時刻を更新し alarm は更新しないことを確かめている
  - _Requirements: 5.4, 5.5, 9.3_
  - _Boundary: QuakeRelay status_

- [x] 3.2 (P) サイネージハブに接続元ごとの接続数を数える口を足す
  - サイトの入口で、クライアントが送った接続元のヘッダを削除し、デプロイ時の既存の環境の値 (`prod`・`dev`・`preview`、無ければ `unknown`) を接続元としてハブに渡す
  - ハブは WebSocket の受け入れ時に接続元のタグを付け、タグごとの接続数 (タグの無い接続は `unknown`) と計数時刻を返す
  - サイトの入口に `GET /api/signage/stats` を足し、GET 以外は 405 を返す。認可は Cloudflare Access に任せる
  - 応答に IP や端末識別子を含めない
  - 完了時: vitest で、接続元のタグの計数、クライアントの送ったヘッダが上書きされること、GET 以外の 405 を確かめている
  - _Requirements: 5.3, 9.5_
  - _Boundary: SignageHub stats, SiteEntry_

- [x] 3.3 (P) CMS の駐車場カメラに、混雑判定の状態を記録する
  - カメラに判定の状態の 4 項目 (最終成功時刻・最終失敗時刻・直近のエラー・連続失敗回数) を足す。人は管理画面でも API でも書けず、管理画面には読み取り専用で表示する
  - 列を足すだけの migration を 1 本足す
  - 判定の結果の記録と同じ時点で、成功 (品質不足を含む) は成功時刻の更新と連続失敗の 0 化、失敗とレート制限による見送りは失敗時刻・エラー (先頭 200 文字)・連続失敗の加算を記録する。同一窓の見送りと置き換えは記録しない
  - 連続失敗の加算は SQL の 1 文で行い、並行する判定でも数え漏らさない
  - カメラの更新の失敗は警告として記録し、判定結果の公開は止めない
  - 完了時: vitest で、成功でのリセット、失敗とレート制限での加算、同一窓の見送り・置き換えでの不変、並行の加算を確かめ、migration の up・down が通る
  - _Requirements: 5.2_
  - _Boundary: ParkingJudgeHealth_

- [x] 3.4 CMS に、駐車場カメラの受信と判定の状態を返す読み取り専用の endpoint を足す
  - `GET /api/ops/parking-health` だけを登録し、Bearer トークンを環境変数のトークンと長さに依存しない定数時間の比較で照合する。未設定は 503、無い・不一致は 401
  - カメラごとに、名前・割り当て先の駐車場名・最終受信時刻・端末の受信時刻・判定の状態の 4 項目・駐車場の状態の更新時刻を、深さ 0 で読み出して返す。書き込みはしない
  - トークンのハッシュ・電波強度などの端末の詳細・画像の URL は応答に含めない
  - 完了時: vitest で、トークン無し 401、不一致 401、未設定 503、応答の形と含めない項目を確かめている
  - _Depends: 3.3_
  - _Requirements: 5.1, 5.2, 9.3, 9.4, 9.5_

- [x] 4. collector の情報源 (取得層)
  - 各情報源は値を取り出すだけで判定しない。宣言が無い・無効の情報源は外部に問い合わせない。失敗は例外として送り出し、同じ周期でリトライしない
  - 情報源のテストは情報源ごとのファイル (`test_festival_source_<情報源名>.py`) に置く。情報源の登録 (一覧への追加) は 7.1 で行う
- [x] 4.1 (P) Cloudflare GraphQL から来場者影響の数値とハブの接続数を取る
  - テストファイル: `test_festival_source_cloudflare.py`
  - 1 回の問い合わせで、サイトの Worker (要求数・エラー・リソース超過・CPU 時間の p99)、CMS ホスト (来場者からの直接要求と Worker からの取得要求に限った要求数・5xx・オリジン応答時間の p95)、メディア配信 (同じ許可リストでの要求数・4xx・5xx)、Worker のキャッシュ照会 (CMS ホストへの GET に限った照会数とヒット数)、ハブの直近の分ごとの接続数を取る
  - キャッシュ照会の集計は既存の問い合わせの alias として足し、問い合わせの回数を増やさない
  - 時間窓は分単位の切り捨てから 8 分前〜3 分前の 5 分幅とし、行が無い窓は 0 件として扱う。ハブの接続数の最新の分が 4 分前より古ければ取得遅延の注記を付ける
  - IP・パスの全体・User-Agent などの次元は選ばない。フィールド名は、CPU 時間の p99 が `quantiles.cpuTimeP99` (マイクロ秒、ms に換算する)、オリジン応答時間の p95 が `quantiles.originResponseDurationMsP95`、HTTP メソッドの絞り込みが `clientRequestHTTPMethodName`、キャッシュの状態の次元が `dimensions.cacheStatus`
  - 完了時: 応答の fixture から Item の値 (メディアの要求数、キャッシュ照会の照会数とヒット数を含む) を検算する単体テストと、`errors` を含む応答で例外になるテストが通る
  - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.6, 5.3, 6.11, 6.12, 9.2, 9.5_
  - _Boundary: festival.cloudflare_

- [x] 4.2 (P) Analytics Engine からスタンプの処理結果を数える
  - テストファイル: `test_festival_source_stamp.py`
  - 本番の記録に限り、4.1 と同じ時間窓で、記録失敗・レート制限・D1 失敗の件数をサンプリング間隔で重み付けして数える
  - 書き込みが無い窓は 0 件として扱う
  - 完了時: SQL 応答の fixture から 3 つの件数を検算する単体テストが通る
  - _Requirements: 4.6, 5.6, 9.2_
  - _Boundary: festival.stamp_

- [x] 4.3 (P) 地震リレーの状態を読み取り専用トークンで取る
  - テストファイル: `test_festival_source_quake.py`
  - `/status` に読み取り専用トークンを付けて取り、定期起動の最終実行時刻と期間内かどうか、上流ごとの接続・受信・最終受信時刻、到達不能の有無、Discord 通知の設定・停止・最終成否を Item にする
  - 応答に定期起動の情報が無い (改修前のリレー) 場合は、定期起動の Item を作らない
  - 完了時: 改修後と改修前の `/status` の fixture から Item を作る単体テストが通る
  - _Requirements: 5.4, 5.5, 9.3_
  - _Boundary: festival.quake_

- [x] 4.4 (P) サイトの Worker からサイネージの接続元ごとの接続数を取る
  - テストファイル: `test_festival_source_signage.py`
  - `/api/signage/stats` に Access のサービストークンのヘッダを付けて取り、接続元ごとの接続数と計数時刻を Item にする
  - Access に阻まれた応答 (ログイン画面への転送や HTML) は解釈できない応答として失敗にする
  - 完了時: stats の fixture から Item を作るテストと、HTML の応答で例外になるテストが通る
  - _Requirements: 5.3_
  - _Boundary: festival.signage_

- [x] 4.5 (P) CMS から駐車場カメラの状態と開催日程を取る
  - テストファイル: `test_festival_source_parking.py`、`test_festival_source_schedule.py`
  - 駐車場の取得が無効の宣言の間は問い合わせない。有効なら、クラスタ内の CMS Service 経由で `/api/ops/parking-health` に Bearer トークンを付けて取り、カメラごとの Item にする
  - 開催日程は、CMS の公開設定から駐車場の監視時間と開催日だけを認証なしで取り、Item にする
  - 実装の最初の検証として、collector の Pod から CMS Service に届くことを確かめる。届かなければ原因を添えて報告し、回答が出るまで該当する情報源を無効の宣言で止めて他の作業を進める
  - 完了時: parking-health と公開設定の fixture から Item を作る単体テストが通り、CMS Service への到達の実測結果が記録されている
  - _Requirements: 5.1, 5.2, 5.8, 9.3_
  - _Boundary: festival.parking, festival.schedule_

- [x] 4.6 (P) ODPT のバスの公開フィードの生成時刻を取る
  - テストファイル: `test_festival_source_bus.py`
  - 2 つのフィードを 30 秒ごとに取り、protobuf の header の生成時刻だけを標準ライブラリの varint の読み取りで取り出す
  - 片方の失敗はその Item を取得遅延にして理由を記し、両方の失敗は例外にする
  - 完了時: フィードの fixture から生成時刻を取り出すテストと、header が無いときに失敗するテストが通る
  - _Requirements: 5.7, 9.2_
  - _Boundary: festival.bus_

- [x] 4.7 (P) 全ノードの Ready と資源の使用率を取る
  - テストファイル: `test_festival_source_nodes.py`
  - Kubernetes のノード一覧を期待集合とし、ノードごとに Ready、CPU・メモリの使用率、kubelet から読むルートディスクの使用率を Item にする。1 ノードの kubelet に届かなければ、そのノードのディスクだけ取得遅延にする
  - fail2ban の照合用に、ノードのアドレスと Pod の CIDR をモジュール内の最新値として保持し、評価器に渡す手段を用意する。これらは Item に入れない
  - 実装の最初の検証として、collector の Pod から全ノードの 10250 番に届くことを確かめる。届かなければ原因を添えて報告し、回答が出るまで該当する情報源を無効の宣言で止めて他の作業を進める
  - 完了時: ノード一覧・メトリクス・kubelet の fixture から Item と内部アドレスの集合を作る単体テストが通り、10250 番への到達の実測結果が記録されている
  - _Requirements: 6.4, 7.5, 9.5_
  - _Boundary: festival.nodes_

- [x] 4.8 (P) CMS と DB の Pod の状態を取る
  - テストファイル: `test_festival_source_workloads.py`
  - CMS の Deployment の宣言数、Ready の Pod 数、`restart_window` 以内の再起動、OOM と CrashLoop の有無、CPU 使用量と limit の比の最大値を Item にする
  - CNPG の Cluster から primary を決め、DB の各 Pod の役割、CPU 使用量対 limit、CMS と同じ窓と規則での再起動・OOM・CrashLoop の有無を Item にする。Pod IP はモジュール内の最新値として DB の取得用に保持し、表示しない
  - cloudflared の Pod を宣言の namespace とラベルセレクタで取り、Pod ごとの CPU 使用量対 limit の最大値を Item にする
  - 完了時: Deployment・Pod・Cluster・メトリクスの fixture から CMS・DB・cloudflared の Item を作る単体テストが通る
  - _Requirements: 6.1, 6.2, 6.7, 6.9_
  - _Boundary: festival.workloads_

- [x] 4.9 CNPG の exporter から DB の接続数と replica の遅延を取る
  - テストファイル: `test_festival_source_cnpg.py`
  - Prometheus テキスト形式から指定した名前のサンプルを読む最小の読み取りを用意する
  - 4.8 が保持する DB の Pod の宛先から各 Pod の 9187 番を取り、Pod ごとの接続数の和・接続の上限・replica の遅延・primary の統計情報の無い表の数 (1.4 のカスタムクエリ) を Item にする。1 Pod に届かなければその Pod を取得遅延、全 Pod に届かなければ例外にする
  - 実装の最初の検証として、collector の Pod から全 DB の Pod (他のノードの Pod を含む) の 9187 番に届くことを確かめる。届かなければ原因を添えて報告し、回答が出るまでこの情報源を無効の宣言で止めて他の作業を進める
  - 完了時: Prometheus テキストの fixture から値 (Pod ごとの接続数、統計情報の無い表の数を含む) を読むテストが通り、9187 番への到達の実測結果が記録されている
  - _Requirements: 6.2, 6.3, 6.8, 6.14_

- [x] 4.10 kubelet から DB のメモリと、CMS・cloudflared の CPU スロットリングを取る
  - テストファイル: `test_festival_source_workloads.py`
  - 4.8 と同じ情報源に足す。対象の Pod が載っているノードの kubelet に、4.7 と同じ経路で問い合わせる
  - DB の各 Pod について、kubelet の `/stats/summary` のコンテナの rss を memory limit で割った比の最大値を Item に足す。ページキャッシュを含む値は使わない
  - CMS と cloudflared の Pod について、kubelet の `/metrics/cadvisor` からスロットリングされた期間と全期間のカウンタを読み、前回の取得との差分の比をコンテナごとに求め、対象ごとの最大値を Item に足す。前回の値が無い行 (初回・新しい Pod・カウンタの減少) はその周期の計算から除き、すべて除かれたら取得遅延の注記を付ける
  - 1 ノードの kubelet に届かなければ、そのノードの Pod のメモリとスロットリングだけを欠かす
  - 完了時: `/stats/summary` の fixture から rss 対 limit を、`/metrics/cadvisor` の 2 回分の fixture からスロットリング率 (初回・カウンタの減少・期間の増分 0 を含む) を検算する単体テストが通る
  - _Requirements: 6.6, 6.10_

- [x] 5. 評価・通知・生存確認
- [x] 5.1 項目の定義の枠組みと来場者影響の項目を作る
  - 区分・判定の結果・項目群の定義 (依存する情報源・参照先・通知の要否・判定) と、宣言の閾値による注意・異常の共通の判定 (向きと等号の扱い) を用意する
  - 省略できる閾値名が宣言されていない項目は、判定せず表示のみの結果を返す共通の扱いを用意する
  - Worker のエラー率・リソース超過・CPU の p99、CMS の 5xx 比率・オリジン応答時間の p95、メディアの 4xx・5xx、UptimeRobot の各モニターの項目を定義する。比率は最低要求数未満なら正常とし、IdP と CMS DB に割り当てたモニターは来場者影響の区分から除く
  - 課金・利用枠・DMARC・TLS-RPT・メール配送の項目は定義しない。参照先の Cloudflare ダッシュボードはアカウント ID を含まない URL にする
  - 完了時: 閾値の境界 (注意・異常の等号)、最低要求数、モニターの除外、閾値が未宣言の項目が表示のみになることを確かめる単体テストが通る
  - _Requirements: 1.4, 3.3, 3.7, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 6.13_

- [x] 5.2 機能別の項目を作る
  - 駐車場カメラごとの最終受信からの経過と、連続失敗回数・最終成功からの経過の悪い方による判定の項目を定義する。開催日かつ監視時間内 (終了時刻ちょうどを含む) だけ判定し、それ以外は時間外にする。開催日程が一度も取れていなければ取得失敗にする
  - サイネージの本番の接続数 (設置台数を下回ると注意、宣言の台数以上欠けると異常。設置台数 0 の間は判定しない)、地震リレーの定期起動の経過・上流ごとの状態・Discord 通知の状態 (期間外は時間外。定期起動の情報が無い応答は取得失敗)、スタンプの 3 種の件数、バスのフィードごとの生成時刻からの経過 (開催日で絞らない) の項目を定義する
  - 完了時: 時間外の境界 (開催日・監視時間の終了ちょうど・期間内フラグ)、設置台数との比較、定期起動の情報が無い応答の扱いを確かめる単体テストが通る
  - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8_

- [x] 5.3 容量と基盤・セキュリティの項目を作る
  - CMS の Ready 率・再起動 (直近の再起動は注意、OOM と CrashLoop は異常)・CPU 対 limit、DB primary の CPU 対 limit と接続率、replica ごとの遅延、ノードごとの Ready・CPU・メモリ・ディスク、Tunnel の状態と接続数の項目を定義する
  - DB の instance ごとのメモリ (rss) 対 limit と接続数、全 instance の再起動 (CMS と同じ規則)、primary の統計情報の無い表の数、CMS と cloudflared の CPU スロットリング率、cloudflared の CPU 対 limit、メディア配信を除く CMS へのリクエストのレート (窓の平均)、Cache API のヒット率 (閾値が未宣言の間は表示のみ) の項目を定義する
  - 対応中の dr-incident (異常) と infra-alert (注意) の件数と表題、窓内の Falco の優先度別の件数、IdP と CMS DB 経由の応答のモニター、Healthchecks.io の各チェック、内部アドレスに対する fail2ban の BAN の件数 (IP は照合だけに使い表示しない)、通知器と生存確認の状態 (通知しない) の項目を定義する
  - 完了時: 内部アドレスの BAN の照合、Falco の優先度の閾値、通知器・生存確認の状態の判定、リクエストのレートの計算 (メディアを除く)、ヒット率の照会 0 件の扱いを確かめる単体テストが通り、全項目のキーが一意である
  - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8, 6.9, 6.10, 6.11, 6.12, 6.13, 6.14, 7.1, 7.2, 7.3, 7.4, 7.5, 9.5_

- [x] 5.4 15 秒ごとに全項目を評価して保存する評価器を作る
  - 結果の写しと Falco の件数・内部アドレスを入力に項目を作り、依存する情報源の取得失敗・鮮度の上限超過・未取得・取得遅延の注記で判定を上書きする (表示のみの項目も同じ)。取得失敗のときも直前の値は残す
  - 無効の情報源 (無効の宣言、駐車場の取得の無効) だけに依存する項目は作らない
  - 情報源の失敗が始まった時刻を評価のたびに計算し、再起動時は通知の状態から復元する
  - 1 回の評価の例外は記録して次に進み、失敗理由を状態に残す。外部には問い合わせない
  - 完了時: 取得失敗と鮮度の超過による上書き、無効の情報源の項目を作らないこと、例外後も次の評価が続くことを単体テストで確かめている
  - _Requirements: 2.2, 2.3, 2.4, 6.13, 8.4, 9.7_

- [x] 5.5 通知の差分を計画する
  - 評価結果と通知の状態から、異常に入った・再通知の間隔を超えた・正常または時間外に戻った・異常中に消えた項目と、猶予を超えた情報源の失敗・回復を、通知として計画する。副作用を持たない
  - 注意・取得遅延・取得失敗は異常の状態を変えず、異常から注意への下降は回復とみなさない。失敗中の情報源に依存する項目は項目として通知しない
  - 通知の対象外 (通知器・生存確認) の項目と、表示のみの項目は計画しない
  - 完了時: 異常・再通知・回復・対象外・情報源のまとめ・注意への下降で回復しないこと、表示のみの項目を計画しないことを単体テストで確かめている
  - _Requirements: 6.13, 8.1, 8.2, 8.3, 8.4_

- [x] 5.6 通知を当日専用の Discord webhook に送る
  - 1 回の評価で計画した通知を 1 件 (2000 文字以内) にまとめて送り、超える分は次の評価に回す。項目名・現在値・閾値・画面への参照を載せ、メンションは起こさない
  - 送信に成功した通知についてだけ通知の状態を更新する
  - 429 は `Retry-After` まで、残数 0 はリセットまで、通信断と 5xx は 60 秒送らない。401・403・404 は送信を止め、理由を状態に残す。webhook が未設定なら送らない
  - 送信先は当日専用の webhook だけとし、既存の送信先は使わない。失敗の理由は状態コードか network・timeout だけにし、URL をログ・画面に出さない
  - 完了時: 送信失敗で通知の状態が変わらないこと、レート制限の待機、停止の扱い、エラー文に URL が残らないことを単体テストで確かめている
  - _Requirements: 8.1, 8.2, 8.5, 8.6, 9.2, 9.4_

- [x] 5.7 (P) Healthchecks.io に当日監視の生存を知らせる
  - 評価が例外なく終わったときだけ、前回から 60 秒以上経っていれば ping を送る。ping URL が未設定なら送らない
  - 失敗しても同じ評価で再送せず、評価と通知を止めない。成否を状態に残し、失敗の理由に URL を出さない
  - 完了時: 評価が成功したときだけ 60 秒に 1 回まで ping すること、評価の例外では ping しないこと、失敗の理由に URL が残らないことを単体テストで確かめている
  - _Requirements: 8.7, 9.4_
  - _Boundary: FestivalHeartbeat_

- [x] 6. 当日監視画面
- [x] 6.1 (P) 当日監視画面のページと断片をサーバー側で描画する
  - 保存された評価結果と状態だけを読み、上部の帯 (異常・注意の件数、異常の項目名、最終更新時刻、判定の停止)、4 区分のタブ見出し (最悪の判定と異常の件数)、タブパネルのタイル (名前・判定・現在値・取得時刻) を描画する
  - 閾値・参照先・データの時刻・注記は、選択時に詳細パネルに出す項目ごとの雛形に入れる
  - 判定は既存の記号と文字を併記し、画面表記は design の「画面表記」の表どおりに一箇所で定義する。表示のみの項目は「表示のみ」とし、正常の色を使わず、帯とタブ見出しの件数・最悪の判定に数えない
  - 宣言が無いときは「当日監視の宣言がありません」を表示する
  - 完了時: 一時 SQLite の評価結果から描画した断片で、帯の件数と異常の項目名、タブ見出しの最悪の判定・異常件数、詳細の雛形の内容が一致することを単体テストで確かめている
  - _Requirements: 1.1, 1.4, 2.2, 3.1, 3.2, 3.5, 3.6, 3.7, 3.9, 6.13_
  - _Depends: 2.3_
  - _Boundary: FestivalPage_

- [x] 6.2 ブラウザ側の自動更新と選択状態の保持を作る
  - 15 秒ごとに断片を取り (同一オリジンの認証情報、キャッシュなし、転送を追わない、10 秒で打ち切る)、成功したら差し替えて、選択中のタブ・項目・詳細パネルを戻す。選択中の項目が消えていれば「この項目は現在ありません」を出す
  - 401 は「表示が古い・再ログインが必要」とページへのリンク、それ以外の失敗は「表示が古い (最終更新)」を、表示を保ったまま帯に出し、最終成功から 45 秒を超えたら強調する
  - タブの選択を URL のハッシュに反映し、タブとタイルをキーボードで操作できるようにする
  - 完了時: ページを開いたまま操作なしで断片が差し替わり、更新をまたいで選択中のタブと詳細が保たれる
  - _Requirements: 2.1, 2.5, 3.10_

- [x] 6.3 1920×1080 でスクロールなしに収まるレイアウトにする
  - 帯・タブ見出し・タイルの格子・詳細パネルを design の寸法の予算で配置する
  - 1 タブの項目数が通常の表示で収まる数を超えたら、タイルを小型表示に切り替える
  - 完了時: 1920×1080 で、1 タブ 40 個と小型表示の閾値を超える項目数の両方で、本文に縦スクロールが出ない
  - _Requirements: 3.8_

- [ ] 7. 統合と検証
- [x] 7.1 当日監視を collector に組み込み、配布の設定を足す
  - 10 の情報源を情報源の一覧に登録し、`/admin/festival` と断片の経路を `/admin/` の配下に足し、起動時に評価器 (通知器と生存確認を含む) のスレッドを起動する
  - 当日監視のパッケージを新しい ConfigMap で配り、initContainer でのコピーと volume を足し、kustomization に新しいファイルを列挙する。宣言の変更で ConfigMap のハッシュが変わり Pod が作り直されることを確かめる
  - portal の nginx は変えず、既存の admin の認可が新しい経路にもかかることを確かめる
  - 駐車場の取得は、CMS の改修の本番反映の有無に合わせて有効・無効を宣言する
  - 完了時: ローカルで collector が起動し、`/admin/festival` と断片が 200 を返し、`kustomize build` が通る。portal の nginx の設定に差分がない
  - _Depends: 2.4, 4.10, 5.6, 5.7, 6.3_
  - _Requirements: 1.1, 1.2, 1.3, 3.4, 9.7_

- [x] 7.2 評価から描画・通知までを結合テストで確かめる
  - 偽の結果を入れた Collector と一時 SQLite で、評価・保存・断片の描画までを通す
  - `/admin/` の要確認に `festival.*` の異常が出ないことを確かめる
  - ローカルの偽の Discord と偽の Healthchecks.io で、429 から待機して再送すること、複数の通知を 1 件にまとめること、2000 文字を超える分を次に回すこと、ping の間隔を確かめる。本番の webhook と ping URL は使わない
  - 完了時: 上記の結合テストがすべて通る
  - _Requirements: 1.5, 2.3, 2.4, 8.1, 8.2, 8.3, 8.4, 8.5, 8.7, 9.7_

- [x] 7.3 当日監視画面を実ブラウザで確かめる
  - Playwright (headless) で、ローカルの collector と fixture の SQLite を使う
  - 1920×1080 で 4 つのタブそれぞれの本文に縦スクロールが無いこと (1 タブ 40 個と小型表示の閾値を超える数)、タブと項目を選んだまま 2 回の自動更新を待って保たれること、断片の応答を 401・500・通信断に差し替えたときの帯の警告と表示の保持、タブ見出しと帯の件数・異常の項目名の一致を確かめる
  - 完了時: 上記の E2E テストがすべて通る
  - _Requirements: 2.1, 2.5, 3.5, 3.8, 3.9, 3.10_

- [ ] 7.4 運用の記録を当日監視に合わせる
  - 運用ダッシュボードの runbook に当日監視画面の節 (宣言の変更、無効の宣言、駐車場の有効化、Healthchecks.io の通知先の手動設定、戻し方) を足す
  - steering の監視スタック節の運用ダッシュボードの行を「通知もする」に更新し、シークレット一覧と手動発行の資格情報の表に新しいキーとサービストークンを足す
  - 本番反映後に collector のメモリを実測し、追加分の見積りとあわせて runbook に記録する。limit の 75% を超えた場合は、増加の要因を調べて limit の見直しを諮る
  - 完了時: runbook と steering に当日監視の記述があり、collector のメモリの実測値が記録されている
  - _Requirements: 9.1, 9.6_

- [x] 8. 当日監視画面の推移・内訳・タイルの格子
  - 各小タスクは、完了時の条件を確かめるテストを先に書き、失敗することを確かめてから実装する
  - 6.1 の詳細の雛形 (`<template data-detail-for>`)、6.2 の雛形の複製による詳細の表示、6.3 の寸法の予算と小型表示の切り替え、7.3 の E2E は、本タスクの 8.7〜8.9・8.11 が置き換える。既存のテストのうち置き換わる挙動を確かめているものは、新しい挙動のテストに書き換える
- [x] 8.1 (P) 1 分値の表と、項目のグラフ・内訳の列を Store に足す
  - `model.py` に `MetricUnit` (`ratio`・`count`・`ms`・`seconds`・`rps`) と `Breakdown` (見出し・列・整形済みの行) を足し、`FestivalItem` に `metric`・`unit`・`warn`・`crit`・`breakdown`・`link_label` を足す
  - `festival_items` に `metric`・`unit`・`warn`・`crit`・`breakdown` (`Breakdown` の JSON)・`link_label` の列を足し、`festival_history` (主キー `(key, minute)`、`WITHOUT ROWID`) と索引 `festival_history_minute` を、既存の表と同じく起動時に作る
  - `replace_festival_items` に 1 分値 (`history`・`history_minute`) を渡せるようにし、同じトランザクションで `INSERT OR REPLACE` と、`minute` が `history_minute − 24 時間` より前の行の削除を行う。既存の `RETENTION` には加えない
  - `load_festival_history(since, until, key=None)` で、範囲内の 1 分値を項目キーごとに時刻順で返す (key を渡すとその項目だけ)
  - テストファイル: `test_festival_store.py`
  - 完了時: 一時 SQLite に対し、主キーが同じ行の上書き、期間の読み取り (全項目と 1 項目)、24 時間より前の行の削除、`festival_items` の内訳とグラフの列の往復を確かめる単体テストが通る
  - _Requirements: 3.11, 3.13, 3.14, 9.1_
  - _Boundary: Store_

- [x] 8.2 (P) Cloudflare と Pod の取得結果に内訳を残す
  - `festival.cloudflare`: 合計に畳む前の行から、`worker.by_status` (`{status, requests, errors}`)、`cms.by_status`・`media.by_status` (`edgeResponseStatus` ごとの `{status, count}`。同じステータスは足し合わせる)、`cms_cache.by_cache_status` (`{cache_status, count}`) を `Item.values` に足す。問い合わせの回数と選ぶ次元は変えない
  - `festival.workloads`: 最大値を求める前の Pod ごとの値から、`cms.by_pod` (`{pod, ready, cpu_ratio, throttle_ratio, restarts_recent, oom_recent, crashloop}`) と `cloudflared.by_pod` (`{pod, cpu_ratio, throttle_ratio}`) を足す。DB には足さない
  - 配列の各要素は数値と文字列だけの辞書にし、IP などの個人を識別できる値を入れない
  - テストファイル: `test_festival_source_cloudflare.py`、`test_festival_source_workloads.py`
  - 完了時: fixture の行と `by_status`・`by_cache_status`・`by_pod` の内訳が一致することを単体テストで確かめている
  - _Requirements: 3.14, 9.2, 9.5_
  - _Boundary: festival.cloudflare, festival.workloads_

- [x] 8.3 項目にグラフの値・閾値の線・参照先の名前を持たせる
  - `Judgement` に `metric`・`unit`・`warn`・`crit`・`breakdown` を、`ItemGroup` に `link_label` を足す
  - design の「グラフの対象」の表の全項目で、`metric`・`unit`・閾値の線を入れる。期間の閾値は秒に換算し、算出できない値 (最終受信の時刻が無い、lookups が 0 など) は `metric = None` にする。表示のみの項目と、閾値が未宣言の Cache API のヒット率は線を `None` にする。サイネージの線は設置台数 − `signage_missing` の注意・異常とし、設置台数 0 の間は線なし
  - 駐車場の受信の経過 (秒) と判定の連続失敗回数をグラフの対象にする
  - 表示のみの項目 `visitor.worker.requests` (「Worker 要求数」、「{requests} 件」) を足す。閾値名を持たず、常に `judged = False` を返す
  - 参照先の名前は、Cloudflare ダッシュボード・Netdata・`/admin/#…`・CMS 管理画面・Healthchecks.io のコンソールに、それぞれ `fest.link.cloudflare`・`fest.link.netdata`・`fest.link.admin`・`fest.link.cms`・`fest.link.healthchecks` を割り当てる
  - テストファイル: `test_festival_catalog.py`
  - 完了時: グラフの対象の全項目で値・単位・線が入り、表に無い項目で `unit` が `None` であること、期間の閾値の秒への換算、サイネージの線、表示のみの項目で線が無いこと、`visitor.worker.requests` が通知を計画せず帯とタブの件数に数えられないこと、受信なしの駐車場で `metric = None` になることを単体テストで確かめている
  - _Requirements: 3.5, 3.7, 3.11, 3.12, 4.1, 6.13_
  - _Depends: 8.1_
  - _Boundary: FestivalCatalog_

- [x] 8.4 項目に内訳の表を持たせる
  - design の「内訳」の表の全項目で、見出し・列・並び順のとおりに `Breakdown` を作る。値は情報源が既に取得した結果だけから作り、問い合わせを足さない
  - DB の instance ごとの内訳は `festival.workloads` と `festival.cnpg` の `db.*` を Pod 名で突き合わせ、片方が無い・取得失敗のときはその列を「—」にする。primary を先頭、残りは Pod 名順にする
  - 値の書式は、比率は小数 1 桁の百分率、時刻は JST の `HH:MM:SS`、真偽は「はい」「いいえ」、値が無いときは「—」、再起動は `capacity.cms.restarts` の現在値と同じ書式にする
  - 表に無い項目 (`capacity.tunnel` を含む) は内訳を持たない。列に IP などの個人を識別できる値を入れない
  - テストファイル: `test_festival_catalog.py`
  - 完了時: 内訳の表の全項目で見出し・列・並び順が表のとおりであること、DB の突き合わせと片方が無いときの「—」、内訳の無い項目で `breakdown` が `None` であることを単体テストで確かめている
  - _Requirements: 3.14, 9.5_
  - _Depends: 8.2, 8.3_
  - _Boundary: FestivalCatalog_

- [x] 8.5 (P) 評価器で 1 分値を記録し、内訳を保存する
  - `Judgement` の `metric`・`unit`・`warn`・`crit`・`breakdown` と `ItemGroup.link_label` を `FestivalItem` に移す。取得失敗・取得遅延の項目も、直前の結果から作った内訳を残す
  - 評価時刻を分単位で切り捨てた分が、最後に記録した分 (評価器のメモリにだけ持つ) から変わった最初の評価でだけ 1 分値を記録する
  - 記録するのは `unit` と `metric` が `None` でなく、取得失敗・取得遅延・時間外でない項目とする。表示のみの項目も記録し、判定の結果は記録しない。記録しない分は補わない
  - 記録と 24 時間より前の行の削除は、`festival_items` の置き換えと同じトランザクションで行い、記録の失敗は既存の評価の例外の扱いに従う
  - テストファイル: `test_festival_evaluate.py`
  - 完了時: 分が変わって最初の評価だけが記録すること、取得失敗・取得遅延・時間外・`metric = None`・グラフの対象外の項目を記録しないこと、表示のみの項目を記録すること、24 時間より前の行が消えること、同じ分の再記録で上書きされることを単体テストで確かめている
  - _Requirements: 3.11, 3.13, 3.14, 9.1_
  - _Depends: 8.1, 8.3_
  - _Boundary: FestivalEvaluator_

- [x] 8.6 (P) スパークラインと当日の折れ線を SVG にする
  - `festival/chart.py` に `sparkline`・`day_chart`・`format_axis` を作る。入力の値だけから描き、SQLite と時計を読まない。import は `model`・`festival.labels`・`render.components` の `esc`・`render.svg` の `_nice_max` に限る
  - スパークライン: design の「スパークライン」のとおり、直近 60 個の分、`viewBox="0 0 600 100"`、縦軸 0〜`_nice_max(最大値)` (すべて 0・記録なしは 0〜1)、軸・目盛・文字なし、`aria-hidden="true"`。記録が 1 つも無ければ空文字
  - 当日の折れ線: design の「当日の折れ線」のとおり、JST の 00:00 から評価時刻まで、`viewBox="0 0 528 220"`、縦軸の上限に閾値を含め、3 本の目盛と単位ごとの文字、正時の横軸の目盛 (8 時間以下は 1 時間、16 時間以下は 2 時間、それ以上は 3 時間)、注意・異常の破線と右の文字 (縦の間隔が 14px 未満なら上側を 14px 上へ)、`role="img"` と `aria-label`。記録が 1 つも無ければ「当日の記録がありません」
  - 両方とも欠測の分で線を切って前後を結ばず、1 点だけの分は長さ 0 の線分を丸い端で描く
  - 当日の推移と単位の表記 (`fest.detail.chart*`) を `festival/labels.py` に足す
  - テストファイル: `test_festival_chart.py`
  - 完了時: スパークラインの横軸 (60 個の分)・縦軸の上限 (0 だけ・記録なしで 0〜1)・欠測での切断・1 点だけの分、当日の折れ線の範囲・閾値を含む上限・閾値の線と文字・文字のずらし・目盛の間隔 (8・16 時間の境界)・記録なしの表示・単位ごとの書式を単体テストで確かめている
  - _Requirements: 3.11, 3.12, 3.13_
  - _Depends: 8.1_
  - _Boundary: FestivalChart_

- [x] 8.7 タイルの格子とタイルの中身を描く
  - `tile_grid(n)` を design の「タイルの大きさの決め方」の手順と定数どおりに作り、タブパネルに `data-size` と `--cols`・`--tile-w`・`--tile-h` を出す
  - タイルに、名前・判定・現在値・閾値・注記・時刻 (「データ HH:MM:SS・取得 HH:MM:SS」、データの時刻が無い項目は取得だけ) と、グラフの対象の項目は直近 60 分のスパークラインを常に載せる。スパークラインは `festival_history` の直近 60 分を全項目まとめて 1 回で読む
  - 標準表示と小型表示の各行の高さ・文字の大きさ・余白を design の「タイルの中身」の表どおりにし、溢れた文字は末尾を省略する
  - 帯 120px・タブ見出し 56px・格子の領域 1360 × 904・詳細パネル 560 × 904 の寸法の予算で配置する (6.3 の寸法と小型表示の切り替えを置き換える)
  - 時刻の表記 (`fest.tile.*`) を `festival/labels.py` に足す
  - テストファイル: `test_festival_page.py`
  - 完了時: `tile_grid()` が design の例の表 (1・4・6・8・12・16・20・24・30・37・42 個) と一致し 0 個で `None` になること、標準と小型の境界、一時 SQLite から描いたタイルに閾値・注記・時刻・スパークラインが入り、グラフの対象でない項目にスパークラインが無いことを単体テストで確かめている
  - _Requirements: 3.6, 3.8, 3.11, 3.15_
  - _Depends: 8.1, 8.6_
  - _Boundary: FestivalPage_

- [x] 8.8 選んだ項目の詳細を断片に含めて返す
  - `/admin/festival/fragment` の任意のクエリ `key` を受け、ルート要素に `data-key` を出し、断片の中に詳細パネル `<aside id="detail" aria-live="polite">` を置く。`key` の項目の分だけ詳細を描き、`key` が無ければ未選択の表示にする。`key` は SQL のパラメータと HTML のエスケープを通してだけ使い、200 文字を超える値と該当する項目の無い値は「この項目は現在ありません」にする
  - 詳細パネルに、見出し・現在値・項目の情報 (閾値・データの時刻・取得時刻・注記。注記は 2 行まで)・当日の推移 (グラフの対象の項目だけ。`festival_history` の当該項目の JST の 00:00 から評価時刻までを間引かずに読む)・内訳 (11 行を超えるときは先頭 10 行と「ほか n 行」)・補助のリンク (「{name} を開く」の文字のリンク、外部は新しいタブ) を、design の「詳細パネル」の高さの予算と間隔で出す
  - 6.1 の詳細の雛形 (`<template data-detail-for>`) を除く
  - 詳細・参照先・内訳の見出しと列・真偽の表記 (`fest.detail.*`・`fest.link.*`・`fest.bd.*`・`fest.col.*`・`fest.bool.*`) を design の「画面表記」どおりに `festival/labels.py` に足し、`fest.detail.link` を「{name} を開く」に改める
  - テストファイル: `test_festival_page.py`
  - 完了時: `key` 付きの断片に当該項目の当日の折れ線・内訳・リンクが入り、`key` 無しで未選択、200 文字超・該当無しで「この項目は現在ありません」、内訳 11 行超で「ほか n 行」、`key` の値がエスケープされることを単体テストで確かめている
  - _Requirements: 3.7, 3.12, 3.13, 3.14, 3.16_
  - _Depends: 8.7_
  - _Boundary: FestivalPage_

- [x] 8.9 ブラウザ側で項目の選択・自動選択・取り直しを行う
  - `selectItem` で、タイルに選択の印を付け、詳細パネルを「読み込み中」にして、選択中の項目のキーを付けてすぐに断片を取り直す。15 秒ごとの更新も選択中のキーを付ける
  - 断片の要求に連番 (`requestSeq`) を付け、最後に出した要求でない応答は適用しない
  - 利用者の選択が無い間は、表示中のタブで最も悪い判定の項目 (`model.worst` と同じ順序、同じ判定はタイルの並びで先、表示のみの項目はタブに判定対象が無いときだけ候補) を自動で選び、その `key` で詳細を取り直す。更新のたびとタブの切り替えのたびに選び直す
  - 利用者の選択は自動では変えず、タブを切り替えても保つ。選択中のタイルの再選択または Esc で自動選択に戻る。選んだ項目が消えたら「この項目は現在ありません」を出し、次の更新で自動選択に戻る。タブに項目が無いときだけ「項目を選ぶと推移と内訳を表示します」を出す
  - 失敗時は詳細パネルを含めて表示を保つ (6.2 の雛形の複製による詳細の表示を置き換える)
  - 完了時: 8.11 の E2E のうち、選択の保持と更新、選択の切り替えの競合、自動選択の項目を先に書き、それらが通る
  - _Requirements: 3.7, 3.10, 3.16_
  - _Depends: 8.8_
  - _Boundary: FestivalPage_

- [x] 8.10 配布の設定と結合テストを推移・内訳に合わせる
  - kustomization の configMap `collector-festival` に `festival/chart.py` を足す
  - 偽の結果を入れた Collector と一時 SQLite で、時計を進めて複数の分を評価し、断片のスパークラインと、`key` を付けた断片の当日の折れ線・内訳に反映されることを結合テストに足す
  - テストファイル: `test_festival_integration.py`
  - 完了時: 上記の結合テストが通り、`kustomize build` が通り、ローカルの collector で `key` を付けた断片が 200 を返す
  - _Requirements: 3.11, 3.12, 3.14, 9.1_
  - _Depends: 8.4, 8.5, 8.8_

- [x] 8.11 当日監視画面を実ブラウザで確かめる (7.3 を置き換える)
  - Playwright (headless) で、ローカルの collector と fixture の SQLite を使い、1920×1080 で確かめる。検証後と失敗時にブラウザを閉じる
  - テストは `collector/app/tests/e2e_festival.py` に置き、`python3 -I` で手動で実行する (ファイル名を `test_` で始めず、CI の `unittest discover` の対象にしない)
  - 4 つのタブのそれぞれで、格子と詳細パネルに縦スクロールが無いこと (`scrollHeight <= clientHeight`)。項目数は 1・12・20 (標準表示)・24・42 (小型表示)
  - 各項目数で、タイルの寸法が `tile_grid()` の値と一致し、タイルの各行 (名前・現在値・スパークライン・閾値・注記・時刻) が予算の高さに収まること (`getBoundingClientRect()` で実測する)
  - 内訳 11 行以上とグラフと注記 2 行を持つ項目を選び、詳細パネルの各部分が予算の高さに収まり、パネルに縦スクロールが無いこと
  - 項目を選ぶとページを遷移せずに当日の折れ線 (閾値の線と文字つき) と内訳が出ること、欠測のある fixture で線が途切れること
  - タブを切り替え、項目を選んだ状態で 2 回の自動更新を待ち、タブと詳細パネルが保たれ、詳細の値が更新後の値になること
  - 項目 A を選んだ直後に項目 B を選び、A の応答を遅らせても、詳細パネルに A が出ないこと
  - 開いた直後とタブの切り替え直後に、選ばなくてもそのタブで最も悪い判定の項目の詳細が出ること。最も悪い項目が変わる fixture に差し替えると次の更新で詳細が切り替わること。利用者が選んだ項目は、より悪い項目が現れても変わらず、選択を外すと自動選択に戻ること
  - 断片の応答を 401・500・通信断に差し替えたときの帯の警告と表示の保持、タブ見出しの最悪の判定・異常件数と帯の件数・異常の項目名の一致
  - 完了時: 上記の E2E テストがすべて通る
  - _Requirements: 2.1, 2.5, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.11, 3.12, 3.13, 3.14, 3.15, 3.16_
  - _Depends: 8.9, 8.10_

- [x] 8.12 (P) 運用の記録を推移の保持に合わせる
  - 運用ダッシュボードの runbook の当日監視画面の節に、タイルの推移と画面内の詳細、推移の値を collector の SQLite に 1 分ごとに 24 時間分だけ持つこと、`festival_history` は復元の対象外で失うとその時点より前の推移が消えることを足す
  - runbook の collector のメモリの節に、推移と内訳の追加分の見積り (試算 +6〜10Mi) と合計の見込み (試算 約 79Mi、limit の約 49%) を、試算であることを明記して足す。本番反映後の実測は 7.4 で行い、この合計の見込みと比べる
  - steering の監視スタック節の運用ダッシュボードの行に、推移の表示のために既存の SQLite に 1 分値を 24 時間分持つ (時系列の保存基盤は置かない) ことを足す
  - steering の dr.md の「運用ダッシュボードのデータ (collector の SQLite)」に、当日監視の 4 表 (評価結果・通知の状態・状態・1 分値) も復元の対象外で、失うと通知の状態と推移が初期化されることを足す
  - 完了時: runbook と steering に推移の保持とメモリの見積りの記述がある
  - _Requirements: 9.1, 9.6_

- [x] 8.13 サイネージの接続数を判定せず表示のみにする
  - 設置台数 (`installed`) と `signage_missing` の閾値を設定から取り除き、`function.signage.prod` は閾値名を持たない表示のみの項目にする。`judge` が `judged = False` の `Judgement` を直接返し、現在値は「{prod} 台」とする。グラフは接続数の値だけで、閾値の線 (`warn`・`crit`) は `None` にする
  - 取り除く箇所: `collector/app/config.py` の `SignageTarget.installed`・`_table` の許可キー `("url", "installed")`・`SignageTarget` の生成・閾値の宣言を検証する表の `signage_missing`、`collector/dashboard.toml` の `[festival.signage]` の `installed` とそのコメント・`[festival.thresholds]` の `signage_missing`、`collector/app/festival/catalog.py` の `_signage` の設置台数との比較・`installed <= 0` の分岐・`signage_missing` の判定・線の算出、`collector/app/tests/test_festival_catalog.py` の `SignageTest` (`test_against_installed`・`test_zero_installed_is_display_only`)・`test_signage_line_is_installed_minus_missing`・`test_signage_without_installed_has_no_line`・グラフの対象の表の `function.signage.prod` の注記、`collector/app/tests/test_festival_config.py` の `f.signage.installed` の検証 (33 行付近) と不正値の検証の `installed` の 2 件 (160・161 行付近)
  - テストファイル: `test_festival_catalog.py`、`test_festival_config.py`
  - 完了時: `function.signage.prod` が接続数にかかわらず `judged = False` で線が `None` であり、通知を計画せず帯とタブの件数に数えられないこと、`[festival.signage]` に `installed`・`[festival.thresholds]` に `signage_missing` を宣言すると `ConfigError` になること、内訳が従来どおり出ることを単体テストで確かめ、collector の単体テストが通る
  - _Requirements: 5.3, 6.13_
  - _Depends: 8.3_
  - _Boundary: FestivalCatalog_

- [ ] 8.14 閾値を根拠のある値に改め、根拠の無い項目を表示のみにする
  - `collector/dashboard.toml` の `[festival.thresholds]` を design の「閾値」の表どおりに改める。表にない閾値名 (`cms_cache_hit_ratio`・`stamp_failed`・`stamp_d1_failed`・`stamp_rate_limited`・`db_replica_lag_s`・`db_backends`・`node_memory_ratio`・`db_unanalyzed_tables`・`parking_judge_failures`・`parking_judge_success_age`) は宣言しない。異常を宣言しない閾値 (`worker_error_rate`・`worker_exceeded`・`worker_cpu_p99_ms`・`media_5xx`・`db_memory_rss_ratio`) は `crit` を書かない。`min_requests` は 50 のまま
  - `collector/app/config.py`: `OPTIONAL_THRESHOLDS` に上の 10 個の閾値名と `cms_rps` を入れる。`dashboard.toml` は `cms_rps` を宣言しない (値は 8.15 で足す)。`Threshold.crit` を `float | timedelta | None` にし、`_threshold` は `crit` を省略できる表として読む (`_table` の必須キーを `warn` だけにして `crit` を任意にする)。`crit` を省略したときは注意と異常の大小の検証をしない
  - `collector/app/festival/catalog.py`: `grade` は `th.crit` が `None` のとき異常を返さない。`threshold_text` は `th.crit` が `None` のとき「注意 ≥ x」だけを返す。`with_chart` は `crit` が `None` なら線を `None` にする。駐車場の `_judge` (435 行付近。`thresholds["parking_judge_failures"]`・`["parking_judge_success_age"]` を直接引く) は、宣言した閾値だけで判定し、どちらも宣言されていないときは `judged = False` の `Judgement` を返す。`_restart_status` は `crashloop` が真のときだけ `Status.CRIT` を返し、それ以外 (再起動・OOM) は `Status.OK` を返す (`_cms_restarts`・`_db_restarts` の現在値の文字 `_restart_text` は変えない)。`judged_value` と閾値の判定で `crit` が `None` の閾値を使う項目は `alert_on_warn = True` にする。`Judgement` と `FestivalItem` に `alert_on_warn: bool` を足し、`collector/app/store.py` の `festival_items` に列 `alert_on_warn` を足して往復させる
  - `collector/app/festival/notify.py` の `plan`: `alert_on_warn` の項目は `Status.WARN` で異常と同じ通知 (開く・再通知・回復) を計画する。`alert_on_warn` でない項目は従来どおり `Status.CRIT` で計画し、注意では回復にしない。`Notice` に `warn: bool` を足し、注意の通知は `_line` で「🟡 注意: <項目名> — 現在 <値> (<閾値の文字>)」(再通知は「🟡 注意 (継続): …」)、`_header` は「異常 n 件・注意 n 件・回復 n 件」(0 件の区分は出さない。注意は異常と別に数える) にする
  - 表示のみの項目は `judged = False`・通知しない・帯とタブの件数に数えない (既存の `judged = False` の扱いに乗る)。`restart_window` のコメント・設定はそのまま使う
  - 直すテスト: `collector/app/tests/test_festival_config.py` (`cms_cache_hit_ratio` を使う宣言の検証と、`cms_rps`・`tunnel_connections`・`bus_feed_age` の逆転・等号の検証を、宣言する閾値名に合わせて直す。`crit` 省略の読み込み、`crit` 省略で注意と異常の検証をしないこと、`warn` 省略が `ConfigError` になることを足す)、`collector/app/tests/test_festival_catalog.py` (`judged_value` の `cms_cache_hit_ratio` を使うテスト、`capacity.cms.restarts`・`capacity.db.restarts` の判定のテスト (406〜410 行付近)、`capacity.db.unanalyzed` の判定 (428 行付近)、グラフの対象の表 (618〜639 行付近) の閾値の線、`crit` 省略の `grade`・`threshold_text`・線のテストを足す)、`collector/app/tests/test_festival_notify.py` (`alert_on_warn` の項目が注意で通知・注意のまま再通知・正常で回復、取得遅延で凍結、`alert_on_warn` でない項目は注意で通知しないこと、注意の行・見出しの書式を足す)、`collector/app/tests/test_festival_store.py` (`alert_on_warn` の往復)、`collector/app/tests/test_festival_evaluate.py`・`test_festival_page.py`・`test_festival_integration.py` のうち、削除した閾値名や再起動の判定に依存するもの
  - テストファイル: `test_festival_config.py`、`test_festival_catalog.py`、`test_festival_evaluate.py`、`test_festival_notify.py`、`test_festival_store.py`、`test_festival_page.py`、`test_festival_integration.py`
  - 完了時: `dashboard.toml` の閾値が design の表と一致し (宣言する閾値名が表のとおり)、`crit` を省略した閾値の項目が注意までで異常にならず閾値の文字と線に異常が出ないこと、`crit` を省略した閾値の項目が注意で通知され (「🟡 注意: …」、見出しに「注意 n 件」) 注意から回復すると回復が通知されること、`capacity.cms.restarts`・`capacity.db.restarts` が CrashLoop のときだけ異常で再起動・OOM のみでは正常であること、表示のみにする項目が `judged = False` で通知を計画せず帯とタブの件数に数えられないこと、`cms_cache_hit_ratio` などを宣言すると通常の判定に戻ることを単体テストで確かめ、collector の単体テストが通る
  - _Requirements: 3.3, 3.4, 6.7, 6.13, 8.1, 8.2_
  - _Depends: 8.13_
  - _Boundary: FestivalConfig、FestivalCatalog、FestivalNotifier、Store_

- [ ] 8.15 非公開の閾値を環境変数で渡す
  - `collector/app/config.py`: `parse_festival` に `private_thresholds: str | None` を足し、`parse`・`load` が環境変数 `OPS_FESTIVAL_PRIVATE_THRESHOLDS` の値を渡す (`main.run` は `config_mod.load` に `env` を渡す)。値は JSON のオブジェクト (キーは閾値名、値は `{"warn": …, "crit": …}`。`crit` 省略可。期間は toml と同じ書式の文字列) で、`[festival.thresholds]` に足し合わせる。同じ閾値名が両方にあれば `ConfigError`。向き・逆転・未知の閾値名は toml の閾値と同じ検証を通す。未設定・空文字なら足さない。JSON が壊れている・オブジェクトでないときは `ConfigError` にし、エラー文に値を含めない
  - `collector/external-secret.yaml`: 当日監視の並びに `OPS_FESTIVAL_PRIVATE_THRESHOLDS` を既存の `OPS_*` と同じ書き方で足す。`collector/deployment.yaml` は `envFrom` の `collector-secrets` で全キーが環境変数になるため変更しない
  - Infisical prod への値の登録: 実装者が infisical CLI (`infisical secrets set --env=prod`) で `OPS_FESTIVAL_PRIVATE_THRESHOLDS` を登録する。値 (JSON) は手元の負荷試験の分析結果から渡し、このタスクにもリポジトリにも書かない。登録のコマンドは出力を `/dev/null` に捨て、値を端末に出さない。登録の確認はキー名だけで行う (値の出力を伴う `get` は使わない)。登録後に ExternalSecret の反映と collector の再起動を待ち、画面の `capacity.cms.rps` が判定の対象になっていること (「表示のみ」でないこと) を確かめる
  - `.kiro/steering/tech.md` の「Infisical で管理するシークレット一覧」の運用ダッシュボードの行に `OPS_FESTIVAL_PRIVATE_THRESHOLDS` のキー名を足す (値は書かない)
  - テスト (`test_festival_config.py`): toml の閾値に環境変数の閾値が足し合わさること、同じ閾値名が両方にあると `ConfigError`、壊れた JSON・オブジェクトでない JSON で `ConfigError` かつエラー文に値が出ないこと、未設定・空で足さず `cms_rps` が表示のみになること、未知の閾値名・注意と異常の逆転が `ConfigError` になること。テストの値は実測値を含まないダミーにする
  - テストファイル: `test_festival_config.py`
  - 完了時: 上記のテストを含む collector の単体テストが通り、`kustomize build` が通り、本番で `OPS_FESTIVAL_PRIVATE_THRESHOLDS` の登録後に `cms_rps` が判定される
  - _Requirements: 3.3, 3.4, 9.4_
  - _Depends: 8.14_
  - _Boundary: FestivalConfig_
