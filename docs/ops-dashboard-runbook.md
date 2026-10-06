# 運用ポータル・運用ダッシュボード 運用手順

`dash.aramakisai.com` で提供する。executive はポータル (委員会サービスへのリンク集)、admin は加えて運用ダッシュボード (`/admin/`) を使える。構成は ArgoCD Application `ops-dashboard` (namespace `ops-dashboard`、portal = nginx + oauth2-proxy + Homer、collector = Python)。マニフェストは `gitops/manifests/prod/ops-dashboard/`。変更はすべて PR → マージ → ArgoCD 同期で反映する (クラスタへの直接操作はしない)。

## リンクの追加・変更

リンクの単一ソースは `gitops/manifests/prod/ops-dashboard/portal/links.yaml`。

- 公開してよい URL: `services[].items[]` に `name` / `icon` / `subtitle` / `url` を追加する。
- 内部向け URL (公開リポジトリに書かない): 項目に `url_env: <環境変数名>` を書き、次の手順で値を渡す。環境変数が空だと、その項目だけが表示されない。
  1. Infisical (prod) にキー (例 `PORTAL_NOTION_URL`) を登録する。
  2. `portal/external-secret.yaml` の内部向けリンク用 ExternalSecret の `data` に `remoteRef.key` を追加し、`portal/deployment.yaml` の環境変数 (optional) に同名で渡す。
  3. PR をマージし、`kubectl get externalsecret -n ops-dashboard` で反映を確認する (`spec.data` を足した場合は ArgoCD が反映を見逃すことがあるため、`.kiro/steering/tech.md` の既知問題の手順で確認する)。
- admin 専用の項目は `portal/admin-overlay.yaml` に書く。
- 生成の検証: `portal/test-generate.sh`。

## プラン・期待サーバーの宣言

`gitops/manifests/prod/ops-dashboard/collector/dashboard.toml` を編集して PR を出す。collector は起動時に検証し、誤りがあると起動に失敗する (ConfigMap 反映後に Pod を再起動して確認する)。

- プランの切り替え: `[[plans]]` の `from` / `until` を書き換える。同一 `service` の期間は重ねない。
- 稼働しているべきサーバー: `[[servers]]` に `name` を列挙する (イベント期間中に `prod-node-2` / `prod-node-3` を追加する場合は、期間に合わせて追加・削除する)。
- 資格情報の有効期限: 期限のあるトークンを再発行したら `[[credentials]]` の `expires_on` を更新する。
- 凍結中のワークロード: `[exclude]` に Namespace または名前を列挙する。
- 閾値・情報源の宣言: `[thresholds]`、`[sources."<source_id>"]`。

## executive・admin の付与・剥奪

ロールは Zitadel で管理する (手順は Zitadel の既存運用)。反映の仕組みは次のとおり。

1. Zitadel のロール `executive` / `admin` が ID token の `groups` claim に入る。
2. oauth2-proxy が `groups` claim を読む (`--oidc-groups-claim=groups`)。ポータル全体は `executive`、`/admin/` 配下 (運用ダッシュボード) と admin 用リンクは `admin` を持つ場合だけ通す。
3. セッション cookie は `--cookie-refresh=1h` で 1 時間ごとに refresh token から ID token を取り直し、ロールの変更を取り込む。

付与は最大 1 時間以内に反映され、剥奪は最大 1 時間以内に効く (cookie の有効期限は 12 時間だが、refresh 時にロールを失っていれば拒否される)。即時に反映したい場合は、利用者がログアウト (`/oauth2/sign_out`) して再ログインする。

## 資格情報

collector が使う読み取り専用資格情報の権限・所有アカウント・ローテーション手順は `.kiro/steering/tech.md` の「手動発行する資格情報 (ops-dashboard)」を参照。

## データの扱い

collector の SQLite と mailserver の状態・ログ・レポート用の PVC は再構築時に復元されない。詳細は `.kiro/steering/dr.md`。
