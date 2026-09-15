#!/usr/bin/env bash
# Runs the full end-to-end check against a running API. Prints 54 assertions.
set -euo pipefail
cd "$(dirname "$0")/apps/api"
PYTHONPATH=. exec ./.venv/bin/python tests/e2e_verify.py
