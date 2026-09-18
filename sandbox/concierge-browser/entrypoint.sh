#!/usr/bin/env bash
# concierge-browser entrypoint: Xvfb + openbox + x11vnc + noVNC + headed
# Chromium on the given DISPLAY.  CONCIERGE_ACCEL branches the GL path:
#   igpu -> angle/vulkan (host must expose the DRM node, mounted at spawn)
#   cpu  -> swiftshader (public-repo default; zero GPU needed)
# The first positional arg, when present, is the start URL (e.g. the merchant
# domain the user is logging into).
set -euo pipefail

export DISPLAY="${DISPLAY:-:99}"
PROFILE_DIR="${PROFILE_DIR:-/tmp/profile}"
ACCEL="${CONCIERGE_ACCEL:-cpu}"
NOVNC_PORT="${NOVNC_PORT:-6080}"
CDP_PORT="${CDP_PORT:-9222}"
CDP_PROXY_PORT="${CDP_PROXY_PORT:-9223}"
VNC_PASSWORD="${VNC_PASSWORD:-}"
START_URL="${1:-}"

mkdir -p "$PROFILE_DIR"

# --- X server + window manager -------------------------------------------
Xvfb "$DISPLAY" -screen 0 1600x1000x24 -nolisten tcp &
XVFB_PID=$!
sleep 1
openbox &

# --- x11vnc + noVNC ------------------------------------------------------
VNC_PASSWD_FILE=/tmp/vnc-passwd
if [ -n "$VNC_PASSWORD" ]; then
    x11vnc -storepasswd "$VNC_PASSWORD" "$VNC_PASSWD_FILE"
else
    touch "$VNC_PASSWD_FILE"
fi
# Xvfb + Chromium can leave x11vnc's XDamage region empty even while the
# desktop is changing.  Disable damage tracking so the initial framebuffer
# and subsequent browser paints are always sent to noVNC.
x11vnc -display "$DISPLAY" -forever -shared -noxdamage -rfbauth "$VNC_PASSWD_FILE" -rfbport 5900 -quiet &
VNC_PID=$!

# noVNC web client on $NOVNC_PORT, proxying to the local VNC port.
websockify --web=/usr/share/novnc "$NOVNC_PORT" localhost:5900 &
WEBSOCKIFY_PID=$!

# --- Chromium flags by accel knob (2.6) ----------------------------------
CHROMIUM_FLAGS=(
    --user-data-dir="$PROFILE_DIR"
    --no-first-run
    --no-default-browser-check
    --disable-session-crashed-bubble
    --disable-features=Translate,MediaRouter
    --window-size=1400,900
    --noerrdialogs
    --disable-notifications
    --no-sandbox
    --disable-dev-shm-usage
    --remote-debugging-address=0.0.0.0
    --remote-debugging-port="${CDP_PORT:-9222}"
)
if [ "$ACCEL" = "igpu" ]; then
    CHROMIUM_FLAGS+=(--use-gl=angle --use-angle=vulkan)
else
    CHROMIUM_FLAGS+=(--disable-gpu --use-gl=swiftshader)
fi

# patchright's headed Chromium binary (patchright install chromium).
CHROME_BIN=$(find /root/.cache/ms-playwright -type f \( \
    -path '*chromium*/chrome-linux/chrome' -o \
    -path '*chromium*/chrome-linux64/chrome' \) | head -n1)
if [ -z "$CHROME_BIN" ]; then
    echo "concierge-browser: no patchright chromium found" >&2
    exit 1
fi

if [ -n "$START_URL" ]; then
    "$CHROME_BIN" "${CHROMIUM_FLAGS[@]}" --app="https://$START_URL" &
else
    "$CHROME_BIN" "${CHROMIUM_FLAGS[@]}" &
fi
CHROME_PID=$!

# Chromium deliberately binds its DevTools endpoint to loopback. Expose a
# container-only bridge on the private Docker network so the bot can attach
# to this exact headed browser without publishing CDP to the host/internet.
CDP_PUBLIC_HOST="$(hostname)" CDP_PORT="$CDP_PORT" CDP_PROXY_PORT="$CDP_PROXY_PORT" \
    python3 /cdp_proxy.py &
CDP_PROXY_PID=$!

echo "concierge-browser up: display=$DISPLAY accel=$ACCEL novnc=:$NOVNC_PORT profile=$PROFILE_DIR"

cleanup() {
    kill "$CHROME_PID" "$WEBSOCKIFY_PID" "$VNC_PID" "$CDP_PROXY_PID" 2>/dev/null || true
    kill "$XVFB_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

wait "$CHROME_PID" "$WEBSOCKIFY_PID" "$VNC_PID" "$XVFB_PID"
