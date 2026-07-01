"""Airport arrivals / departures for the map's Flights layer.

Two sources, picked automatically:
  • SCHEDULED — AeroDataBox (RapidAPI) when ``FLIGHTS_API_KEY`` is set. A real timetable
    (airline, flight no., scheduled time, status) for the next ~12 h.
  • OBSERVED  — OpenSky Network (free, no key) as the fallback: recent ADS-B arrivals/
    departures from the last few hours (NOT a schedule).

Cached with a long TTL — both sources are rate-limited (AeroDataBox free tier is small).
Never raises.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta

_TTL_S = 600.0            # refresh at most every 10 min
_MAX = 8                  # rows shown per list
_cache: dict = {"t": 0.0, "icao": None, "data": None}

# --- OpenSky (observed, free) ------------------------------------------------
_OSKY_ARR = "https://opensky-network.org/api/flights/arrival"
_OSKY_DEP = "https://opensky-network.org/api/flights/departure"
_OSKY_WINDOW_S = 3 * 3600

# --- AeroDataBox (scheduled, keyed) ------------------------------------------
_ADB_HOST = "aerodatabox.p.rapidapi.com"
_ADB_KEY_ENV = "FLIGHTS_API_KEY"        # your RapidAPI key for AeroDataBox
_ADB_WINDOW_H = 12


def _hhmm_unix(ts) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(ts).strftime("%H:%M")   # container TZ (set TZ env)
    except (OverflowError, OSError, ValueError):
        return None


def _hhmm_iso(s) -> str | None:
    m = re.search(r"(\d{2}:\d{2})", str(s or ""))
    return m.group(1) if m else None


def _osky_clean(rows, kind: str) -> list[dict]:
    out = []
    for r in rows or []:
        cs = (r.get("callsign") or "").strip()
        other = r.get("estDepartureAirport") if kind == "arrival" else r.get("estArrivalAirport")
        ts = r.get("lastSeen") if kind == "arrival" else r.get("firstSeen")
        out.append({"callsign": cs or None, "airline": None, "other": other or None,
                    "when": _hhmm_unix(ts), "status": None, "_ts": ts or 0})
    out.sort(key=lambda x: x["_ts"], reverse=True)
    return [{k: v for k, v in r.items() if k != "_ts"} for r in out[:_MAX]]


async def _opensky(icao: str, client) -> dict | None:
    end = int(time.time())
    begin = end - _OSKY_WINDOW_S

    async def fetch(url, kind):
        try:
            r = await client.get(url, params={"airport": icao, "begin": begin, "end": end}, timeout=20)
            r.raise_for_status()
            return _osky_clean(r.json(), kind)
        except Exception:
            return []

    arr = await fetch(_OSKY_ARR, "arrival")
    dep = await fetch(_OSKY_DEP, "departure")
    return {"icao": icao, "scheduled": False, "arrivals": arr, "departures": dep,
            "window_h": _OSKY_WINDOW_S // 3600}


def _adb_clean(rows, kind: str) -> list[dict]:
    out = []
    for f in rows or []:
        mv = f.get("movement") or {}
        ap = mv.get("airport") or {}
        st = mv.get("scheduledTime") or {}
        when = _hhmm_iso(st.get("local") or st.get("utc"))
        out.append({
            "callsign": (f.get("number") or "").strip() or None,
            "airline": (f.get("airline") or {}).get("name"),
            "other": ap.get("iata") or ap.get("icao") or ap.get("name"),
            "when": when,
            "status": f.get("status"),
            "_k": st.get("utc") or st.get("local") or "",
        })
    out.sort(key=lambda x: x["_k"])                 # soonest first
    return [{k: v for k, v in r.items() if k != "_k"} for r in out[:_MAX]]


async def _aerodatabox(icao: str, client) -> dict | None:
    key = os.environ.get(_ADB_KEY_ENV)
    if not key:
        return None                                  # no key -> caller falls back to OpenSky
    now = datetime.now()
    frm = now.strftime("%Y-%m-%dT%H:%M")
    to = (now + timedelta(hours=_ADB_WINDOW_H)).strftime("%Y-%m-%dT%H:%M")
    url = f"https://{_ADB_HOST}/flights/airports/icao/{icao}/{frm}/{to}"
    headers = {"x-rapidapi-key": key, "x-rapidapi-host": _ADB_HOST}
    params = {"withLeg": "false", "withCancelled": "true", "withCodeshared": "false",
              "withCargo": "false", "withPrivate": "false", "withLocation": "false"}
    try:
        r = await client.get(url, headers=headers, params=params, timeout=25)
        r.raise_for_status()
        j = r.json()
    except Exception:
        return None                                  # transient -> fall back to OpenSky
    return {"icao": icao, "scheduled": True, "window_h": _ADB_WINDOW_H,
            "arrivals": _adb_clean(j.get("arrivals"), "arrival"),
            "departures": _adb_clean(j.get("departures"), "departure")}


async def get_flights(icao: str, client) -> dict | None:
    """{icao, scheduled, arrivals:[…], departures:[…], window_h} or None. Never raises.

    Uses AeroDataBox (scheduled) when ``FLIGHTS_API_KEY`` is set, else OpenSky (observed).
    """
    icao = (icao or "").strip().upper()
    if not icao:
        return None

    now = time.monotonic()
    if _cache["data"] and _cache["icao"] == icao and now - _cache["t"] < _TTL_S:
        return _cache["data"]

    data = await _aerodatabox(icao, client)          # scheduled, if a key is configured
    if data is None:
        data = await _opensky(icao, client)          # observed fallback

    got = data and (data.get("arrivals") or data.get("departures"))
    if got or _cache["icao"] != icao:
        _cache.update(t=now, icao=icao, data=data)
    return _cache["data"]
