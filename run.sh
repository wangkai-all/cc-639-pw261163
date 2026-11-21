#!/usr/bin/env bash
# Start the demo imaging service and the workstation web app.
set -euo pipefail
cd "$(dirname "$0")"

PY=.venv/bin/python
if [ ! -x "$PY" ]; then
  echo "virtualenv not found; create it first:" >&2
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

"$PY" -m server.demo_scp &
DEMO_PID=$!
trap 'kill $DEMO_PID 2>/dev/null || true' EXIT

sleep 1
echo "Demo imaging service:  127.0.0.1:11112 (AE DEMOQRSCP)"
echo "Workstation web UI:    http://127.0.0.1:8000"
exec "$PY" -m uvicorn server.main:app --host 127.0.0.1 --port 8000
