#!/bin/zsh
set -u

SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR" || exit 1
exec python3 tools/student_handoff.py menu
