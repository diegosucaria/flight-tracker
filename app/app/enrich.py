"""Enrich a flight with route (FROM›TO) + airline, and airframe (type/reg/operator),
from community databases.

Route/type are NOT in the ADS-B signal; we look them up by callsign / hex and cache
(they rarely change — airframe data is effectively static, so we cache it forever,
misses included). Coverage is good for airliners, patchy for GA.

Two lookups, both against adsbdb:
- route_for_callsign(callsign) -> origin/destination (+ their coords) + airline
- aircraft_info(hex)           -> type / type_desc / registration / operator / military

aircraft_info() is intended as a FALLBACK ONLY for fields missing from the local
aircraft.json (a DB-enabled readsb emits t/r/desc/ownOp/dbFlags locally; this device
does not, so we backfill from adsbdb).
"""
from __future__ import annotations

import httpx

_ROUTE_CACHE: dict[str, dict | None] = {}
_AIRCRAFT_CACHE: dict[str, dict | None] = {}
ADSBDB = "https://api.adsbdb.com/v0"


def _airport_code(node: dict) -> str | None:
    """Prefer IATA, fall back to ICAO — some routes have a null iata_code."""
    if not isinstance(node, dict):
        return None
    return node.get("iata_code") or node.get("icao_code")


async def route_for_callsign(callsign: str, client: httpx.AsyncClient) -> dict | None:
    """Return route + airline for a callsign, or ``None``.

    Shape::

        {'origin': 'JFK', 'destination': 'LAX', 'airline': 'Example Airlines',
         'origin_lat': 40.6413, 'origin_lon': -73.7781,
         'dest_lat': 33.9416,   'dest_lon': -118.4085}

    ``origin``/``destination`` are the IATA code (ICAO fallback). The lat/lon are
    pulled from the adsbdb flightroute so the caller can estimate flight duration.
    Result is cached per callsign (including misses).
    """
    cs = (callsign or "").strip().upper()
    if not cs:
        return None
    if cs in _ROUTE_CACHE:
        return _ROUTE_CACHE[cs]

    out: dict | None = None
    try:
        r = await client.get(f"{ADSBDB}/callsign/{cs}", timeout=5)
        if r.status_code == 200:
            fr = r.json()["response"]["flightroute"]
            origin = fr.get("origin") or {}
            dest = fr.get("destination") or {}
            out = {
                "origin": _airport_code(origin),
                "destination": _airport_code(dest),
                "airline": (fr.get("airline") or {}).get("name"),
                # coords (may be None) → used for the duration estimate
                "origin_lat": origin.get("latitude"),
                "origin_lon": origin.get("longitude"),
                "dest_lat": dest.get("latitude"),
                "dest_lon": dest.get("longitude"),
            }
    except (httpx.HTTPError, KeyError, ValueError):
        out = None

    _ROUTE_CACHE[cs] = out
    return out


async def aircraft_info(hex_id: str, client: httpx.AsyncClient) -> dict | None:
    """Return airframe info for a Mode-S hex, or ``None``.

    Shape::

        {'type': 'B788', 'type_desc': '787 8', 'registration': 'N788EX',
         'operator': 'Example Airlines', 'military': False}

    Used ONLY as a fallback for fields not present locally in aircraft.json.
    adsbdb has no military flag, so ``military`` is always ``False`` here — the
    authoritative military bit comes from the local ``dbFlags`` when available.
    Result is cached per hex (including misses) — airframe data is static.
    """
    hx = (hex_id or "").strip().lower()
    if not hx:
        return None
    if hx in _AIRCRAFT_CACHE:
        return _AIRCRAFT_CACHE[hx]

    out: dict | None = None
    try:
        r = await client.get(f"{ADSBDB}/aircraft/{hx}", timeout=5)
        if r.status_code == 200:
            a = r.json()["response"]["aircraft"]
            out = {
                "type": a.get("icao_type"),
                "type_desc": a.get("type"),
                "registration": a.get("registration"),
                "operator": a.get("registered_owner"),
                "military": False,   # adsbdb provides no mil flag
            }
    except (httpx.HTTPError, KeyError, ValueError):
        out = None

    _AIRCRAFT_CACHE[hx] = out
    return out
