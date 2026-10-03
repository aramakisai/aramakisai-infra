#!/usr/bin/env bash
# 使い捨て検証環境。ノードコンテナは systemd 入り privileged Debian (本番ノード相当の VM 代用)。
# shellcheck disable=SC2086
set -euo pipefail
NET=scaleout-verify
up() {  # up <n>
  local n=$1
  docker volume create sv-node-$n-rancher >/dev/null; docker volume create sv-node-$n-kubelet >/dev/null
  docker run -d --name sv-node-$n --hostname sv-node-$n --network $NET --ip 10.0.1.$n --privileged \
    --cgroupns=private --tmpfs /run --tmpfs /tmp \
    -v sv-node-$n-rancher:/var/lib/rancher -v sv-node-$n-kubelet:/var/lib/kubelet scaleout-verify-node >/dev/null
  # Cilium が /sys/fs/bpf を shared mount として要求する
  docker exec sv-node-$n sh -c 'mount -t bpf bpf /sys/fs/bpf 2>/dev/null; mount --make-rshared /'
}
case $1 in
  net) docker network inspect $NET >/dev/null 2>&1 || docker network create --subnet 10.0.1.0/24 --gateway 10.0.1.254 $NET ;;
  up) up $2 ;;
  down) docker rm -f sv-node-$2 >/dev/null 2>&1 || true; docker volume rm sv-node-$2-rancher sv-node-$2-kubelet >/dev/null 2>&1 || true ;;
  destroy) for n in 1 2 3; do "$0" down $n; done; docker network rm $NET >/dev/null 2>&1 || true; docker rmi scaleout-verify-node >/dev/null 2>&1 || true ;;
esac
