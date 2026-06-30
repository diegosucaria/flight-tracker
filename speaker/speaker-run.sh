#!/bin/sh
# Tower-audio speaker: stream the airband Icecast feed to the USB sound card, robustly.
#
#  * Self-heals the intermittent boot "usb 1-x: can't set config #1, error -71": the cheap USB
#    audio card sometimes fails its FIRST set-config during the boot enumeration storm and the
#    kernel never retries, so it's on the bus (lsusb) but has no ALSA card. If we see that, we
#    re-authorize the device to force a fresh set-config (needs a privileged container for the
#    /sys write — see docker-compose).
#  * Frees the EXCLUSIVE ALSA device (plughw, no dmix) before each (re)launch, so a previous
#    mpv that hasn't fully released it can't make the next one fail busy → the start/exit churn.
STREAM="http://airband:8000/atc.mp3"
ALSA_DEV="alsa/plughw:CARD=Audio,DEV=0"

card_up() { aplay -l 2>/dev/null | grep -qi "USB Audio"; }

reauth_usb_audio() {   # retry USB set-config for a stuck USB-audio device (the boot -71 workaround)
  for d in /sys/bus/usb/devices/*/; do
    if grep -qi "audio" "$d/product" 2>/dev/null && [ -w "$d/authorized" ]; then
      echo "[speaker] re-enumerating $(cat "$d/product" 2>/dev/null) (USB set-config retry)"
      echo 0 > "$d/authorized" 2>/dev/null; sleep 1; echo 1 > "$d/authorized" 2>/dev/null; sleep 2
    fi
  done
}

/usr/local/bin/volume-poll.sh &        # keep the USB-card volume in sync with the UI slider
beeped=0
while true; do
  if card_up; then
    if [ "$beeped" = 0 ]; then
      echo "[speaker] beep"
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
    echo "[speaker] USB sound card not enumerated — retrying USB set-config / waiting"
    reauth_usb_audio
    sleep 8
  fi
done
