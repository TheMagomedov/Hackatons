#!/usr/bin/env bash
set -euo pipefail
studio_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd -- "$studio_root"
if [[ -n "${PYTHON_EXECUTABLE:-}" ]]; then
  studio_python="$PYTHON_EXECUTABLE"
elif [[ -x .venv/bin/python ]]; then
  studio_python="$studio_root/.venv/bin/python"
elif [[ -x ../venv/bin/python ]]; then
  studio_python="$studio_root/../venv/bin/python"
else
  studio_python=python3
fi
exec "$studio_python" "$studio_root/scripts/run-local-tunnel.py" "$@"
