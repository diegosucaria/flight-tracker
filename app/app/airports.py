"""Offline airport coordinate table + resolver.

Coords are verified from OurAirports (public domain). The receiver never needs the
network to resolve its OWN home airport — the bundled dict below guarantees that, so
a Pi reboot with no internet can still anchor arrivals/departures. The fetch fallback
only runs for an UNKNOWN user-entered code and caches the result to /config.

Public API:
- resolve_airport(code, client=None, allow_fetch=True)  -> record dict | None
- home_airport_coords(cfg)                               -> (lat, lon) | (None, None)
"""
from __future__ import annotations

import csv
import io
import json
import os

# --- Bundled table (verified OurAirports coords, public domain) ----------------
# Keyed by ICAO. Each entry carries its IATA so we can build an IATA index too.
# lat/lon are decimal degrees (south/west negative).
_AIRPORTS: dict[str, dict] = {
    # A small offline seed of major world hubs so common codes resolve with no network. ANY
    # other ICAO/IATA still resolves via the OurAirports fetch below — your home airport need
    # not be in this list (it's looked up + cached from your HOME_AIRPORT on first run).
    "KSEA": {"iata": "SEA", "lat": 47.4502, "lon": -122.3088, "name": "Seattle–Tacoma Intl"},
    "KJFK": {"iata": "JFK", "lat": 40.6398, "lon": -73.7789,  "name": "New York – John F. Kennedy Intl"},
    "KLAX": {"iata": "LAX", "lat": 33.9425, "lon": -118.4081, "name": "Los Angeles Intl"},
    "KORD": {"iata": "ORD", "lat": 41.9742, "lon": -87.9073,  "name": "Chicago O'Hare Intl"},
    "EGLL": {"iata": "LHR", "lat": 51.4706, "lon": -0.4619,   "name": "London Heathrow"},
    "LFPG": {"iata": "CDG", "lat": 49.0097, "lon": 2.5479,    "name": "Paris Charles de Gaulle"},
    "EDDF": {"iata": "FRA", "lat": 50.0264, "lon": 8.5431,    "name": "Frankfurt am Main"},
    "OMDB": {"iata": "DXB", "lat": 25.2528, "lon": 55.3644,   "name": "Dubai Intl"},
    "RJTT": {"iata": "HND", "lat": 35.5523, "lon": 139.7798,  "name": "Tokyo Haneda"},
    "YSSY": {"iata": "SYD", "lat": -33.9399, "lon": 151.1753, "name": "Sydney Kingsford Smith"},
    "SBGR": {"iata": "GRU", "lat": -23.4313, "lon": -46.4700, "name": "São Paulo – Guarulhos Intl"},
}

# Build an IATA->ICAO index over the bundled table once at import.
_IATA_INDEX: dict[str, str] = {v["iata"]: k for k, v in _AIRPORTS.items() if v.get("iata")}

_CACHE_PATH = os.environ.get("AIRPORT_CACHE_PATH", "/config/airports_cache.json")
_OURAIRPORTS_URL = (
    "https://raw.githubusercontent.com/davidmegginson/"
    "ourairports-data/main/airports.csv"
)
_runtime_cache: dict[str, dict] = {}   # in-memory, loaded lazily from disk


def _record(code: str, icao: str, iata, lat, lon, name, elev=0.0) -> dict:
    """Build the uniform resolver record returned to callers."""
    try:
        elev_ft = round(float(elev), 0)
    except (TypeError, ValueError):
        elev_ft = 0.0
    return {
        "code": code,
        "icao": icao,
        "iata": iata or None,
        "lat": round(float(lat), 4),
        "lon": round(float(lon), 4),
        "elev_ft": elev_ft,
        "name": name,
    }


def _lookup_bundled(code: str) -> dict | None:
    """Resolve a normalized code against the offline bundled table (ICAO or IATA)."""
    if code in _AIRPORTS:                        # ICAO hit
        a = _AIRPORTS[code]
        return _record(code, code, a["iata"], a["lat"], a["lon"], a["name"])
    if code in _IATA_INDEX:                       # IATA hit
        icao = _IATA_INDEX[code]
        a = _AIRPORTS[icao]
        return _record(code, icao, a["iata"], a["lat"], a["lon"], a["name"])
    return None


