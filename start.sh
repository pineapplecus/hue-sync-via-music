#!/usr/bin/env bash
# Start the light show. Any extra arguments go to `sync.py run` (e.g. ./start.sh --preset noe-default).
cd "$(dirname "$0")"
[[ -x .venv/bin/python ]] || { echo "Run ./setup.sh first."; exit 1; }
exec .venv/bin/python sync.py run "$@"
