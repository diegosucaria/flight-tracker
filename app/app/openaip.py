"""Airspaces near the home airport from OpenAIP (keyed) — for the map's Airspace layer.

Needs a free OpenAIP API key in ``OPENAIP_API_KEY``. Airspaces are static, so the result is
cached for hours. Never raises. OpenAIP geometry is GeoJSON ([lon, lat]); we flip each ring to
Leaflet's [lat, lon].
"""
from __future__ import annotations

import os
import time

_URL = "https://api.core.openaip.net/api/airspaces"
_KEY_ENV = "OPENAIP_API_KEY"
_TTL_S = 6 * 3600            # airspaces barely change — refresh a few times a day at most
_RADIUS = 1.5               # deg half-box around the airport
_cache: dict = {"t": 0.0, "icao": None, "data": None}

_ICAO_CLASS = {0: "A", 1: "B", 2: "C", 3: "D", 4: "E", 5: "F", 6: "G"}
_REF = {0: "GND", 1: "MSL", 2: "STD"}


def _limit_str(lim) -> str | None:
    if not isinstance(lim, dict):
        return None
    v = lim.get("value")
    if v is None:
        return None
    if lim.get("unit") == 6:                      # flight level
        return f"FL{v}"
    ref = _REF.get(lim.get("referenceDatum"), "")
    if v == 0 and ref in ("GND", ""):
        return "GND"
    return f"{v}ft{(' ' + ref) if ref else ''}"


def _restrictive(name: str, tcode) -> bool:
    n = (name or "").upper()
    return tcode in (1, 2, 3) or any(k in n for k in ("RESTRICT", "DANGER", "PROHIB"))


def _clean(items) -> list[dict]:
    out = []
    for a in items or []:
        g = a.get("geometry") or {}
        coords = g.get("coordinates")
        if g.get("type") != "Polygon" or not coords:
            continue
        try:
            ring = [[float(pt[1]), float(pt[0])] for pt in coords[0] if len(pt) >= 2]
        except (TypeError, ValueError, IndexError):
            continue
        if len(ring) < 3:
            continue
        name = a.get("name") or ""
        out.append({
            "name": name,
            "class": _ICAO_CLASS.get(a.get("icaoClass")),
            "lower": _limit_str(a.get("lowerLimit")),
            "upper": _limit_str(a.get("upperLimit")),
            "restrictive": _restrictive(name, a.get("type")),
            "ring": ring,
        })
    return out


async def get_airspaces(icao: str, lat: float, lon: float, client) -> dict | None:
    """{icao, airspaces:[{name, class, lower, upper, restrictive, ring}]} or None. Never raises."""
    key = os.environ.get(_KEY_ENV)
    if not key or not lat or not lon:
        return None
    icao = (icao or "").strip().upper()

    now = time.monotonic()
    if _cache["data"] and _cache["icao"] == icao and now - _cache["t"] < _TTL_S:
        return _cache["data"]

    bbox = f"{lon - _RADIUS},{lat - _RADIUS},{lon + _RADIUS},{lat + _RADIUS}"   # SW lon,lat, NE lon,lat
    try:
        r = await client.get(_URL, params={"bbox": bbox, "limit": 300},
                             headers={"x-openaip-api-key": key}, timeout=25)
        r.raise_for_status()
        j = r.json()
    except Exception:
        return _cache["data"]                     # keep last-good on a transient failure

    items = j.get("items") if isinstance(j, dict) else (j if isinstance(j, list) else [])
    data = {"icao": icao, "airspaces": _clean(items)}
    if data["airspaces"] or _cache["icao"] != icao:
        _cache.update(t=now, icao=icao, data=data)
    return _cache["data"]
