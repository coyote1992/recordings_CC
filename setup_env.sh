#!/usr/bin/env bash
# Prepare a fresh cloud container for record_batch.py (idempotent).
#   - Playwright python package (Google Chrome (H.264-capable Chromium) is installed via playwright)
#   - ffmpeg, Xvfb, PulseAudio
#   - virtual display :99 (2000x1400, viewport cropped to 1920x1080 by ffmpeg) and a null audio sink "rec" for capturing sound
set -euo pipefail

python3 -c "import playwright" 2>/dev/null || pip install -q playwright
# Wistia serves H.264/AAC, which stock Chromium builds cannot decode -> use Chrome (Chromium-based)
[ -x /opt/google/chrome/chrome ] || python3 -m playwright install chrome
command -v ffmpeg >/dev/null || apt-get install -y ffmpeg
command -v Xvfb >/dev/null || apt-get install -y xvfb
if ! command -v pulseaudio >/dev/null; then
  apt-get update -q && apt-get install -y pulseaudio pulseaudio-utils
fi

if ! pgrep -x Xvfb >/dev/null; then
  setsid nohup Xvfb :99 -screen 0 2000x1400x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
  sleep 1
fi

if ! pgrep -x pulseaudio >/dev/null; then
  cat >/tmp/pa.pa <<'EOF'
load-module module-native-protocol-unix auth-anonymous=1 socket=/tmp/pulse.sock
load-module module-null-sink sink_name=rec sink_properties=device.description=rec
set-default-sink rec
EOF
  mkdir -p /var/run/pulse
  pulseaudio --system --disallow-exit --exit-idle-time=-1 -n -F /tmp/pa.pa -D
  sleep 1
fi

echo "ready: DISPLAY=:99 PULSE_SERVER=unix:/tmp/pulse.sock"
