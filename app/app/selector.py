"""Pick the single 'featured' flight to show — the one you'd hear from the window.

Default rule: among aircraft inside the watch sector, the lowest + closest (a plane
you can hear is low and near). Hysteresis keeps the currently-featured aircraft
sticky so the display doesn't flip between two equally-close planes.

This module also does the cheap, offline annotation work: distance/bearing from the
receiver, distance to the home airport, copying any LOCAL enrichment fields that a
DB-enabled readsb merges into aircraft.json, and arrival/departure classification.
"""
from __future__ import annotations

from .geo import bearing_deg, haversine_km

# How much closer (km) a challenger must be to steal "featured" from the incumbent.
_HYSTERESIS_KM = 5.0
# ...or this much lower (ft) — a markedly lower plane wins even if slightly farther.
_HYSTERESIS_ALT_FT = 1500.0

# ADS-B emitter categories counted as "general aviation" (skipped when hide_general_aviation):
# A1 = light (<15500 lb), A7 = rotorcraft, B1 glider, B2 lighter-than-air, B3 parachutist,
# B4 ultralight, B6 UAV, B7 space. Kept: A0 (unknown), A2 small/bizjet, A3-A6 airliners/large.
_GA_CATEGORIES = {"A1", "A7", "B1", "B2", "B3", "B4", "B6", "B7"}


def in_sector(bearing: float, center: float, half_angle: float) -> bool:
    """Is ``bearing`` within ``center ± half_angle`` (degrees, wrap-safe)?"""
    diff = abs((bearing - center + 180) % 360 - 180)
    return diff <= half_angle


def alt_ft(ac: dict) -> float:
    """Barometric altitude as a number. readsb reports the string 'ground' for
    surface aircraft (which broke the numeric sort) — treat that as 0."""
    v = ac.get("alt_baro")
    return float(v) if isinstance(v, (int, float)) else 0.0


def _num(value) -> float | None:
    """Coerce a value to float, or None if it isn't numeric (e.g. 'ground')."""
    return float(value) if isinstance(value, (int, float)) else None


def is_airborne(ac: dict) -> bool:
    """Skip taxiing/parked aircraft (alt_baro == 'ground') — not "flights you hear"."""
    return ac.get("alt_baro") != "ground"


def annotate(ac: dict, lat: float, lon: float, airport: dict | None = None) -> dict:
    """Annotate a decoded aircraft dict (returns a shallow copy).

    Adds geometry — ``distance_km`` + ``bearing_from_me_deg`` from the receiver,
    and ``distance_to_airport_km`` from the home airport when ``airport`` is given
    (a ``{"lat":..., "lon":...}`` dict). Also copies any LOCAL enrichment fields a
    DB-enabled readsb merges into the row, using the exact aircraft.json keys:
    ``t``→type, ``r``→registration, ``desc``→type_desc, ``ownOp``→operator, and the
    ``dbFlags`` bit-0 → ``military`` bool. These are left absent (None) when the
    device doesn't emit them, so the caller can backfill from adsbdb.
    """
    ac = dict(ac)

    if ac.get("lat") is not None and ac.get("lon") is not None:
        ac["distance_km"] = round(haversine_km(lat, lon, ac["lat"], ac["lon"]), 2)
        ac["bearing_from_me_deg"] = round(bearing_deg(lat, lon, ac["lat"], ac["lon"]))
        if airport and airport.get("lat") is not None and airport.get("lon") is not None:
            ac["distance_to_airport_km"] = round(
                haversine_km(airport["lat"], airport["lon"], ac["lat"], ac["lon"]), 2)
        else:
            ac["distance_to_airport_km"] = None
    else:
        ac.setdefault("distance_to_airport_km", None)

    # --- Copy local DB enrichment (present only on DB-enabled readsb) -----------
    # exact aircraft.json keys → our flight-object field names.
    ac["type"] = ac.get("t")               # ICAO type code, e.g. "B788"
    ac["type_desc"] = ac.get("desc")       # human type string, e.g. "BOEING 787-8"
    ac["registration"] = ac.get("r")       # tail number, e.g. "CC-BBB"
    ac["operator"] = ac.get("ownOp")       # registered owner/operator
    ac["military"] = bool(ac.get("dbFlags", 0) & 1)   # dbFlags bit 0 = military

    return ac


def _matches_airport(code, cfg) -> bool:
    """Does an origin/destination code match the configured home airport?

    Matches on either IATA or ICAO so adsbdb (which returns IATA, ICAO fallback)
    lines up with ``cfg.home_airport`` regardless of which form the user entered.
    """
    if not code:
        return False
    # home_codes() yields the home airport's {ICAO, IATA} from the bundled seed OR the
    # resolved-airport cache, so this matches adsbdb's IATA routes for ANY home airport.
    from .airports import home_codes
    return str(code).strip().upper() in home_codes(cfg)


