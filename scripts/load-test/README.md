# load-test

CMS (Payload) API のブレークポイント探索用 k6 シナリオ。クラスタに常駐せず、Docker 経由で外部から実行する。

## 実行方法

```bash
docker run --rm -i \
  -e BASE_URL=https://cms.aramakisai.com \
  -e TARGET_PATH=/api/globals/festival_meta?depth=1 \
  -e START_RATE=1 \
  -e MAX_RATE=50 \
  -e RAMP_DURATION=10m \
  grafana/k6 run - < breakpoint.js
```

試走（短時間・低レート、シナリオ動作確認のみ）:

```bash
docker run --rm -i \
  -e BASE_URL=https://example.com \
  -e TARGET_PATH=/ \
  -e START_RATE=1 \
  -e MAX_RATE=2 \
  -e RAMP_DURATION=5s \
  grafana/k6 run - < breakpoint.js
```

## 対象エンドポイントの切替

- `BASE_URL`: 対象のベース URL。Cloudflare 経由 (`https://cms.aramakisai.com`) と、Cloudflare を経由しないオリジン直の URL の双方を、同一シナリオのまま切り替えて指定する
- `TARGET_PATH`: `BASE_URL` に続く CMS API のパス（既定値 `/api/globals/festival_meta?depth=1`。フロントエンドが全ページで呼ぶ global で、DB へ到達する）

## 実行パラメータ

| 変数 | 既定値 | 内容 |
|------|--------|------|
| `START_RATE` | `1` | 開始レート (req/s) |
| `MAX_RATE` | `50` | 到達させる上限レート (req/s) |
| `RAMP_DURATION` | `10m` | `START_RATE` から `MAX_RATE` まで上げ続ける時間。plateau・ramp-down は設けない |
| `PRE_ALLOCATED_VUS` | `50` | 事前確保する VU 数 |
| `MAX_VUS` | `500` | 応答遅延時に追加確保できる VU 数上限 |
| `ERROR_RATE_THRESHOLD` | `0.1` | エラー率がこの値を超えると `abortOnFail` により停止する (`real-mix.js` は開始 30 秒後から評価する) |

## 記録項目

実行後、k6 の標準出力（end-of-test summary）およびメトリクスから以下を記録する。

- ブレークポイントに到達したレート（到達前に `abortOnFail` で停止した場合はその時点のレート）
- その時点のエラー種別（`http_req_failed` の内訳、タイムアウトか 5xx か）
- `http_req_duration` の p95 / p99
- 同時刻の Netdata / `kubectl top` によるノード・Pod のリソース使用状況（本シナリオの出力範囲外。別途記録する）

Cloudflare 経由の実行結果とオリジン直の実行結果は、別々の実行として記録し混在させない。

## real-mix.js (実リクエスト比率の再現)

`breakpoint.js` が単一エンドポイントを叩くのに対し、`real-mix.js` は重み付き URL 一覧 (`MIX_FILE`) に従って各イテレーションで URL を 1 つ選んで GET する。実際にオリジンへ届いたリクエストの比率で負荷を掛けるためのシナリオ。実行器・閾値・`abortOnFail`・`BASE_URL` 等の環境変数は `breakpoint.js` と同じ (`TARGET_PATH` は使わない)。

比率データは実測値から逆算できるためリポジトリに置かない。実行時に JSON ファイルを渡す。

### MIX_FILE の形式

`{ "url": <BASE_URL に続くパス+クエリ>, "weight": <正の数> }` の配列。`weight` は相対値で、合計が 1 である必要はない。

```json
[
  { "url": "/api/globals/navigation?depth=1", "weight": 10 },
  { "url": "/api/topics?where%5Bpublished_at%5D%5Bless_than_equal%5D=__NOW__&limit=0", "weight": 2.5 }
]
```

- `url` 中の `__NOW__` は実行時に現在時刻 (分単位, `YYYY-MM-DDTHH:mm:00.000Z` を URL エンコードしたもの) へ置換される。公開日時で絞る where が分ごとに別 URL になるため
- クエリ文字列は URL エンコード済みで書く

### 実行例

Cloudflare 経由:

```bash
docker run --rm -i \
  -v "$PWD/mix.json:/mix.json:ro" \
  -e MIX_FILE=/mix.json \
  -e BASE_URL=https://cms.aramakisai.com \
  -e START_RATE=1 \
  -e MAX_RATE=50 \
  -e RAMP_DURATION=10m \
  grafana/k6 run - < real-mix.js
```

当日想定レートを一定時間保つ (Cloudflare 経由):

```bash
docker run --rm -i \
  -v "$PWD/mix.json:/mix.json:ro" \
  -e MIX_FILE=/mix.json \
  -e BASE_URL=https://cms.aramakisai.com \
  -e START_RATE=1 \
  -e MAX_RATE=<target-rps> \
  -e RAMP_DURATION=<duration> \
  -e HOLD_DURATION=<duration> \
  grafana/k6 run --out json=/out/result.json - < real-mix.js
```

`MIX_FILE` 以外の変数は `breakpoint.js` と同じ。追加で `HOLD_DURATION` (既定 `0s`) は、`MAX_RATE` に達した後そのレートを保つ時間。0 以外のとき `RAMP_DURATION` の後に同レートの保持段階が付く。保持中も閾値と `abortOnFail` は有効。保持段階の推移は `--out json=<file>` で時系列として取れる (コンテナで実行する場合は出力先を `-v` でマウントする)。追加で `SUMMARY_TOP` (既定 `30`) が path ごとの集計表に出す行数。

`CACHE_BUST` (既定 off、`1` で有効) は、各リクエスト URL に乱数のクエリ `lt=<乱数>` (既存クエリがあれば `&lt=`、無ければ `?lt=`) を付与する。Cloudflare の 60 秒エッジキャッシュを外し、オリジンへ全量届けるために使う。`name` タグは付与前のクエリを除いた path なので、path ごとの集計には影響しない。

`error_rate` と `http_req_failed` の閾値は `delayAbortEval: '30s'` を持つ。実行開始から 30 秒間は `abortOnFail` の評価を行わず、立ち上がり直後の少数サンプルによる誤停止を避ける。

### 出力

end-of-test の標準出力に、リクエスト数の多い path から順に、件数・エラー率・平均/p95 応答時間 (path は `name` タグ。クエリを除いたもの) と全体の p95/p99 を出す。`http_req_duration{name:<path>}` / `error_rate{name:<path>}` のサブメトリクスは `--out` の出力にも含まれる。
