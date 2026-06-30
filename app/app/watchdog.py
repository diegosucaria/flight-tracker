"""Feed watchdog.

If readsb's cumulative message counter stops advancing for a while, the SDR feed has
almost certainly died (USB hot-plug, glitch, decoder crash) — there's essentially always
*some* Mode-S in the air, so a flat counter means no signal is reaching us. We then restart
the `airspy` service via the balena supervisor API so it re-grabs the device, with no manual
intervention.

No-ops off balena (supervisor env vars absent), so local dev is unaffected.
"""
from __future__ import annotations

import os
import time

import httpx

SUPERVISOR = os.environ.get("BALENA_SUPERVISOR_ADDRESS", "")
API_KEY = os.environ.get("BALENA_SUPERVISOR_API_KEY", "")
APP_ID = os.environ.get("BALENA_APP_ID", "")
STALL_SECONDS = float(os.environ.get("WATCHDOG_STALL_SECONDS", "300"))
COOLDOWN_SECONDS = float(os.environ.get("WATCHDOG_COOLDOWN_SECONDS", "180"))
SERVICE = os.environ.get("WATCHDOG_SERVICE", "airspy")


async def restart_service(name: str, client: httpx.AsyncClient) -> tuple[bool, str]:
    """Restart a compose service via the balena supervisor. No-op off balena.

    Returns ``(attempted, message)``. ``attempted`` is False when the supervisor env is
    absent (local dev) so callers can report 'not on balena' rather than an error.
    """
    if not (SUPERVISOR and API_KEY and APP_ID):
        return (False, "supervisor api unavailable (local dev)")
    url = f"{SUPERVISOR}/v2/applications/{APP_ID}/restart-service?apikey={API_KEY}"
    try:
        r = await client.post(url, json={"serviceName": name}, timeout=10)
        return (True, f"HTTP {r.status_code}")
    except Exception as exc:  # noqa: BLE001
        return (True, f"error: {exc}")


class FeedWatchdog:
    def __init__(self) -> None:
        self._last_messages: int | None = None
        self._last_change = time.monotonic()
        self._last_restart = 0.0

    async def check(self, messages: int | None, client: httpx.AsyncClient) -> None:
        if not (SUPERVISOR and API_KEY and APP_ID) or messages is None:
            return  # not on balena / supervisor API not enabled / no data field
        now = time.monotonic()
        if messages != self._last_messages:
            self._last_messages = messages
            self._last_change = now
            return
        if now - self._last_change < STALL_SECONDS:
            return
        if now - self._last_restart < COOLDOWN_SECONDS:
            return
        self._last_restart = now
        self._last_change = now  # give the restart time to take effect
        await self._restart(client)

    async def _restart(self, client: httpx.AsyncClient) -> None:
        _, msg = await restart_service(SERVICE, client)
        verb = "restart FAILED for" if msg.startswith("error") else "restarted"
        print(f"[watchdog] feed flat {STALL_SECONDS:.0f}s - {verb} '{SERVICE}': {msg}")