def classify(ac: dict, cfg) -> dict:
    """Set ``is_arrival`` / ``is_departure`` on the aircraft (mutates + returns).

    ARRIVAL   = destination is the home airport, OR (descending < -150 fpm AND
                alt < 12000 ft AND within 45 km of the airport).
    DEPARTURE = origin is the home airport, OR (climbing > 150 fpm AND alt < 12000
                ft AND within 45 km of the airport).
    Route-based matches require an enriched origin/destination; the vertical-rate
    heuristic works on raw aircraft.json fields alone.
    """
    alt = _num(ac.get("alt_baro"))
    rate = _num(ac.get("baro_rate"))
    dist_apt = ac.get("distance_to_airport_km")
    near = dist_apt is not None and dist_apt < 45.0
    low = alt is not None and alt < 12000.0

    arrival = _matches_airport(ac.get("destination"), cfg)
    if not arrival and rate is not None and low and near:
        arrival = rate < -150.0

    # Departure = LEAVING the home field, so the plane must be near AND still climbing out
    # (low), not at cruise. The altitude gate stops a high overflight whose enriched origin is
    # wrongly "home" (a stale/reused-callsign adsbdb route) from being mislabelled "TAKING OFF"
    # — e.g. an FL340 cruiser passing over the field.
    departure = False
    if near and low:
        departure = bool(_matches_airport(ac.get("origin"), cfg)
                         or (rate is not None and rate > 150.0))

    ac["is_arrival"] = bool(arrival)
    ac["is_departure"] = bool(departure)
    return ac


def _passes_traffic_mode(ac: dict, cfg) -> bool:
    """Apply cfg.traffic_mode (all | arrivals | departures | runway) to one aircraft."""
    mode = getattr(cfg, "traffic_mode", "all")
    if mode == "arrivals":
        return bool(ac.get("is_arrival"))
    if mode == "departures":
        return bool(ac.get("is_departure"))
    if mode == "arrdep":          # arrivals + departures only (exclude overflights)
        return bool(ac.get("is_arrival") or ac.get("is_departure"))
    if mode == "runway":
        # only arrivals landing on a runway whose approach passes your window
        return ac.get("landing_runway") in (getattr(cfg, "visible_runways", None) or [])
    return True   # "all" (or anything unknown) → no filtering


def _in_proximity(ac: dict, cfg) -> bool:
    """Is this aircraft inside the close 'right in front of me' proximity zone?

    Such aircraft are featured even with no callsign / even if GA (the clutter filters
    are bypassed) and take priority — the 'it just passed my window' case. Gated on a
    near distance band, the proximity sector's bearing, AND a low AGL ceiling so a high
    overflight that merely shares the compass direction doesn't trip it.
    """
    p = getattr(cfg, "proximity", None)
    if p is None or not getattr(p, "enabled", False):
        return False
    d = ac.get("distance_km")
    if d is None or not (p.min_km <= d <= p.max_km):
        return False
    if not in_sector(ac.get("bearing_from_me_deg", 0), p.center_deg, p.half_angle_deg):
        return False
    elev = getattr(cfg, "airport_elev_ft", 0.0) or 0.0
    return (alt_ft(ac) - elev) <= p.max_agl_ft


def pick_featured(aircraft: list[dict], cfg, last_hex: str | None = None) -> dict | None:
    """Choose the featured flight: filters → (watch sector OR proximity sector) → rule.

    Two sectors decide candidacy. The broad WATCH sector applies the clutter filters
    (hide no-callsign / hide GA). The close PROXIMITY sector BYPASSES those filters and
    takes PRIORITY — a low plane in front of your window is featured even with no flight
    ID, over a distant arrival. ``traffic_mode`` still applies to both.

    ``last_hex`` is the hex of the previously-featured aircraft; if it is still a valid
    candidate we keep it featured (HYSTERESIS) unless a challenger is clearly better —
    more than ``_HYSTERESIS_KM`` km closer, or markedly lower — so the display stops
    flickering between two similar planes.
    """
    cands: list[dict] = []
    for ac in aircraft:
        if "distance_km" not in ac or not is_airborne(ac):
            continue
        prox = _in_proximity(ac, cfg)
        # Clutter filters apply only OUTSIDE the proximity zone — a plane right in front
        # of you is worth featuring even without a flight ID / even if it's light GA.
        if not prox:
            if getattr(cfg, "hide_no_callsign", False) and not (ac.get("flight") or "").strip():
                continue                              # skip hex-only targets (no flight ID)
            if getattr(cfg, "hide_general_aviation", False) and ac.get("category") in _GA_CATEGORIES:
                continue                              # skip light GA / rotorcraft / gliders
        if not _passes_traffic_mode(ac, cfg):         # traffic_mode applies to both sectors
            continue
        in_watch = (cfg.watch.min_km <= ac["distance_km"] <= cfg.watch.max_km
                    and in_sector(ac.get("bearing_from_me_deg", 0),
                                  cfg.watch.center_deg, cfg.watch.half_angle_deg))
        if not (prox or in_watch):                    # must be in at least one sector
            continue
        cands.append(ac)

    if not cands:
        return None

    if cfg.select_rule == "closest":
        key = lambda a: a["distance_km"]                       # noqa: E731
    else:  # default: lowest_closest — lowest altitude, then nearest
        key = lambda a: (alt_ft(a), a["distance_km"])          # noqa: E731

    # Priority: if anything is in the proximity zone, feature the best of THOSE — a plane
    # in front of your window wins over a distant arrival; else use the full pool.
    prox_cands = [a for a in cands if _in_proximity(a, cfg)]
    pool = prox_cands or cands
    best = min(pool, key=key)

    # --- Hysteresis: prefer the incumbent unless the new best is clearly better. The
    # incumbent is honoured only WITHIN the active pool, so a fresh proximity contact
    # preempts a watch-sector incumbent instead of being held back by hysteresis.
    if last_hex:
        incumbent = next((a for a in pool if a.get("hex") == last_hex), None)
        if incumbent is not None and incumbent is not best:
            closer = incumbent["distance_km"] - best["distance_km"]
            lower = alt_ft(incumbent) - alt_ft(best)
            clearly_better = closer > _HYSTERESIS_KM or lower > _HYSTERESIS_ALT_FT
            if not clearly_better:
                return incumbent

    return best
