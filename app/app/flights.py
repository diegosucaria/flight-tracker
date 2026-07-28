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

_TTL_S = 1800.0          # refresh at most every 30 min — ONE airport call returns every
                         #   arrival/departure, so this bounds AeroDataBox usage well inside
                         #   the free tier (a 12 h schedule barely changes in 30 min)
_ERR_RETRY_S = 300.0     # after a failed refresh: keep the last good data, retry in 5 min
                         #   (without this a stale cache + a down upstream = a fetch per poll tick)
_MAX = 8                  # rows shown per list (the CACHE keeps the full lists for route matching)
_cache: dict = {"t": -1e12, "icao": None, "data": None, "good": False}

# --- OpenSky (observed, free) ------------------------------------------------
_OSKY_ARR = "https://opensky-network.org/api/flights/arrival"
_OSKY_DEP = "https://opensky-network.org/api/flights/departure"
_OSKY_WINDOW_S = 3 * 3600

# --- AeroDataBox (scheduled, keyed) ------------------------------------------
_ADB_HOST = "aerodatabox.p.rapidapi.com"
_ADB_KEY_ENV = "FLIGHTS_API_KEY"        # your RapidAPI key for AeroDataBox
_ADB_PAST_H = 2           # look BACK too — the flight that just departed is the one in the air
_ADB_AHEAD_H = 10         # (2 + 10 = the API's 12 h max window, same cost as before)
_ADB_WINDOW_H = _ADB_PAST_H + _ADB_AHEAD_H


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
    return [{k: v for k, v in r.items() if k != "_ts"} for r in out]


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
    return [{k: v for k, v in r.items() if k != "_k"} for r in out]


async def _aerodatabox(icao: str, client) -> dict | None:
    key = os.environ.get(_ADB_KEY_ENV)
    if not key:
        return None                                  # no key -> caller falls back to OpenSky
    now = datetime.now()
    frm = (now - timedelta(hours=_ADB_PAST_H)).strftime("%Y-%m-%dT%H:%M")
    to = (now + timedelta(hours=_ADB_AHEAD_H)).strftime("%Y-%m-%dT%H:%M")
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
    if _cache["icao"] == icao and now - _cache["t"] < (_TTL_S if _cache["good"] else _ERR_RETRY_S):
        return _cache["data"]

    data = await _aerodatabox(icao, client)          # scheduled, if a key is configured
    # A successful AeroDataBox response counts as good even with empty lists (a legitimately
    # quiet window) — otherwise a sleepy overnight airport would be re-fetched every 5 min
    # and burn the free tier. Only a FAILED fetch gets the short retry.
    ok = data is not None
    if data is None:
        data = await _opensky(icao, client)          # observed fallback (never None)
        ok = bool(data and (data.get("arrivals") or data.get("departures")))

    if ok or _cache["icao"] != icao or not _cache["good"]:
        _cache.update(t=now, icao=icao, data=data, good=ok)
    else:
        # Failed refresh but we still hold good rows for this airport: keep them, retry soon.
        _cache["t"] = now - _TTL_S + _ERR_RETRY_S
    return _cache["data"]


def for_display(data: dict | None) -> dict:
    """Trim the full cached lists down to the few rows the map's corner box shows."""
    if not data:
        return {}
    out = dict(data)
    out["arrivals"] = (data.get("arrivals") or [])[:_MAX]
    out["departures"] = (data.get("departures") or [])[:_MAX]
    return out


_IATA_NO_RE = re.compile(r"^([A-Z]\d|\d[A-Z]|[A-Z]{2,3})\s*0*(\d+)$")  # "AR 1570" / "FO5441"
_NUM_RE = re.compile(r"0*(\d{1,4})[A-Z]?$")                            # trailing number of a callsign


async def schedule_route_for(cfg, callsign: str, airline_iata: str | None, client) -> dict | None:
    """Authoritative route for a home-airport flight, from the airport's own timetable.

    Airlines reuse callsigns across different city pairs, so the community callsign→route
    DB (adsbdb) can return a stale-but-geographically-plausible pair that the geometry
    checks can't reject. The schedule (already fetched for the Flights layer — this adds
    no API calls beyond that cache's cadence) has the real leg; let it win.

    Matching: ICAO callsign ``ARG1570`` → trailing number ``1570`` + the airline's IATA
    code (``AR``, from adsbdb's airline record) → schedule row ``AR 1570``. Falls back to
    a number-only match when that is unambiguous. Observed-mode (OpenSky) rows carry raw
    callsigns and match directly. Returns ``{"origin","destination","sched_when",
    "sched_status"}`` (home end expressed as ``cfg.home_iata``) or ``None``.
    """
    cs = (callsign or "").strip().upper()
    if not cs:
        return None
    data = await get_flights(getattr(cfg, "home_airport", ""), client)
    if not data:
        return None
    home = (getattr(cfg, "home_iata", "") or getattr(cfg, "home_airport", "") or "").strip().upper()

    m = _NUM_RE.search(cs)
    num = m.group(1) if m else None
    want_iata = (airline_iata or "").strip().upper()

    exact: list = []
    loose: list = []
    for kind, rows in (("dep", data.get("departures") or []),
                       ("arr", data.get("arrivals") or [])):
        for row in rows:
            rcs = (row.get("callsign") or "").strip().upper()
            if not rcs or not row.get("other"):
                continue
            if not data.get("scheduled"):
                if rcs.replace(" ", "") == cs:       # observed rows carry the raw callsign
                    exact.append((kind, row))
                continue
            fm = _IATA_NO_RE.match(rcs)
            if not fm or not num or fm.group(2).lstrip("0") != num.lstrip("0"):
                continue
            (exact if (want_iata and fm.group(1) == want_iata) else loose).append((kind, row))

    # Require an UNAMBIGUOUS match — two rows with the same number (a turnaround using one
    # number both ways) could point either direction, so pass rather than guess.
    pick = exact[0] if len(exact) == 1 else (loose[0] if (not exact and len(loose) == 1) else None)
    if not pick:
        return None
    kind, row = pick
    other = str(row["other"]).strip().upper()
    o, d = (home, other) if kind == "dep" else (other, home)
    return {"origin": o, "destination": d,
            "sched_when": row.get("when"), "sched_status": row.get("status")}
