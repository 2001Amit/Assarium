#!/usr/bin/env bash
#
# Starts the Assarium API on port 8000.
#
# Uses the project's own virtualenv directly rather than going through Poetry, so a
# broken or missing Poetry on the host never blocks the app. Poetry breaks itself
# whenever the Python it was installed against moves, which is common enough that it
# should not sit between you and running the product.
#
#   ./start-api.sh              start, or report what already holds the port
#   ./start-api.sh --restart    stop whatever is on the port first, then start
#
set -euo pipefail
cd "$(dirname "$0")/apps/api"

PORT="${ASSARIUM_API_PORT:-8000}"
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
  # If it is already this app, say so rather than failing with a bind error the user
  # then has to interpret.
  if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/health" 2>/dev/null | grep -q '"status"'; then
    if [ "$RESTART" -eq 0 ]; then
      echo "The Assarium API is already running on port $PORT (pid $PID)."
      echo "  Open        http://localhost:3000"
      echo "  Restart it  ./start-api.sh --restart"
      exit 0
    fi
    echo "Stopping the running API (pid $PID)..."
  else
    NAME="$(ps -p "$PID" -o comm= 2>/dev/null || echo 'unknown process')"
    if [ "$RESTART" -eq 0 ]; then
      echo "Port $PORT is held by something that is not Assarium: $NAME (pid $PID)."
      echo "  Free it     ./start-api.sh --restart"
      echo "  Or use      ASSARIUM_API_PORT=8001 ./start-api.sh"
      exit 1
    fi
    echo "Stopping $NAME (pid $PID) on port $PORT..."
  fi
  kill "$PID" 2>/dev/null || true
  for _ in $(seq 1 20); do [ -z "$(holder || true)" ] && break; sleep 0.25; done
  [ -n "$(holder || true)" ] && { echo "Could not free port $PORT."; exit 1; }
fi

if [ ! -x ".venv/bin/uvicorn" ]; then
  echo "No virtualenv found. Creating one and installing dependencies..."
  python3 -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet \
    "fastapi>=0.115" "uvicorn[standard]" "pydantic>=2.9" "pydantic-settings>=2.6" \
    "sqlalchemy>=2.0.36" "cryptography>=43" "duckdb>=1.1" "pyarrow>=18" "pandas>=2.2" \
    "httpx>=0.27" python-multipart "sqlglot>=25" "openai>=2.0" pytz openpyxl \
    "psycopg[binary]" pymysql
  echo "Done."
fi

echo "API starting on http://127.0.0.1:$PORT"
exec ./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$PORT" "$@"
