"""Recent arrivals / departures at the home airport, from OpenSky Network (free, no key).

NB: this is RECENT OBSERVED traffic (ADS-B, last few hours), NOT a scheduled timetable —
OpenSky has no forward schedule. Cached with a long TTL because the endpoints are
credit-expensive and the data only trickles in. Never raises.
"""
from __future__ import annotations

import time

_ARR = "https://opensky-network.org/api/flights/arrival"
_DEP = "https://opensky-network.org/api/flights/departure"
_TTL_S = 600.0            # refresh at most every 10 min (OpenSky /flights is rate-limited)
_WINDOW_S = 3 * 3600      # look back 3 hours
_MAX = 8                  # rows shown per list
_cache: dict = {"t": 0.0, "icao": None, "data": None}


def _clean(rows, kind: str) -> list[dict]:
    """Normalise OpenSky flight rows -> {callsign, other, time, kind}, newest first."""
    out = []
    for r in rows or []:
        cs = (r.get("callsign") or "").strip()
        # arrival: came FROM estDepartureAirport, landed at lastSeen
        # departure: going TO estArrivalAirport (often unknown), left at firstSeen
        other = r.get("estDepartureAirport") if kind == "arrival" else r.get("estArrivalAirport")
        ts = r.get("lastSeen") if kind == "arrival" else r.get("firstSeen")
        out.append({"callsign": cs or None, "other": other or None, "time": ts})
    out.sort(key=lambda x: x["time"] or 0, reverse=True)
    return out[:_MAX]


async def get_flights(icao: str, client) -> dict | None:
    """{icao, arrivals:[…], departures:[…], window_h} for ``icao``, or None. Never raises."""
    icao = (icao or "").strip().upper()
    if not icao:
        return None

    now = time.monotonic()
    if _cache["data"] and _cache["icao"] == icao and now - _cache["t"] < _TTL_S:
        return _cache["data"]

    end = int(time.time())
    begin = end - _WINDOW_S

    async def fetch(url: str, kind: str):
        try:
            r = await client.get(url, params={"airport": icao, "begin": begin, "end": end}, timeout=20)
            r.raise_for_status()
            return _clean(r.json(), kind)
        except Exception:
            return []

    arr = await fetch(_ARR, "arrival")
    dep = await fetch(_DEP, "departure")
    data = {"icao": icao, "arrivals": arr, "departures": dep, "window_h": _WINDOW_S // 3600}
    # cache even an empty result for this airport, but don't clobber a good cache with a
    # transient double-failure for the SAME airport (keep last-good then).
    if arr or dep or _cache["icao"] != icao:
        _cache.update(t=now, icao=icao, data=data)
    return _cache["data"]
