# zitadel-bootstrap

`infisical-auth` Secret作成と同型の、GitOps外の例外的初期化ロール。

Zitadelは`gitops/manifests/prod/zitadel/statefulset.yaml`の`ZITADEL_FIRSTINSTANCE_*`
環境変数により、初回起動時に一度だけ組織・管理者・Terraform provider用machine user
(`terraform-provider`, role: `IAM_OWNER`)とPersonal Access Tokenを自動発行し、
Pod内の一時ファイル(`/zitadel-data/zitadel-admin-sa.pat`)へ書き込む。このPATは
再取得不可能な一度きりの成果物のため、本ロールはPod再作成等で失われる前に
`kubectl exec`で回収しローカルファイルへ保存する。

## 使い方 (k3d検証環境限定)

```bash
export ZITADEL_POC_KUBECONFIG=/path/to/k3d-kubeconfig.yaml   # 必須。ambient contextには依存しない
ansible-playbook ansible/playbooks/zitadel-bootstrap.yml
```

再実行してもローカルに既にPATがあればスキップする(冪等)。

## 本番運用時のInfisical登録

このロールはPoC用k3dインスタンスの一時トークンを扱うため、本番Infisicalへの登録は
行わない。本番Zitadelで同様の手順によりPATを取得した場合、以下のように登録する
(infisical CLIは出力に平文シークレットを含みうるため`/dev/null`への抑制必須):

```bash
infisical secrets set --env=prod ZITADEL_TERRAFORM_PAT="$(cat <取得したPATファイル>)" >/dev/null 2>&1
```
