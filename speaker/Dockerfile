# Minimal "tower audio" speaker container: plays the airband Icecast stream to the USB
# sound card. Baking mpv + alsa-utils in (instead of `apk add` on every container start)
# makes restarts fast and offline-safe — important because the "Test speaker" button and
# any reboot restart this container, and a no-network start would otherwise leave it with
# no mpv and no audio. The run command stays in docker-compose.yml.
FROM alpine:3.20
RUN apk add --no-cache mpv alsa-utils
COPY volume-poll.sh /usr/local/bin/volume-poll.sh
RUN chmod +x /usr/local/bin/volume-poll.sh
