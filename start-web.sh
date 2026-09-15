#!/usr/bin/env bash
#
# Starts the Assarium web app on port 3000. It proxies /api to the API on 8000, so the
# browser only ever sees one origin and there is no CORS preflight.
#
#   ./start-web.sh              start, or report what already holds the port
#   ./start-web.sh --restart    stop whatever is on the port first, then start
#
set -euo pipefail
cd "$(dirname "$0")/apps/web"

PORT="${ASSARIUM_WEB_PORT:-3000}"
RESTART=0
# Written as an explicit if: the `[ a ] || [ b ] && { ... }` form has surprising exit
# status under `set -e` and is easy to misread.
if [ "${1:-}" = "--restart" ] || [ "${1:-}" = "-r" ]; then
  RESTART=1
  shift
fi

holder() { lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1; }

PID="$(holder || true)"
if [ -n "$PID" ]; then
  NAME="$(ps -p "$PID" -o comm= 2>/dev/null || echo 'unknown process')"
  if [ "$RESTART" -eq 0 ]; then
    echo "Port $PORT is already in use by $NAME (pid $PID)."
    echo "  If that is the web app already running, just open http://localhost:$PORT"
    echo "  Restart it   ./start-web.sh --restart"
    echo "  Or use       ASSARIUM_WEB_PORT=3001 ./start-web.sh"
    exit 0
  fi
  echo "Stopping $NAME (pid $PID) on port $PORT..."
  kill "$PID" 2>/dev/null || true
  for _ in $(seq 1 20); do [ -z "$(holder || true)" ] && break; sleep 0.25; done
fi

[ -d node_modules ] || { echo "Installing web dependencies..."; npm install; }

# Warn rather than fail: the UI loads fine without the API, it just has no data.
curl -fsS --max-time 2 "http://127.0.0.1:${ASSARIUM_API_PORT:-8000}/api/health" >/dev/null 2>&1 \
  || echo "Note: the API does not answer on port ${ASSARIUM_API_PORT:-8000}. Run ./start-api.sh in another terminal."

echo "Web starting on http://localhost:$PORT"
exec npm run dev -- --port "$PORT"