def _load_disk_cache() -> dict:
    """Lazily load the OurAirports fetch cache from /config (once per process)."""
    global _runtime_cache
    if not _runtime_cache and os.path.exists(_CACHE_PATH):
        try:
            with open(_CACHE_PATH) as f:
                _runtime_cache = json.load(f)
        except (OSError, ValueError):
            _runtime_cache = {}
    return _runtime_cache


def _save_disk_cache() -> None:
    """Persist the fetch cache to /config; best-effort (never raises)."""
    try:
        os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
        with open(_CACHE_PATH, "w") as f:
            json.dump(_runtime_cache, f)
    except OSError:
        pass


async def _fetch_and_cache(client) -> None:
    """One-time: pull the OurAirports CSV, index ICAO+IATA into the disk cache.

    Runs only when an unknown code is requested AND allow_fetch is True. Indexes
    every large/medium/small airport; the result is written to /config so it
    survives container restarts.
    """
    r = await client.get(_OURAIRPORTS_URL, timeout=30)
    r.raise_for_status()
    reader = csv.DictReader(io.StringIO(r.text))
    for row in reader:
        if row["type"] not in ("large_airport", "medium_airport", "small_airport"):
            continue
        # icao_code is blank for many small fields — fall back to ident.
        icao = (row["icao_code"] or row["ident"] or "").strip().upper()
        if not icao:
            continue
        iata = (row["iata_code"] or "").strip().upper() or None
        try:
            rec = _record(icao, icao, iata, row["latitude_deg"],
                          row["longitude_deg"], row["name"], row.get("elevation_ft"))
        except (TypeError, ValueError):
            continue
        _runtime_cache[icao] = rec
        if iata:
            _runtime_cache[iata] = rec
    _save_disk_cache()


async def resolve_airport(code: str, client=None, allow_fetch: bool = True) -> dict | None:
    """Resolve an ICAO (``KSEA``) or IATA (``SEA``) code to a record dict.

    Returns ``{"code", "icao", "iata", "lat", "lon", "name"}`` or ``None``.

    Order: bundled dict → /config fetch cache → one-time OurAirports fetch (only
    if a ``client`` is given and ``allow_fetch``). ``client`` is the app's shared
    ``httpx.AsyncClient``; pass ``None`` for offline-only resolution.
    """
    code = (code or "").strip().upper()
    if not code:
        return None

    hit = _lookup_bundled(code)
    if hit:
        return hit

    cache = _load_disk_cache()
    if code in cache:
        return {**cache[code], "code": code}

    if client is not None and allow_fetch:
        try:
            await _fetch_and_cache(client)
        except Exception:                         # network down / CSV moved → graceful miss
            return None
        if code in _runtime_cache:
            return {**_runtime_cache[code], "code": code}

    return None


def home_codes(cfg) -> set[str]:
    """All codes that identify the home airport (ICAO + IATA), for route matching.

    adsbdb routes use IATA (e.g. ``SEA``); cfg.home_airport is ICAO (``KSEA``). This
    returns ``{"KSEA", "SEA"}`` so we can tell whether a route end is home.
    """
    code = (getattr(cfg, "home_airport", "") or "").strip().upper()
    codes = {code} if code else set()
    # Map ICAO<->IATA from the bundled seed OR the resolved-airport cache (populated at startup
    # by resolve_airport), so any home airport — not just seeded ones — matches IATA routes.
    hit = _lookup_bundled(code) or _load_disk_cache().get(code)
    if hit:
        if hit.get("icao"):
            codes.add(hit["icao"].upper())
        if hit.get("iata"):
            codes.add(hit["iata"].upper())
    return codes


def home_airport_coords(cfg) -> tuple[float | None, float | None]:
    """Best (lat, lon) for the configured home airport.

    Prefers the cached ``cfg.airport_lat``/``cfg.airport_lon`` (already resolved
    and persisted), else falls back to the OFFLINE bundled lookup of
    ``cfg.home_airport``. Never touches the network — the async fetch path lives in
    ``resolve_airport``; this is the cheap synchronous accessor the hot poll loop
    and annotate() use.
    """
    lat = getattr(cfg, "airport_lat", None)
    lon = getattr(cfg, "airport_lon", None)
    if lat and lon:                               # non-zero, non-None cache hit
        return float(lat), float(lon)

    hit = _lookup_bundled((getattr(cfg, "home_airport", "") or "").strip().upper())
    if hit:
        return hit["lat"], hit["lon"]
    return None, None
