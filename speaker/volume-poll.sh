#!/bin/sh
# Apply the UI volume slider to the USB sound card. The app writes a 0-100 value to
# /airband-config/volume (shared volume); we poll it every 2s so changes are live with no
# restart / no audio drop. Safe before the card enumerates (amixer just fails quietly).
while true; do
  V=$(cat /airband-config/volume 2>/dev/null || echo 100)
  amixer -c Audio sset Headphone "${V}%" unmute >/dev/null 2>&1
  sleep 2
done
