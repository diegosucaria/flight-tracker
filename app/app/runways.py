"""Infer the active landing runway from ADS-B, so the UI/display can say whether an
arrival will pass the window (a near-side runway) or land on the far side.

We don't get the runway from the signal — we infer it from each arrival's **true track**
on final: a plane lined up to land has its track ≈ the runway bearing AND the airport ahead
of it. Aggregating the last few arrivals gives the airport's *active* runway (it's
wind-driven and flips through the day).

Runway geometry (each landing direction's **true** bearing) is resolved at RUNTIME for the
configured home airport from **OurAirports** (public domain) and cached to ``/config`` — so
this works for ANY airport with no hardcoded table. ``resolve_runways()`` runs once at
startup; the hot path reads the cache via the synchronous ``runways_for()``. Offline-safe:
a failed fetch just means no runway inference until a later successful resolve.

Public API:
- ``async resolve_runways(code, client, allow_fetch=True)`` -> list[{"id","brg"}]  (startup)
- ``runways_for(code)`` -> list[{"id","brg"}]                                       (cached)
- ``infer_landing_runway`` / ``infer_departure_runway`` / ``active_runway``
"""
from __future__ import annotations

import csv
import io
import json
import math
import os

from .geo import haversine_km

_CACHE_PATH = os.environ.get("RUNWAY_CACHE_PATH", "/config/runways_cache.json")
_OURAIRPORTS_RUNWAYS_URL = (
    "https://raw.githubusercontent.com/davidmegginson/"
    "ourairports-data/main/runways.csv"
)
# ICAO -> [{"id": "01", "brg": 358.0}, ...]; populated by resolve_runways() at startup.
_RUNWAY_CACHE: dict[str, list[dict]] = {}

_MATCH_TOL_DEG = 25.0       # how close the track must be to a runway bearing
_AHEAD_TOL_DEG = 40.0       # how close the airport must be to "straight ahead" (departures)
_CORRIDOR_KM = 8.0          # final-approach corridor half-width (cross-track from centerline)


def _ang_diff(a: float, b: float) -> float:
    """Smallest absolute angle between two bearings (0-180)."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from point 1 to point 2, degrees (0-360)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


# ---------------------------------------------------------------------------
# Runtime resolution (OurAirports runways.csv) + cache
# ---------------------------------------------------------------------------
def _heading_from_coords(row: dict, end: str) -> float | None:
    """Fallback true heading for a runway end, computed from the two thresholds' coords."""
    other = "he" if end == "le" else "le"
    try:
        return bearing_deg(
            float(row[f"{end}_latitude_deg"]), float(row[f"{end}_longitude_deg"]),
            float(row[f"{other}_latitude_deg"]), float(row[f"{other}_longitude_deg"]),
        )
    except (KeyError, ValueError, TypeError):
        return None


def _parse_runways(csv_text: str, code: str) -> list[dict]:
    """Extract both landing directions of every open runway at ``code`` from runways.csv.

    Each physical runway row carries a low end (``le_*``) and high end (``he_*``); each end
    is a landing direction with its own ident + true heading. Prefer the published
    ``*_heading_degT``; fall back to the bearing between the two thresholds.
    """
    out: list[dict] = []
    for row in csv.DictReader(io.StringIO(csv_text)):
        if (row.get("airport_ident") or "").strip().upper() != code:
            continue
        if (row.get("closed") or "0").strip() in ("1", "yes", "true"):
            continue
        for end in ("le", "he"):
            ident = (row.get(f"{end}_ident") or "").strip()
            if not ident:
                continue
            raw = (row.get(f"{end}_heading_degT") or "").strip()
            try:
                brg = float(raw)
            except ValueError:
                brg = _heading_from_coords(row, end)
                if brg is None:
                    continue
            out.append({"id": ident, "brg": round(brg % 360.0, 1)})
    return out


def _load_disk_cache() -> dict:
    """Lazily load the resolved-runways cache from /config (once per process)."""
    global _RUNWAY_CACHE
    if not _RUNWAY_CACHE and os.path.exists(_CACHE_PATH):
        try:
            with open(_CACHE_PATH) as f:
                _RUNWAY_CACHE = json.load(f)
        except (OSError, ValueError):
            _RUNWAY_CACHE = {}
    return _RUNWAY_CACHE


def _save_disk_cache() -> None:
    """Persist the cache to /config; best-effort (never raises)."""
    try:
        os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
        with open(_CACHE_PATH, "w") as f:
            json.dump(_RUNWAY_CACHE, f)
    except OSError:
        pass


