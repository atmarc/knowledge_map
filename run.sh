#!/usr/bin/env bash
# Start Knowledge Map at http://127.0.0.1:8765
cd "$(dirname "$0")"
exec uv run uvicorn app.main:app --host 127.0.0.1 --port "${PORT:-8765}" "$@"
