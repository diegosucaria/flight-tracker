#!/bin/sh
# Tower-audio speaker: stream the airband Icecast feed to the USB sound card, robustly.
#
#  * The cheap USB audio card intermittently fails its set-config during boot enumeration
#    ("usb 1-x: can't set config #1, error -71" — a USB signal-level fault): it's on the bus
#    (lsusb) but has NO ALSA card. We recover it with a full driver UNBIND/REBIND, which forces a
#    fresh enumeration + set-config — stronger than toggling /authorized (which couldn't clear the
#    -71 and, worse, de-authorized a card that was actually up). Retried with backoff.
#      NOTE: -71 is a PHYSICAL-layer error. If it keeps failing across resets, the real fix is the
#      USB cable / port / dongle (re-seat, move off the SDR's bus, or replace) — not more software.
#  * balena forbids bind-mounting /dev, so `devices: /dev/snd` is a STATIC snapshot from container-
#    create time: a card that enumerates later exists on the host but has no node in here. We
#    mknod the missing control+playback nodes ourselves once ALSA has the card (privileged, so the
#    device cgroup permits it — verified: mknod'd nodes play fine).
#  * Frees the EXCLUSIVE ALSA device before each (re)launch so a lingering mpv can't fail it busy.
STREAM="http://airband:8000/atc.mp3"
ALSA_DEV="alsa/plughw:CARD=Audio,DEV=0"

usb_card_index() {   # echo the ALSA index of the USB card (id "Audio"); non-zero if it isn't present
  for c in /sys/class/sound/card*; do
    [ "$(cat "$c/id" 2>/dev/null)" = "Audio" ] && { echo "${c##*/card}"; return 0; }
  done
  return 1
}

ensure_card_nodes() {   # mknod the USB card's nodes into our static /dev/snd if ALSA has them but we don't
  for node in "controlC$1" "pcmC${1}D0p"; do
    [ -e "/dev/snd/$node" ] && continue
    mm=$(cat "/sys/class/sound/$node/dev" 2>/dev/null) || continue     # "major:minor" from ALSA
    mknod "/dev/snd/$node" c "${mm%%:*}" "${mm##*:}" 2>/dev/null && echo "[speaker] created /dev/snd/$node ($mm)"
  done
}

card_ready() {   # USB card present in ALSA AND its device nodes exist in this container
  n=$(usb_card_index) || return 1
  ensure_card_nodes "$n"
  [ -e "/dev/snd/controlC$n" ] && [ -e "/dev/snd/pcmC${n}D0p" ]
}

usb_audio_devid() {   # echo the USB device id (e.g. "1-2") of the audio dongle, if it's on the bus
  for d in /sys/bus/usb/devices/*/; do
    id=${d%/}; id=${id##*/}
    case "$id" in *:*) continue ;; esac                       # skip interface dirs (1-2:1.0 …)
    grep -qi "audio" "$d/product" 2>/dev/null && { echo "$id"; return 0; }
  done
  return 1
}

reset_usb_audio() {   # full driver unbind/rebind → fresh enumeration + set-config (the -71 recovery)
  usb_card_index >/dev/null 2>&1 && return 0                   # never reset a card that IS up
  id=$(usb_audio_devid) || { echo "[speaker] USB audio not on bus"; return 1; }
  echo "[speaker] recovering USB audio ($id): driver unbind/rebind"
  echo "$id" > /sys/bus/usb/drivers/usb/unbind 2>/dev/null; sleep 2
  echo "$id" > /sys/bus/usb/drivers/usb/bind   2>/dev/null; sleep 4
}

/usr/local/bin/volume-poll.sh &        # keep the USB-card volume in sync with the UI slider
beeped=0; fails=0; last_n=""
while true; do
  if card_ready; then
    fails=0; last_n=$(usb_card_index)
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
    # card gone — drop any stale nodes we mknod'd so a fresh enumeration gets the right minors
    [ -n "$last_n" ] && { rm -f "/dev/snd/controlC$last_n" "/dev/snd/pcmC${last_n}D0p" 2>/dev/null; last_n=""; }
    fails=$((fails + 1))
    echo "[speaker] USB sound card not enumerated (try $fails) — recovering"
    reset_usb_audio
    # Back off after repeated failures: a persistently-failing set-config is a hardware link
    # problem, and hammering the bus every few seconds only adds enumeration noise.
    if [ "$fails" -ge 5 ]; then sleep 30; else sleep 6; fi
  fi
done
