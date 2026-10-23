"""Switch the airband SDR and the USB GPS off (and back on) from the web UI.

"Off" stops the services that use the device, via the balena supervisor, so the dongle
sits idle. The Pi 5 can't cut power to a single USB port, so the device stays powered;
unplug it to fully isolate it. The supervisor starts every service again on a reboot or
a new release, so ``enforce_loop`` re-stops the ones switched off in the config.
"""
from __future__ import annotations

import asyncio

import httpx

from .airband import AIRBAND_SERVICE, AIRBAND_SPEAKER_SERVICE
from .watchdog import service_action

GPS_SERVICE = "gpsd"
# Device -> (config flag, services in start order). Stop runs in reverse order.
DEVICES = {
    "sdr": ("sdr_enabled", (AIRBAND_SERVICE, AIRBAND_SPEAKER_SERVICE)),
    "gps": ("gps_enabled", (GPS_SERVICE,)),
}
ENFORCE_EVERY_S = 300.0


def states(cfg) -> dict:
    """{"sdr": bool, "gps": bool} — True = on."""
    return {name: bool(getattr(cfg, flag)) for name, (flag, _) in DEVICES.items()}


async def apply(name: str, on: bool, client: httpx.AsyncClient) -> tuple[bool, str]:
    """Start (on) or stop (off) one device's services. Returns ``(attempted, message)``."""
    _, services = DEVICES[name]
    action = "start" if on else "stop"
    attempted, msgs = False, []
    for svc in (services if on else reversed(services)):
        attempted, msg = await service_action(action, svc, client)
        msgs.append(f"{svc}: {msg}")
        if not attempted:
            break
    return attempted, "; ".join(msgs)


async def enforce_loop(cfg) -> None:
    """Keep switched-off devices stopped after a reboot or release. Never raises."""
    await asyncio.sleep(10)               # let the supervisor finish starting the stack
    async with httpx.AsyncClient() as client:
        while True:
            for name, on in states(cfg).items():
                if not on:
                    try:
                        attempted, msg = await apply(name, False, client)
                    except Exception as exc:  # noqa: BLE001 — keep the loop alive
                        attempted, msg = True, f"error: {exc}"
                    if not attempted:
                        return            # not on balena: nothing to enforce
                    if "error" in msg or "HTTP 2" not in msg:
                        print(f"[devices] keeping {name} off: {msg}")
            await asyncio.sleep(ENFORCE_EVERY_S)
