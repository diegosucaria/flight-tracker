"""Airband (VHF tower audio) status helper.

The `airband` container runs rtl_airband (SCAN over the configured tower/approach/ground
frequencies) feeding its own bundled Icecast server. The browser plays the MP3
stream directly from that container's published port 8000; this module just lets the
web UI show a live/offline dot + the currently-active scan frequency by scraping
Icecast's status JSON from inside the compose network.

`online` here means "rtl_airband is connected to Icecast and producing the stream"
(i.e. the receiver chain is up) — not necessarily "a controller is speaking right
now". The scan-frequency tag (Icecast stream title) is a decent proxy for activity.
"""
from __future__ import annotations

import os
import tempfile

import httpx

from .watchdog import restart_service

# Internal (compose-network) URL the app uses to read Icecast status.
ICECAST_BASE = os.environ.get("AIRBAND_ICECAST_BASE", "http://airband:8000")
MOUNT = os.environ.get("AIRBAND_MOUNT", "atc.mp3")
# Public port the browser hits (same host as the UI, the airband container's 8000).
STREAM_PORT = int(os.environ.get("AIRBAND_STREAM_PORT", "8000"))
ENABLED = os.environ.get("AIRBAND_ENABLED", "1") not in ("0", "false", "False", "")
# Where the app writes the rtl_airband custom config. The airband container mounts the
# same shared volume at /run/rtlsdr-airband (the image's CUSTOMCONFIG path).
AIRBAND_CONFIG_PATH = os.environ.get("AIRBAND_CONFIG_PATH", "/airband-config/rtl_airband.conf")
AIRBAND_SERVICE = os.environ.get("AIRBAND_SERVICE", "airband")
AIRBAND_SPEAKER_SERVICE = os.environ.get("AIRBAND_SPEAKER_SERVICE", "airband-speaker")
# The speaker container polls this file (on the shared airband_config volume) every ~2s and
# applies it as the USB sound-card mixer level — so volume changes are live without a restart.
AIRBAND_VOLUME_PATH = os.environ.get("AIRBAND_VOLUME_PATH", "/airband-config/volume")


def write_volume(vol: int, path: str = AIRBAND_VOLUME_PATH) -> None:
    """Write the USB-soundcard volume (0-100) for the speaker to pick up. Never raises."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(str(max(0, min(100, int(vol)))))
    except (OSError, TypeError, ValueError) as exc:
        print(f"[airband] volume write failed: {exc}")


async def airband_status(client: httpx.AsyncClient) -> dict:
    """Return airband stream status for the web UI.

    Shape::

        {"enabled": True, "online": True, "mount": "atc.mp3",
         "port": 8000, "title": "119.100", "listeners": 0}

    ``online`` is False (with ``enabled`` still True) when the airband container is
    deploying, the RTL-SDR is unplugged, or Icecast has no source yet. The UI hides
    the whole card when ``enabled`` is False.
    """
    if not ENABLED:
        return {"enabled": False}

    out: dict = {"enabled": True, "online": False, "mount": MOUNT, "port": STREAM_PORT}
    try:
        r = await client.get(f"{ICECAST_BASE}/status-json.xsl", timeout=3)
        if r.status_code == 200:
            stats = r.json().get("icestats", {}) or {}
            sources = stats.get("source", [])
            if isinstance(sources, dict):          # Icecast emits a bare object for 1 source
                sources = [sources]
            for s in sources:
                listenurl = str(s.get("listenurl", ""))
                if listenurl.endswith(MOUNT) or not sources:
                    out["online"] = True
                    out["title"] = s.get("title") or s.get("server_name")
                    out["listeners"] = s.get("listeners")
                    out["bitrate"] = s.get("bitrate") or s.get("ice-bitrate")
                    break
    except (httpx.HTTPError, ValueError):
        pass   # container not up yet / no source — reported as online: False
    return out


def write_airband_conf(text: str, path: str = AIRBAND_CONFIG_PATH) -> None:
    """Atomically write the rtl_airband config to the shared volume.

    Temp-file + os.replace so the airband container never reads a half-written file when
    it restarts (mirrors Config.save()).
    """
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


async def apply_airband_config(cfg, client: httpx.AsyncClient) -> dict:
    """Render + write the config, then restart the airband service to reload it.

    rtl_airband has no live reload (SIGHUP shuts it down), so a service restart IS the
    reload. The bundled Icecast + the airband-speaker reconnect on their own afterwards.
    """
    write_airband_conf(cfg.render_airband_conf())
    restarted, msg = await restart_service(AIRBAND_SERVICE, client)
    return {"ok": True, "restarted": restarted, "detail": msg}


async def test_beep(client: httpx.AsyncClient) -> dict:
    """Restart the speaker container so it replays its startup test tone."""
    restarted, msg = await restart_service(AIRBAND_SPEAKER_SERVICE, client)
    return {"ok": True, "restarted": restarted, "detail": msg}