async def resolve_runways(code: str, client=None, allow_fetch: bool = True) -> list[dict]:
    """Resolve + cache the runway list for an airport ICAO. Returns ``[{"id","brg"}]``.

    Order: in-memory/disk cache → one-time OurAirports fetch (only if a ``client`` is given
    and ``allow_fetch``). Network failure returns ``[]`` and leaves the cache untouched, so a
    later startup can retry. Call this once at startup with the app's shared httpx client.
    """
    code = (code or "").strip().upper()
    if not code:
        return []
    cache = _load_disk_cache()
    if code in cache:
        return cache[code]
    if client is None or not allow_fetch:
        return []
    try:
        r = await client.get(_OURAIRPORTS_RUNWAYS_URL, timeout=30)
        r.raise_for_status()
        rwys = _parse_runways(r.text, code)
    except Exception:                             # network down / CSV moved → graceful miss
        return []
    _RUNWAY_CACHE[code] = rwys
    _save_disk_cache()
    return rwys


def runways_for(airport_code: str | None) -> list[dict]:
    """Cached runway list for an airport (populated by ``resolve_runways`` at startup)."""
    return _load_disk_cache().get((airport_code or "").strip().upper(), [])


# ---------------------------------------------------------------------------
# Inference (unchanged) — operates on the resolved runway bearings
# ---------------------------------------------------------------------------
def infer_landing_runway(ac: dict, airport: dict | None, code: str | None) -> str | None:
    """CONFIDENT landing runway for an aircraft established on a final, or None.

    Uses the runway's extended centerline: the aircraft must be (a) tracking the runway
    bearing (±tol), (b) on the APPROACH side of the field, and (c) within the final-approach
    corridor (small cross-track from the extended centerline). Vectored arrivals not yet on a
    final return None — the caller falls back to the airport's active-runway prior. Only these
    confident matches should feed the active-runway rollup (no circular reinforcement).
    """
    rwys = runways_for(code)
    if not rwys or not airport:
        return None
    track = ac.get("track")
    lat, lon = ac.get("lat"), ac.get("lon")
    a_lat, a_lon = airport.get("lat"), airport.get("lon")
    if track is None or None in (lat, lon, a_lat, a_lon):
        return None
    d_km = haversine_km(a_lat, a_lon, lat, lon)
    to_plane = bearing_deg(a_lat, a_lon, lat, lon)      # bearing FROM the field TO the aircraft

    best, best_err, best_cross = None, _MATCH_TOL_DEG + 1.0, _CORRIDOR_KM + 1.0
    for r in rwys:
        err = _ang_diff(track, r["brg"])
        if err > _MATCH_TOL_DEG:                        # must be flying the runway heading
            continue
        # On final, the aircraft sits on the reciprocal (approach) side of the centerline.
        delta = math.radians(_ang_diff(to_plane, (r["brg"] + 180.0) % 360.0))
        cross = d_km * math.sin(delta)                  # offset from the extended centerline
        along = d_km * math.cos(delta)                  # > 0 ⇒ on the approach side
        if along <= 0 or cross > _CORRIDOR_KM:
            continue
        # best alignment; on a tie (overlapping siblings) prefer the tighter centerline
        if err < best_err or (err == best_err and cross < best_cross):
            best, best_err, best_cross = r["id"], err, cross
    return best


def infer_departure_runway(ac: dict, airport: dict | None, code: str | None) -> str | None:
    """Best-guess departure runway id for a climbing aircraft, or None.

    Mirror of :func:`infer_landing_runway` but the field is BEHIND the aircraft: a plane
    climbing out tracks ≈ the runway bearing with the airport behind it. The caller should
    only pass aircraft already judged to be departing (climbing + low + near the field).
    """
    rwys = runways_for(code)
    if not rwys or not airport:
        return None
    track = ac.get("track")
    lat, lon = ac.get("lat"), ac.get("lon")
    a_lat, a_lon = airport.get("lat"), airport.get("lon")
    if track is None or None in (lat, lon, a_lat, a_lon):
        return None
    to_airport = bearing_deg(lat, lon, a_lat, a_lon)

    best, best_err = None, _MATCH_TOL_DEG
    for r in rwys:
        err = _ang_diff(track, r["brg"])
        # aligned with the runway AND the field is BEHIND (departing, not landing)
        if err <= best_err and _ang_diff(to_airport, r["brg"]) >= (180.0 - _AHEAD_TOL_DEG):
            best, best_err = r["id"], err
    return best


def active_runway(recent_ids: list[str]) -> str | None:
    """Most common runway among recent arrivals (the airport's current landing direction)."""
    ids = [r for r in recent_ids if r]
    if not ids:
        return None
    return max(set(ids), key=ids.count)
