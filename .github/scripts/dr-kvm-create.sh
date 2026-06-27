#!/usr/bin/env bash
# DR KVM テスト環境作成スクリプト
#
# Debian 13 (trixie) KVM VM を作成し、recovery.sh (KVM テストモード) を実行するための
# 前提環境を整える。prod-node-1 の Hetzner ノードに相当する VM を作る。
#
# 前提条件 (Arch Linux):
#   sudo pacman -S qemu-base libvirt virt-install cdrtools
#   sudo systemctl enable --now libvirtd
#   sudo usermod -aG libvirt $USER && newgrp libvirt
#
# 使い方:
#   bash .github/scripts/dr-kvm-create.sh [--destroy]
#   # VM 作成後、表示されるコマンドで recovery.sh を実行する
#
# クリーンアップ:
#   bash .github/scripts/dr-kvm-create.sh --destroy

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VM_NAME="prod-node-1-drtest"
VM_VCPUS=4
VM_RAM_MB=8192
VM_DISK_GB=40
DEBIAN_VERSION="trixie"      # Debian 13
CLOUD_IMAGE_URL="https://cloud.debian.org/images/cloud/${DEBIAN_VERSION}/latest/debian-13-genericcloud-amd64.qcow2"
KVM_CACHE_DIR="/var/tmp/dr-kvm"
IMAGE_CACHE="${KVM_CACHE_DIR}/debian-${DEBIAN_VERSION}-cloud.qcow2"
VM_DISK="${KVM_CACHE_DIR}/${VM_NAME}.qcow2"
CLOUD_INIT_DIR="${KVM_CACHE_DIR}/cloud-init"
KVM_INVENTORY="${REPO_ROOT}/ansible/inventory/kvm-test.yml"
KUBECONFIG_OUT="/tmp/kubeconfig-kvm-test"

export LIBVIRT_DEFAULT_URI="qemu:///system"

