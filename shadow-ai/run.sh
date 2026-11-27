#!/usr/bin/env bash
# SHADOW AI — dashboard by default, or a verb if one is given.
set -euo pipefail
cd "$(dirname "$0")"
PY="python3"
[ -x ".venv/bin/python" ] && PY=".venv/bin/python"

case "${1:-}" in
  live)   exec "$PY" -m shadow doctor --live ;;
  doctor) exec "$PY" -m shadow doctor ;;
  fleet)
    shift
    if [ "$#" -lt 1 ]; then
      echo "  usage: ./run.sh fleet \"open shop.local and read the title\" [a,b,c]" >&2
      exit 2
    fi
    exec "$PY" -m shadow fleet "$@" ;;
  "")
    echo "  SHADOW AI dashboard on http://127.0.0.1:40330"
    command -v xdg-open >/dev/null 2>&1 && xdg-open http://127.0.0.1:40330 >/dev/null 2>&1 &
    command -v open >/dev/null 2>&1 && open http://127.0.0.1:40330 >/dev/null 2>&1 &
    exec "$PY" -m shadow serve --port 40330 ;;
  *)  exec "$PY" -m shadow "$@" ;;
esac
