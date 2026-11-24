#!/usr/bin/env bash
# Install Knowledge Map as a systemd user service, so it runs in the background from login.
set -euo pipefail
cd "$(dirname "$0")/.."
DIR=$(pwd)
PORT=${PORT:-8765}
UNIT=~/.config/systemd/user/knowledge-map.service

if [[ -e $UNIT && ${1:-} != --force ]]; then
  echo "$UNIT already exists; run with --force to overwrite it." >&2
  exit 1
fi

uv sync
# The service doesn't see your shell's PATH: include the folder of the claude CLI, if installed.
CLAUDE=$(command -v claude || true)
EXTRA=${CLAUDE:+$(dirname "$CLAUDE"):}

mkdir -p "$(dirname "$UNIT")"
cat > "$UNIT" <<UNIT
[Unit]
Description=Knowledge Map (local paper library)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$DIR
Environment=PATH=${EXTRA}/usr/local/bin:/usr/bin:/bin
ExecStart=$DIR/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port $PORT
Restart=on-failure

[Install]
WantedBy=default.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now knowledge-map
echo "Knowledge Map is running at http://127.0.0.1:$PORT"
[[ -z $CLAUDE ]] && echo "Note: the claude CLI wasn't found, so Claude analysis is unavailable until it is installed (then rerun with --force)."
echo "To start it at boot without logging in: loginctl enable-linger $USER"
