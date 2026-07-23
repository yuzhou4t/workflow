#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-diagnose}"

case "$ACTION" in
  diagnose|setup|menu)
    : "${SIXBENCH_CONTAINER_NETWORK:?missing internal network}"
    : "${SIXBENCH_CONTROLLER_ALIAS:?missing controller alias}"
    docker network connect \
      --alias "$SIXBENCH_CONTROLLER_ALIAS" \
      "$SIXBENCH_CONTAINER_NETWORK" \
      "$HOSTNAME"
    if [[ "$ACTION" == "diagnose" ]]; then
      exec python3 /opt/sixbench/runtime_smoke.py
    fi
    if [[ "$ACTION" == "setup" ]]; then
      exec python3 /workspace/tools/student_handoff.py setup
    fi
    exec python3 /workspace/tools/student_handoff.py menu
    ;;
  *)
    exec "$@"
    ;;
esac