log() { echo "$(date -u '+%H:%M:%S') [kvm-create] $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# ============================================================
# --destroy: VM 削除
# ============================================================

if [[ "${1:-}" == "--destroy" ]]; then
  log "VM ${VM_NAME} を削除します"
  virsh destroy  "${VM_NAME}" 2>/dev/null || true
  virsh undefine "${VM_NAME}" --remove-all-storage 2>/dev/null || true
  rm -f "${CLOUD_INIT_DIR}/user-data" "${CLOUD_INIT_DIR}/meta-data" \
        "${CLOUD_INIT_DIR}/cloud-init.iso" 2>/dev/null || true
  log "削除完了"
  exit 0
fi

# ============================================================
# 前提ツール確認
# ============================================================

for cmd in virt-install virsh qemu-img mkisofs; do
  command -v "$cmd" &>/dev/null || die "${cmd} が見つかりません。以下を実行してください:
  sudo pacman -S qemu-base libvirt virt-install cdrtools
  sudo systemctl enable --now libvirtd
  sudo usermod -aG libvirt \${USER} && newgrp libvirt"
done

# ============================================================
# SSH キー準備 (Ansible 用)
# ============================================================

SSH_KEY_FILE="${HOME}/.ssh/id_ed25519"
SSH_PUB_FILE="${SSH_KEY_FILE}.pub"

if [[ ! -f "${SSH_KEY_FILE}" ]]; then
  log "SSH キーを生成します"
  ssh-keygen -t ed25519 -N "" -f "${SSH_KEY_FILE}"
fi

SSH_PUB=$(cat "${SSH_PUB_FILE}")

# ============================================================
# Ubuntu Cloud Image ダウンロード
# ============================================================

mkdir -p "${KVM_CACHE_DIR}"

# default ネットワーク定義・起動
if ! virsh net-info default &>/dev/null; then
  log "libvirt default ネットワークを定義します"
  NET_XML=$(mktemp /var/tmp/libvirt-default-net.XXXXXX.xml)
  cat > "${NET_XML}" <<'NETXML'
<network>
  <name>default</name>
  <forward mode='nat'/>
  <bridge name='virbr0' stp='on' delay='0'/>
  <ip address='192.168.122.1' netmask='255.255.255.0'>
    <dhcp>
      <range start='192.168.122.2' end='192.168.122.254'/>
    </dhcp>
  </ip>
</network>
NETXML
  virsh net-define "${NET_XML}"
  rm -f "${NET_XML}"
fi

NET_STATE=$(virsh net-info default 2>/dev/null | awk '/^Active:/{print $2}')
if [[ "${NET_STATE}" != "yes" ]]; then
  log "libvirt default ネットワークを起動します"
  if ! virsh net-start default 2>/dev/null; then
    # 既に起動中なら無視
    virsh net-info default 2>/dev/null | grep -q "yes" || \
      die "default ネットワーク起動失敗。'journalctl -u libvirtd -n 50' で確認してください"
  fi
  virsh net-autostart default 2>/dev/null || true
fi

if [[ ! -f "${IMAGE_CACHE}" ]]; then
  log "Debian ${DEBIAN_VERSION} cloud image をダウンロードします (約 400MB)"
  curl -L "${CLOUD_IMAGE_URL}" -o "${IMAGE_CACHE}"
fi

# ============================================================
# VM ディスク作成
# ============================================================

if virsh dominfo "${VM_NAME}" &>/dev/null; then
  log "VM ${VM_NAME} は既に存在します"
else
  log "VM ディスクを作成します (${VM_DISK_GB}GB)"
  qemu-img create -f qcow2 -b "${IMAGE_CACHE}" -F qcow2 "${VM_DISK}" "${VM_DISK_GB}G"

  # ============================================================
  # cloud-init 設定
  # ============================================================

  mkdir -p "${CLOUD_INIT_DIR}"

  cat > "${CLOUD_INIT_DIR}/user-data" <<EOF
#cloud-config
hostname: ${VM_NAME}
fqdn: ${VM_NAME}.local
manage_etc_hosts: true

users:
  - name: root
    ssh_authorized_keys:
      - ${SSH_PUB}

ssh_pwauth: false
disable_root: false

runcmd:
  - sed -i 's/^#*PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config
  - systemctl restart ssh || systemctl restart sshd
EOF

  cat > "${CLOUD_INIT_DIR}/meta-data" <<EOF
instance-id: ${VM_NAME}-$(date +%s)
local-hostname: ${VM_NAME}
EOF

  # 明示的 DHCP 設定 (cloud-init が network を構成しない場合の保険)
  cat > "${CLOUD_INIT_DIR}/network-config" <<'EOF'
version: 2
ethernets:
  id0:
    match:
      name: "en*"
    dhcp4: true
    dhcp6: false
  id1:
    match:
      name: "eth*"
    dhcp4: true
    dhcp6: false
EOF

  mkisofs -output "${CLOUD_INIT_DIR}/cloud-init.iso" \
    -volid cidata -joliet -rock \
    "${CLOUD_INIT_DIR}/user-data" \
    "${CLOUD_INIT_DIR}/meta-data" \
    "${CLOUD_INIT_DIR}/network-config"

  # ============================================================
  # VM 作成
  # ============================================================

  SERIAL_LOG="${KVM_CACHE_DIR}/serial.log"
  NVRAM_FILE="${KVM_CACHE_DIR}/ovmf-nvram.fd"
  cp /usr/share/edk2/x64/OVMF_VARS.4m.fd "${NVRAM_FILE}"
  log "KVM VM ${VM_NAME} を作成します (${VM_VCPUS} vCPU, ${VM_RAM_MB}MB RAM, UEFI)"
  log "シリアルログ: ${SERIAL_LOG}"
  virt-install \
    --name "${VM_NAME}" \
    --vcpus "${VM_VCPUS}" \
    --memory "${VM_RAM_MB}" \
    --disk path="${VM_DISK}",format=qcow2 \
    --disk path="${CLOUD_INIT_DIR}/cloud-init.iso",device=cdrom \
    --os-variant debian12 \
    --network network=default \
    --graphics none \
    --serial file,path="${SERIAL_LOG}" \
    --boot loader=/usr/share/edk2/x64/OVMF_CODE.4m.fd,loader.readonly=yes,loader.secure=no,loader.type=pflash,nvram="${NVRAM_FILE}" \
    --noautoconsole \
    --import

  log "VM 作成完了。起動を待機します"
fi

# ============================================================
# VM の IP アドレスを取得
# ============================================================

log "VM の IP アドレスを待機します (最大 6 分)"
ELAPSED=0
VM_IP=""
while true; do
  # まず DHCP lease から取得
  VM_IP=$(virsh net-dhcp-leases default 2>/dev/null \
    | awk '/ipv4/{print $5}' | cut -d/ -f1 | head -1 || true)
  # fallback: domifaddr --source lease
  if [[ -z "$VM_IP" ]]; then
    VM_IP=$(virsh domifaddr "${VM_NAME}" --source lease 2>/dev/null \
      | awk '/ipv4/{split($4, a, "/"); print a[1]}' | head -1 || true)
  fi
  [[ -n "$VM_IP" ]] && break
  (( ELAPSED >= 360 )) && die "VM の IP 取得タイムアウト (virsh net-dhcp-leases default で確認)"
  sleep 5
  ELAPSED=$(( ELAPSED + 5 ))
done
log "VM IP: ${VM_IP}"

# ============================================================
# SSH 疎通確認
# ============================================================

log "SSH 疎通確認 (最大 3 分)"
ELAPSED=0
while true; do
  ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
    root@"${VM_IP}" echo "SSH OK" &>/dev/null && break
  (( ELAPSED >= 180 )) && die "SSH 接続タイムアウト"
  sleep 5
  ELAPSED=$(( ELAPSED + 5 ))
done
log "SSH 接続確認完了"

# ============================================================
# kvm-test.yml を VM の IP に更新
# ============================================================

log "kvm-test.yml を更新します (IP: ${VM_IP})"
sed -i "s|ansible_host: .*|ansible_host: ${VM_IP}|g" "${KVM_INVENTORY}"
log "inventory 更新完了: ${KVM_INVENTORY}"

# ============================================================
# 実行コマンドを出力
# ============================================================

log ""
log "=========================================================="
log "KVM VM 準備完了"
log "VM IP: ${VM_IP}"
log "KUBECONFIG 出力先: ${KUBECONFIG_OUT}"
log ""
log "recovery.sh を実行してください:"
log ""
log "  DR_SKIP_TAILSCALE_DELETE=1 \\"
log "  DR_SKIP_TFC=1 \\"
log "  DR_SKIP_TAILSCALE_WAIT=1 \\"
log "  DR_ANSIBLE_INVENTORY=${KVM_INVENTORY} \\"
log "  KUBECONFIG_FILE=${KUBECONFIG_OUT} \\"
log "  infisical run --env=prod -- bash .github/scripts/recovery.sh"
log ""
log "VM を削除するには:"
log "  bash .github/scripts/dr-kvm-create.sh --destroy"
log "=========================================================="
