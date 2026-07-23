#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-diagnose}"
case "$ACTION" in
  diagnose|setup|menu) ;;
  *)
    echo "[SixBench] 不支持的 Windows 动作：$ACTION" >&2
    exit 2
    ;;
esac

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_PACKAGE_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
IMAGE_NAME="sixbench-windows-runtime:pilot-r1"
PATH_DIGEST="$(printf '%s' "$SOURCE_PACKAGE_ROOT" | sha256sum | cut -c1-12)"
NETWORK_NAME="sixbench-internal-$PATH_DIGEST"
CONTROLLER_NAME="sixbench-controller-$PATH_DIGEST"
CONTROLLER_ALIAS="sixbench-controller"

if ! grep -qi microsoft /proc/sys/kernel/osrelease; then
  echo "[SixBench] 本入口只允许从 Windows WSL2 运行。" >&2
  exit 2
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "[SixBench] WSL 中未找到 docker。请安装并启动 Docker Desktop，开启 WSL integration。" >&2
  exit 2
fi
if ! docker info >/dev/null 2>&1; then
  echo "[SixBench] Docker daemon 不可用。请先启动 Docker Desktop。" >&2
  exit 2
fi
if [[ ! -S /var/run/docker.sock ]]; then
  echo "[SixBench] 未找到 /var/run/docker.sock。请开启当前 WSL 发行版的 Docker Desktop integration。" >&2
  exit 2
fi

PACKAGE_ROOT="$SOURCE_PACKAGE_ROOT"
if [[ "$ACTION" != "diagnose" ]]; then
  FINGERPRINT="$(
    sha256sum \
      "$SOURCE_PACKAGE_ROOT/ASSIGNMENT.json" \
      "$SOURCE_PACKAGE_ROOT/release-package.json" \
      "$SOURCE_PACKAGE_ROOT/tools/windows/Dockerfile" \
      "$SOURCE_PACKAGE_ROOT/tools/windows/run-in-wsl.sh" \
    | sha256sum \
    | cut -c1-12
  )"
  WORKSPACES_ROOT="${HOME:?}/.local/share/sixbench-windows"
  PACKAGE_ROOT="$WORKSPACES_ROOT/$PATH_DIGEST-$FINGERPRINT/workspace"
  if [[ ! -d "$PACKAGE_ROOT" ]]; then
    echo "[SixBench] 首次运行：正在把测试包复制到 WSL 的 Linux 文件系统……"
    mkdir -p "$(dirname -- "$PACKAGE_ROOT")"
    TEMP_WORKSPACE="$(mktemp -d "$(dirname -- "$PACKAGE_ROOT")/copying.XXXXXX")"
    cp -a "$SOURCE_PACKAGE_ROOT/." "$TEMP_WORKSPACE/"
    mv "$TEMP_WORKSPACE" "$PACKAGE_ROOT"
  fi
fi

SCRIPT_DIR="$PACKAGE_ROOT/tools/windows"
RUNTIME_DIR="$PACKAGE_ROOT/.sixbench-windows"
mkdir -p "$RUNTIME_DIR/controller-home" "$RUNTIME_DIR/controller-tmp" "$PACKAGE_ROOT/RETURN"

echo "[SixBench] 检查固定运行镜像（首次会联网下载并构建，后续复用缓存）……"
docker build \
  --tag "$IMAGE_NAME" \
  --file "$SCRIPT_DIR/Dockerfile" \
  "$SCRIPT_DIR"

if ! docker network inspect "$NETWORK_NAME" >/dev/null 2>&1; then
  docker network create --internal "$NETWORK_NAME" >/dev/null
fi

SOCKET_GID="$(stat -c '%g' /var/run/docker.sock)"
TTY_ARGS=()
if [[ -t 0 && -t 1 ]]; then
  TTY_ARGS=(-it)
fi

set +e
docker run --rm "${TTY_ARGS[@]}" \
  --name "$CONTROLLER_NAME" \
  --network bridge \
  --user "$(id -u):$(id -g)" \
  --group-add "$SOCKET_GID" \
  --mount "type=bind,source=$PACKAGE_ROOT,target=/workspace" \
  --mount "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock" \
  --workdir /workspace \
  --env "HOME=/workspace/.sixbench-windows/controller-home" \
  --env "TMPDIR=/workspace/.sixbench-windows/controller-tmp" \
  --env "SIXBENCH_WINDOWS_WSL_DOCKER=1" \
  --env "SIXBENCH_RUNTIME_IMAGE=$IMAGE_NAME" \
  --env "SIXBENCH_CONTAINER_NETWORK=$NETWORK_NAME" \
  --env "SIXBENCH_CONTROLLER_ALIAS=$CONTROLLER_ALIAS" \
  --env "SIXBENCH_DOCKER_HOST_ROOT=$PACKAGE_ROOT" \
  --env "SIXBENCH_CONTROLLER_ROOT=/workspace" \
  --env "SIXBENCH_WINDOWS_SYNC_ROOT=$SOURCE_PACKAGE_ROOT" \
  "$IMAGE_NAME" "$ACTION"
SIXBENCH_EXIT=$?
set -e

if [[ "$PACKAGE_ROOT" != "$SOURCE_PACKAGE_ROOT" && -d "$PACKAGE_ROOT/RETURN" ]]; then
  mkdir -p "$SOURCE_PACKAGE_ROOT/RETURN"
  cp -a "$PACKAGE_ROOT/RETURN/." "$SOURCE_PACKAGE_ROOT/RETURN/"
fi
exit "$SIXBENCH_EXIT"
