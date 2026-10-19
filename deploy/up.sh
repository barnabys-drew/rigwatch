#!/usr/bin/env bash
# Bring rigwatch up, idempotently, and print where to find it.
#
#   deploy/up.sh           install/start the user unit and expose it to the tailnet
#   deploy/up.sh --open    ...and open it in the Windows default browser
#
# Safe to run any number of times. Never touches Tailscale Funnel: the page lists
# every program running on this PC and must stay tailnet-only.
set -euo pipefail

PORT=8775
TS_PORT="${RIGWATCH_TS_PORT:-10002}"   # tailnet https port; override if taken
LOCAL="http://127.0.0.1:${PORT}"
TS="/mnt/c/Program Files/Tailscale/tailscale.exe"
UNIT=rigwatch.service
HERE="$(cd "$(dirname "$0")" && pwd)"

say() { printf '%s\n' "$*"; }

# 1. the local server (systemd user service, loopback only)
mkdir -p "$HOME/.config/systemd/user"
if ! cmp -s "$HERE/$UNIT" "$HOME/.config/systemd/user/$UNIT"; then
  cp "$HERE/$UNIT" "$HOME/.config/systemd/user/$UNIT"
  systemctl --user daemon-reload
  systemctl --user enable "$UNIT" >/dev/null 2>&1
  systemctl --user restart "$UNIT"
  say "installed $UNIT"
elif ! systemctl --user is-active --quiet "$UNIT"; then
  systemctl --user start "$UNIT"
  say "started $UNIT"
fi
for _ in $(seq 1 15); do
  curl -fsS -o /dev/null "$LOCAL/healthz" && break
  sleep 1
done
curl -fsS -o /dev/null "$LOCAL/healthz" || { say "server did not answer on $LOCAL (journalctl --user -u $UNIT)" >&2; exit 1; }
say "local    $LOCAL/"

# 2. the tailnet exposure
if [ ! -x "$TS" ]; then
  say "tailnet  Tailscale not found at: $TS (local URL only)"
  exit 0
fi
status="$(timeout 15 "$TS" serve status 2>&1 | tr -d '\r' || true)"
if ! printf '%s' "$status" | grep -q "127.0.0.1:${PORT}"; then
  if timeout 15 "$TS" status 2>&1 | grep -qi 'logged out'; then
    say "tailnet  Tailscale is logged out. Run: tailscale.exe up   (local URL only for now)"
    exit 0
  fi
  timeout 20 "$TS" serve --bg --https="$TS_PORT" "$PORT" >/dev/null 2>&1 || true
  status="$(timeout 15 "$TS" serve status 2>&1 | tr -d '\r' || true)"
fi
url="$(printf '%s' "$status" | grep -o "https://[^ ]*ts\.net:${TS_PORT}" | head -1 || true)"
if [ -n "$url" ]; then
  say "tailnet  ${url}/   (tailnet only)"
else
  say "tailnet  not exposed yet: $(printf '%s' "$status" | head -1)"
fi

# 3. optionally open it
if [ "${1:-}" = "--open" ]; then
  /mnt/c/Windows/explorer.exe "${url:-$LOCAL}/" >/dev/null 2>&1 || true
fi
