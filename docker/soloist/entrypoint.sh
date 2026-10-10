#!/bin/sh
set -eu
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"
pulseaudio -n --file=/etc/pulse/stacy.pa --daemonize=yes --exit-idle-time=-1
soloist --device-name "${SOLOIST_DEVICE_NAME:-Stacy}" \
  --api-key "$SOLOIST_API_KEY" --data-dir /home/stacy/.local/share/soloist \
  --ws 0.0.0.0:9090 "$@" &
soloist_pid=$!
python3 /usr/local/bin/pairing_proxy.py &
proxy_pid=$!
trap 'kill "$soloist_pid" "$proxy_pid" 2>/dev/null || true' EXIT
trap 'exit 0' TERM INT
wait "$soloist_pid"
