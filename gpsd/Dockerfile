# Minimal gpsd for the VK-162 USB GPS. Self-built so it never depends on an external image
# (the ghcr.io/sdr-enthusiasts/docker-gpsd:latest image is now "denied" and broke every
# deploy). The run command lives in docker-compose.yml and waits for /dev/ttyACM0, so the
# container is safe whether or not the GPS is plugged in.
FROM alpine:3.20
RUN apk add --no-cache gpsd
