#!/usr/bin/env bash
set -euo pipefail

# リポジトリのルートディレクトリに移動
cd "$(dirname "$0")/.."

# 引数の処理（--auto-approve または -y が指定された場合は Terraform に auto-approve を渡す）
AUTO_APPROVE=""
for arg in "$@"; do
    if [ "$arg" == "--auto-approve" ] || [ "$arg" == "-y" ]; then
        AUTO_APPROVE="-auto-approve"
    fi
done

echo "=== 1. Terraform Apply ==="
(cd terraform && infisical run -- bash -c "export TF_VAR_authentik_token=\$AUTHENTIK_TOKEN && terraform apply ${AUTO_APPROVE}")

echo "=== 2. Waiting for Tailscale SSH Connection ==="
echo "Polling ansible ping to verify SSH reachability..."
RETRIES=0
MAX_RETRIES=60
until ansible -i ansible/inventory/tailscale.yml all -m ping > /dev/null 2>&1; do
    RETRIES=$((RETRIES + 1))
    if [ ${RETRIES} -ge ${MAX_RETRIES} ]; then
        echo "ERROR: Nodes did not become reachable via Tailscale SSH within timeout (10 minutes)."
        exit 1
    fi
    echo "  Nodes not reachable yet (attempt ${RETRIES}/${MAX_RETRIES}), retrying in 10s..."
    sleep 10
done
echo "  ✅ All nodes are reachable via SSH."

echo "=== 3. Ansible Bootstrapping ==="
ansible-playbook -i ansible/inventory/tailscale.yml ansible/playbooks/k3s-bootstrap.yml

echo "🎉 Deployment Completed Successfully!"
