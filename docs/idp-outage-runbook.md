# IdP (Zitadel) 停止時の緊急手順 — 影響範囲と代替手段

## 概要

Zitadel (`https://idp.aramakisai.com`) が停止したときの影響と、各サービスの代わりの手段をまとめる。Zitadel 本体の復旧は GitOps (PR → マージ → ArgoCD 同期) で行う。ArgoCD の UI/API も Zitadel に依存するため、同期は「ArgoCD を UI なしで同期する」の手順で行う。

## 影響範囲

| 区分 | 対象 | 状態 |
|---|---|---|
| 止まる | CMS 管理画面の SSO ログイン | 「CMS への緊急ログイン」を使う |
| 止まる | ops-dashboard (`dash.aramakisai.com`) | 復旧待ち |
| 止まる | ArgoCD の UI/API (`admin.enabled: "false"`、oidc の issuer は Zitadel) | 「ArgoCD を UI なしで同期する」を使う |
| 止まる | Vaultwarden (SSO 必須) | 復旧待ち |
| 止まる | Roundcube (OAUTHBEARER) | 復旧待ち |
| 止まる | スタッフのスタンプ交換画面 (`STAFF_OIDC`) | 復旧待ち |
| 止まる | サイネージ端末の Cloudflare Access 再認証 | `allowed_idps` は Zitadel のみ (`terraform/access.tf:93-109`)。セッションが切れた端末は再ログインできない |
| 続く | 学生団体のローカルログイン | |
| 続く | CMS の緊急用ローカルアカウント | |
| 続く | メール | 認証キャッシュにフォールバックする。`.kiro/steering/dr.md` の「アカウント・認証・送信者認可の構成」を参照 |
| 続く | `make kubectl` | クライアント証明書で認証する |
| 続く | Infisical | Zitadel に依存しない |

## CMS への緊急ログイン

手順の本体は aramakisai-web リポジトリの [docs/cms-operations.md](https://github.com/aramakisai/aramakisai-web/blob/main/docs/cms-operations.md)「実行委員の緊急用ローカルアカウント」にある。認証情報は Infisical prod の `CMS_BREAKGLASS_EMAIL` と `CMS_BREAKGLASS_PASSWORD` に保存されている。

## ArgoCD を UI なしで同期する

> **未検証**: この手順は本番で一度も実行していない。

`argocd --core` は手元で API server を DisableAuth で起動し、argocd-redis と argocd-repo-server へ port-forward する。実際の同期はクラスタ内の application-controller が行う。Zitadel を経由しない。

zitadel Application は自動同期ではなく手動同期 (`gitops/apps/prod/zitadel.yaml`)。Zitadel を直す変更をマージした後は、この手順で同期する。

前提:

- argocd CLI v3.4.4 (サーバーと同じ版) を用意する。入手先は `https://github.com/argoproj/argo-cd/releases/download/v3.4.4/argocd-linux-amd64`。
- `make kube-login` 済みであること (GitHub に依存し、Zitadel には依存しない)。
- `gitops/manifests/prod/kube-access/humans.yaml` に cluster-admin 相当の binding があること。

```bash
# コンテキストの namespace を argocd にする (--core が argocd namespace を参照するため)
kubectl config set-context aramakisai-prod --namespace=argocd

# Application の一覧が表示されれば接続できている
argocd app list --core --kube-context aramakisai-prod

# 同期。zitadel が Synced / Healthy になるまで app list で確認する
argocd app sync <app> --core --kube-context aramakisai-prod
```

## Zitadel 本体の状態確認

zitadel は replicas 3、zitadel-db は CNPG instances 3。

```bash
# zitadel が 3 Pod、zitadel-db が 3 Pod とも READY / Running であること
make kubectl ARGS="get pods -n zitadel"

# STATUS が "Cluster in healthy state"、INSTANCES と READY が 3 であること
make kubectl ARGS="get cluster -n zitadel"
```
