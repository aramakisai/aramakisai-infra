# zitadel-bootstrap

`infisical-auth` Secret作成と同型の、GitOps外の例外的初期化ロール。

Zitadelは`gitops/manifests/prod/zitadel/statefulset.yaml`の`ZITADEL_FIRSTINSTANCE_*`
環境変数により、初回起動時に一度だけ組織・管理者・Terraform provider用machine user
(`terraform-provider`, role: `IAM_OWNER`)とPersonal Access Tokenを自動発行し、
Pod内の一時ファイル(`/zitadel-data/zitadel-admin-sa.pat`)へ書き込む。このPATは
再取得不可能な一度きりの成果物のため、本ロールはPod再作成等で失われる前に
`kubectl exec`で回収しローカルファイルへ保存する。

## 使い方 (本番)

`make kubectl`と同じ方式で、Infisicalの`KUBECONFIG`シークレット(内容そのもの)を
`infisical run`経由で読み込みkubeconfigファイルを自動生成する。ambientな
`kubectl config current-context`には依存しない。

```bash
infisical run -- ansible-playbook -i ansible/inventory/tailscale.yml ansible/playbooks/zitadel-bootstrap.yml
```

## 使い方 (k3d検証環境)

```bash
export ZITADEL_POC_KUBECONFIG=/path/to/k3d-kubeconfig.yaml   # 必須。指定時は本番kubeconfig生成をスキップする
ansible-playbook ansible/playbooks/zitadel-bootstrap.yml
```

再実行してもローカルに既にPATがあればスキップする(冪等)。

## Infisicalへの登録

このロールはPATファイルへの保存までを行い、Infisicalへの登録は行わない。
取得したPATをterraform applyで使う場合は`TF_VAR_zitadel_token`として登録する
(infisical CLIは出力に平文シークレットを含みうるため`/dev/null`への抑制必須):

```bash
infisical secrets set --env=prod TF_VAR_zitadel_token="$(cat <取得したPATファイル>)" >/dev/null 2>&1
```

## project/role/application/action等の投入 (task 9.2)

```bash
ansible-playbook ansible/playbooks/zitadel-resources.yml
```

## Terraform/Ansible管理外のインスタンス設定のimport (task 9.3)

Lockout Policy等、`resources.yml`が管理しないインスタンス設定を
`POST /admin/v1/import`で反映する。対象は`vars/admin_import.yml`参照。
importは一括ロード用APIで再実行に強くない(既にカスタム化済みの場合は
自動でスキップする)ため、通常はk3d検証環境で一度だけ実行する。

```bash
ansible-playbook ansible/playbooks/zitadel-admin-import.yml
```

`ZITADEL_EXTERNAL_DOMAIN`/`ZITADEL_POC_KUBECONFIG`未指定時はk3d検証環境が
対象になる(本番エンドポイントはハードコードされていない)。
