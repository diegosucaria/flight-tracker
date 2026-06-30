"""Live receiver location from gpsd (the VK-162 USB GPS).

Connects to the gpsd daemon (TCP 2947, run by the ``gpsd`` container) and streams TPV
position reports. The app uses this as the receiver position when a fix is available AND
``cfg.use_gps`` is on; otherwise it falls back to the configured lat/lon. It no-ops cleanly
when gpsd isn't running or no GPS is plugged in (open_connection just keeps retrying), so it
is safe to ship now and "just works" the moment the GPS + gpsd container are present.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time

GPSD_HOST = os.environ.get("GPSD_HOST", "gpsd")
GPSD_PORT = int(os.environ.get("GPSD_PORT", "2947"))
FIX_TTL_S = float(os.environ.get("GPS_FIX_TTL_S", "30"))   # a fix older than this is stale
_RECONNECT_S = 5.0


class GpsReader:
    """Background gpsd client. Holds the latest fix and reconnects on any failure."""

    def __init__(self) -> None:
        self._lat: float | None = None
        self._lon: float | None = None
        self._mode = 0            # 0/1 = no fix, 2 = 2D fix, 3 = 3D fix
        self._sats: int | None = None
        self._updated = 0.0       # monotonic time of the last usable TPV

    @property
    def fix(self) -> bool:
        return (self._mode >= 2 and self._lat is not None
                and (time.monotonic() - self._updated) < FIX_TTL_S)

    def position(self) -> tuple[float, float] | None:
        """(lat, lon) of the current live fix, or None when there isn't a fresh one."""
        return (self._lat, self._lon) if self.fix else None

    def status(self) -> dict:
        """Shape for /ws + /api/diag: fix bool, mode (2D/3D), used-sat count, last lat/lon."""
        return {"fix": self.fix, "mode": self._mode, "sats": self._sats,
                "lat": self._lat, "lon": self._lon}

    async def run(self) -> None:
        """Stream TPV/SKY reports from gpsd forever, reconnecting on drop. Never raises."""
        while True:
            writer = None
            try:
                reader, writer = await asyncio.open_connection(GPSD_HOST, GPSD_PORT)
                writer.write(b'?WATCH={"enable":true,"json":true}\n')
                await writer.drain()
                async for line in reader:                 # yields one JSON object per line
                    self._consume(line)
            except (OSError, asyncio.IncompleteReadError, ValueError):
                pass                                       # gpsd down / GPS unplugged — retry
            finally:
                if writer is not None:
                    writer.close()
                    with contextlib.suppress(Exception):
                        await writer.wait_closed()
            await asyncio.sleep(_RECONNECT_S)

    def _consume(self, line: bytes) -> None:
        try:
            msg = json.loads(line)
        except (ValueError, TypeError):
            return
        cls = msg.get("class")
        if cls == "TPV":
            self._mode = int(msg.get("mode") or 0)
            if self._mode >= 2 and isinstance(msg.get("lat"), (int, float)):
                self._lat = float(msg["lat"])
                self._lon = float(msg["lon"])
                self._updated = time.monotonic()
        elif cls == "SKY":
            sats = msg.get("satellites")
            if isinstance(sats, list):
                self._sats = sum(1 for s in sats if isinstance(s, dict) and s.get("used"))
