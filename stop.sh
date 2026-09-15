#!/usr/bin/env bash
#
# Stops both Assarium processes. Safe to run when nothing is running.
set -uo pipefail

for entry in "API:${ASSARIUM_API_PORT:-8000}" "Web:${ASSARIUM_WEB_PORT:-3000}"; do
  name="${entry%%:*}"; port="${entry##*:}"
  pid="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | head -1)"
  if [ -n "$pid" ]; then
    echo "Stopping $name on port $port (pid $pid)..."
    kill "$pid" 2>/dev/null
  else
    echo "$name: nothing running on port $port."
  fi
done
