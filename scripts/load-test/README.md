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
| `ERROR_RATE_THRESHOLD` | `0.1` | エラー率がこの値を超えると `abortOnFail` により即時停止する |

## 記録項目

実行後、k6 の標準出力（end-of-test summary）およびメトリクスから以下を記録する。

- ブレークポイントに到達したレート（到達前に `abortOnFail` で停止した場合はその時点のレート）
- その時点のエラー種別（`http_req_failed` の内訳、タイムアウトか 5xx か）
- `http_req_duration` の p95 / p99
- 同時刻の Netdata / `kubectl top` によるノード・Pod のリソース使用状況（本シナリオの出力範囲外。別途記録する）

Cloudflare 経由の実行結果とオリジン直の実行結果は、別々の実行として記録し混在させない。
