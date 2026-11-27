#!/usr/bin/env bash
# SHADOW AI — dashboard by default, or a verb if one is given.
set -euo pipefail
cd "$(dirname "$0")"
PY="python3"
[ -x ".venv/bin/python" ] && PY=".venv/bin/python"

if [ "$#" -eq 0 ]; then
  echo "  SHADOW AI dashboard on http://127.0.0.1:40330"
  command -v xdg-open >/dev/null 2>&1 && xdg-open http://127.0.0.1:40330 >/dev/null 2>&1 &
  command -v open >/dev/null 2>&1 && open http://127.0.0.1:40330 >/dev/null 2>&1 &
  exec "$PY" -m shadow serve --port 40330
fi
exec "$PY" -m shadow "$@"
