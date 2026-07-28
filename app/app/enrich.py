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

import time

import httpx

# Cache values are (result, retry_after) — retry_after None means the answer is CONFIRMED
# (a 200, or a 404 = "no route/airframe known") and cached forever. A transient failure
# (network error, 5xx, 429) caches None with a retry timestamp instead: without that, a
# 2-minute adsbdb outage used to permanently blank routes for every callsign seen during it.
_ROUTE_CACHE: dict[str, tuple[dict | None, float | None]] = {}
_AIRCRAFT_CACHE: dict[str, tuple[dict | None, float | None]] = {}
_MISS_RETRY_S = 600.0      # failed lookups become retryable after 10 min
_CACHE_MAX = 4000          # 24/7 device: clear rather than grow forever (repopulates cheaply)
ADSBDB = "https://api.adsbdb.com/v0"


def _cache_get(cache: dict, key: str):
    """(hit, value) — a failure entry counts as a hit only until its retry time."""
    entry = cache.get(key)
    if entry is None:
        return False, None
    value, retry_after = entry
    if retry_after is not None and time.monotonic() >= retry_after:
        return False, None
    return True, value


def _cache_put(cache: dict, key: str, value, confirmed: bool) -> None:
    if len(cache) >= _CACHE_MAX:
        cache.clear()
    cache[key] = (value, None if confirmed else time.monotonic() + _MISS_RETRY_S)


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
    hit, cached = _cache_get(_ROUTE_CACHE, cs)
    if hit:
        return cached

    out: dict | None = None
    confirmed = False
    try:
        r = await client.get(f"{ADSBDB}/callsign/{cs}", timeout=5)
        if r.status_code == 200:
            fr = r.json()["response"]["flightroute"]
            origin = fr.get("origin") or {}
            dest = fr.get("destination") or {}
            out = {
                "origin": _airport_code(origin),
                "destination": _airport_code(dest),
                # human name for the UI (city first, else the airport name)
                "origin_name": origin.get("municipality") or origin.get("name"),
                "destination_name": dest.get("municipality") or dest.get("name"),
                "airline": (fr.get("airline") or {}).get("name"),
                # IATA code of the airline (e.g. "AR" for callsign prefix "ARG") — used to
                # match the featured callsign against the airport schedule's flight numbers.
                "airline_iata": (fr.get("airline") or {}).get("iata"),
                # coords (may be None) → used for the duration estimate
                "origin_lat": origin.get("latitude"),
                "origin_lon": origin.get("longitude"),
                "dest_lat": dest.get("latitude"),
                "dest_lon": dest.get("longitude"),
            }
            confirmed = True
        elif r.status_code == 404:
            confirmed = True             # adsbdb's answer IS "no route known" — cache it
    except (httpx.HTTPError, KeyError, ValueError):
        pass                             # transient / malformed → short-lived miss, retried

    _cache_put(_ROUTE_CACHE, cs, out, confirmed)
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
    hit, cached = _cache_get(_AIRCRAFT_CACHE, hx)
    if hit:
        return cached

    out: dict | None = None
    confirmed = False
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
            confirmed = True
        elif r.status_code == 404:
            confirmed = True             # confirmed unknown airframe
    except (httpx.HTTPError, KeyError, ValueError):
        pass                             # transient / malformed → short-lived miss, retried

    _cache_put(_AIRCRAFT_CACHE, hx, out, confirmed)
    return out
