#!/usr/bin/env bash
# SHADOW AI — SHARDX AUTOPILOT ULTIMATE : Linux / macOS installer
set -euo pipefail
cd "$(dirname "$0")"

echo
echo "  SHADOW AI  -  SHARDX AUTOPILOT ULTIMATE"
echo "  local install, nothing leaves this computer"
echo

PY=""
for candidate in python3.12 python3.11 python3.10 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && \
     "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,8) else 1)' 2>/dev/null; then
    PY="$candidate"; break
  fi
done
if [ -z "$PY" ]; then
  echo "  Python 3.8 or newer is required. Install it and run this again."
  exit 1
fi
echo "  python:  $PY ($("$PY" -c 'import sys; print(sys.version.split()[0])'))"

echo "  install: creating a private environment in .venv"
"$PY" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip --quiet
python -m pip install --quiet -e .

[ -f shadow.json ] || { cp shadow.json.example shadow.json; echo "  config:  shadow.json created"; }

echo "  check:   running the self test"
if python run_tests.py >/dev/null 2>&1; then echo "  check:   ok"; else
  echo "  some tests failed — run: .venv/bin/python run_tests.py -v"; fi

echo "  check:   environment"
.venv/bin/shadow doctor || true

cat <<'MSG'

  installed. Start it with:
      ./run.sh                 dashboard on http://127.0.0.1:40330
      ./run.sh run "..."       one goal from the terminal
MSG
