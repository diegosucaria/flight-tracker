#!/bin/sh
# Tower-audio speaker: stream the airband Icecast feed to the USB sound card, robustly.
#
#  * The cheap USB audio card intermittently fails its set-config during boot enumeration
#    ("usb 1-x: can't set config #1, error -71" — a USB signal-level fault), so it sits on the
#    bus (lsusb) with NO ALSA card. We recover it with a full driver UNBIND/REBIND, which forces
#    a fresh enumeration + set-config. That is a stronger reset than toggling /authorized — which
#    could not clear the -71 here and, worse, de-authorized a card that was actually working.
#      NOTE: -71 is a PHYSICAL-layer error. If it keeps failing across resets, the fix is the USB
#      cable / port / dongle (re-seat, move it off the SDR's bus, or replace) — not more software.
#  * /dev/snd is BIND-mounted (see docker-compose), so a card that enumerates AFTER this container
#    started becomes visible here immediately; a plain `--device /dev/snd` snapshot never would.
#  * Frees the EXCLUSIVE ALSA device (plughw, no dmix) before each (re)launch, so a lingering mpv
#    can't make the next one fail busy.
STREAM="http://airband:8000/atc.mp3"
ALSA_DEV="alsa/plughw:CARD=Audio,DEV=0"

card_up()      { aplay -l 2>/dev/null | grep -qi "USB Audio"; }          # usable from here (needs the node)
card_on_host() { grep -qi "USB-Audio" /proc/asound/cards 2>/dev/null; }   # ALSA has it (host-wide, not namespaced)

usb_audio_devid() {   # echo the USB device id (e.g. "1-2") of the audio dongle if it's on the bus
  for d in /sys/bus/usb/devices/*/; do
    id=${d%/}; id=${id##*/}
    case "$id" in *:*) continue ;; esac                       # skip interface dirs (1-2:1.0 …)
    grep -qi "audio" "$d/product" 2>/dev/null && { echo "$id"; return 0; }
  done
  return 1
}

reset_usb_audio() {   # full driver unbind/rebind → fresh enumeration + set-config (the -71 recovery)
  card_on_host && return 0                                     # never reset a card that's actually up
  id=$(usb_audio_devid) || { echo "[speaker] USB audio not found on bus"; return 1; }
  echo "[speaker] recovering USB audio ($id): driver unbind/rebind"
  echo "$id" > /sys/bus/usb/drivers/usb/unbind 2>/dev/null; sleep 2
  echo "$id" > /sys/bus/usb/drivers/usb/bind   2>/dev/null; sleep 4
}

/usr/local/bin/volume-poll.sh &        # keep the USB-card volume in sync with the UI slider
beeped=0
fails=0
while true; do
  if card_up; then
    fails=0
    if [ "$beeped" = 0 ]; then
      echo "[speaker] card up — beep"
      timeout 0.5 speaker-test -D plughw:CARD=Audio -c 1 -t sine -f 880 >/dev/null 2>&1   # ~0.5s
      beeped=1
    fi
    pkill mpv 2>/dev/null; sleep 1      # ensure the exclusive ALSA device is free before launch
    echo "[speaker] mpv start"
    mpv --no-video --no-terminal --really-quiet --ao=alsa --audio-device="$ALSA_DEV" \
        --cache=yes --cache-secs=2 --network-timeout=30 \
        --stream-lavf-o-append=reconnect=1 --stream-lavf-o-append=reconnect_streamed=1 \
        --stream-lavf-o-append=reconnect_delay_max=10 --loop=inf "$STREAM"
    echo "[speaker] mpv exited; retry 3s"; sleep 3
  else
    fails=$((fails + 1))
    echo "[speaker] USB sound card not enumerated (try $fails) — recovering"
    reset_usb_audio
    # Back off after repeated failures: a persistently-failing set-config is a hardware link
    # problem, and hammering the bus every few seconds only adds enumeration noise.
    if [ "$fails" -ge 5 ]; then sleep 30; else sleep 6; fi
  fi
done
