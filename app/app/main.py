"""flight-tracker app: poll decoded aircraft, enrich, pick the featured flight,
serve the web config UI + a REST/WebSocket feed the display subscribes to.

The poll loop fetches ultrafeeder's aircraft.json once a second, annotates every
aircraft (distance/bearing from the receiver, distance to the home airport),
classifies arrivals/departures, picks ONE featured flight (the one you'd see/hear
from the window — sticky via hysteresis), enriches it with route + airframe data,
estimates its flight duration, and broadcasts the result over /ws. REST endpoints
expose the full live picture and let the web UI edit config.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections import deque
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .airband import airband_status, apply_airband_config, test_beep, write_airband_conf, write_volume
from . import navdata
from .airports import home_airport_coords, home_codes, resolve_airport
from .config import Config
from .enrich import aircraft_info, route_for_callsign
from .geo import haversine_km
from .runways import (
    active_runway, infer_departure_runway, infer_landing_runway, resolve_runways, runways_for,
)
from .selector import annotate, classify, pick_featured
from .gps import GpsReader
from .history import History
from .watchdog import FeedWatchdog, restart_service

AIRCRAFT_JSON_URL = os.environ.get(
    "AIRCRAFT_JSON_URL", "http://ultrafeeder/data/aircraft.json")
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "1"))

# Typical airliner ground speed (knots) used when a flight reports no gs, for the
# great-circle duration estimate. 450 kt ≈ a jet's cruise.
_TYPICAL_GS_KT = 450.0
_KT_TO_KMH = 1.852   # 1 knot = 1.852 km/h

cfg = Config.load()
watchdog = FeedWatchdog()
gps = GpsReader()
history = History()


def receiver_pos() -> tuple[float, float]:
    """Receiver location used for ALL geometry (distance/bearing): the live GPS fix when
    ``use_gps`` is on and a fresh fix exists, else the configured lat/lon."""
    if getattr(cfg, "use_gps", True):
        pos = gps.position()
        if pos is not None:
            return pos
    return (cfg.lat, cfg.lon)


def _decimate(dq: deque, maxn: int) -> list:
    """Down-sample a trail to <= maxn points (evenly), always keeping the newest point."""
    n = len(dq)
    if n <= maxn:
        return [[la, lo] for la, lo in dq]
    step = n / maxn
    pts = [dq[int(i * step)] for i in range(maxn - 1)]
    pts.append(dq[-1])
    return [[la, lo] for la, lo in pts]


def _update_trails(annotated: list[dict]) -> None:
    """Append each aircraft's position to its in-memory trail, prune stale ones, and attach
    a decimated ``trail`` to each aircraft dict for the live map."""
    now = time.monotonic()
    for ac in annotated:
        hx, lat, lon = ac.get("hex"), ac.get("lat"), ac.get("lon")
        if not hx or not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue
        dq = trails.get(hx)
        if dq is None:
            dq = trails[hx] = deque(maxlen=TRAIL_MAX)
        if not dq or dq[-1] != (lat, lon):       # skip duplicate consecutive fixes
            dq.append((lat, lon))
        _trail_seen[hx] = now
        near = isinstance(ac.get("distance_km"), (int, float)) and ac["distance_km"] <= cfg.watch.max_km
        ac["trail"] = _decimate(dq, TRAIL_EMIT if near else TRAIL_EMIT_FAR)
    for hx in [h for h, t in _trail_seen.items() if now - t > TRAIL_TTL]:
        trails.pop(hx, None)
        _trail_seen.pop(hx, None)

# Broadcast payload (featured + count) — what /ws and /api/current send.
state: dict = {"featured": None, "count": 0}
# Last-featured hex, for selector hysteresis (kept across ticks).
last_featured_hex: str | None = None
# Full annotated snapshot of every tracked aircraft, for GET /api/aircraft.
all_aircraft: list[dict] = []
# UI "test flight" override: a fake featured flight for tuning the display without waiting
# for real traffic. None = off. Set/cleared via POST /api/test-flight.
test_flight: dict | None = None
test_clock: bool = False           # UI clock-test override: force the idle flip-clock on the panel
# Rolling window of recently-inferred landing runways -> the airport's active runway.
_recent_runways: deque = deque(maxlen=40)
active_runway_id: str | None = None
# Per-aircraft LOCKED departure runway (hex -> id). A plane departs ONE runway then turns onto
# its route; without locking, the post-takeoff turn re-matches a different runway's heading. We
# lock the first confident match (the straight initial climb) and prune when the aircraft leaves.
_departure_runways: dict = {}
# Most recent landing at the home field (callsign, runway, monotonic) — for the LANDED flash.
_recent_landing: tuple | None = None
LANDED_MSG_TTL = 25.0    # seconds the LANDED message lingers after touchdown
# In-memory flight-path trails for the live map: hex -> recent (lat, lon) points.
TRAIL_MAX = 240          # points kept per aircraft (~4 min at 1 Hz)
TRAIL_TTL = 90.0         # drop a trail when its hex hasn't been seen for this long (s)
TRAIL_EMIT = 48          # max trail points emitted for a nearby (in-band) aircraft over /ws
TRAIL_EMIT_FAR = 12      # shorter trail for far aircraft (trims the /ws payload on the Pi)
trails: dict = {}        # hex -> deque[(lat, lon)]
_trail_seen: dict = {}   # hex -> monotonic last-seen
# Full /ws payload (featured + count + aircraft + map context) — pushed each tick so the
# web map updates in real time without polling. Sent to new clients on connect too.
latest_ws: dict = {"featured": None, "count": 0, "aircraft": [],
                   "receiver": {"lat": cfg.lat, "lon": cfg.lon, "gps": False},
                   "airport": {}, "watch": {}}
# Diagnostics for GET /api/diag.
diag: dict = {
    "messages": 0,
    "aircraft_count": 0,
    "max_range_km": 0.0,
    "gps_fix": False,
}
clients: set[WebSocket] = set()


def _airport_dict() -> dict:
    """Current home-airport {code, lat, lon, elev_ft} from config (coords may be None)."""
    lat, lon = home_airport_coords(cfg)
    return {"code": cfg.home_airport, "lat": lat, "lon": lon,
            "elev_ft": getattr(cfg, "airport_elev_ft", 0.0)}


def _estimate_duration_min(featured: dict, route: dict | None) -> int | None:
    """Great-circle duration estimate (minutes) from origin→destination coords.

    Uses the featured flight's reported ground speed when available, else a typical
    airliner cruise speed. Returns ``None`` when the route lacks both endpoints'
    coordinates.
    """
    if not route:
        return None
    o_lat, o_lon = route.get("origin_lat"), route.get("origin_lon")
    d_lat, d_lon = route.get("dest_lat"), route.get("dest_lon")
    if None in (o_lat, o_lon, d_lat, d_lon):
        return None
    dist_km = haversine_km(o_lat, o_lon, d_lat, d_lon)
    gs = featured.get("gs")
    gs_kt = gs if isinstance(gs, (int, float)) and gs > 50 else _TYPICAL_GS_KT
    speed_kmh = gs_kt * _KT_TO_KMH
    if speed_kmh <= 0:
        return None
    return int(round(dist_km / speed_kmh * 60.0))


def _eta_to_airport_min(ac: dict) -> int | None:
    """Minutes until the aircraft reaches the home airport at its current ground speed.

    This is the useful 'ETA' (time-to-arrival), NOT the whole-flight duration. Only for
    arrivals that are actually closing in — for an overflight/departure it'd be meaningless.
    """
    if not ac.get("is_arrival"):
        return None
    d = ac.get("distance_to_airport_km")
    gs = ac.get("gs")
    if not isinstance(d, (int, float)) or not isinstance(gs, (int, float)) or gs <= 50:
        return None
    return max(0, int(round(d / (gs * _KT_TO_KMH) * 60.0)))


def _landed(ac: dict) -> bool:
    """True when an arrival has reached the home field: on the ground, or at runway level
    (within ~100 ft of the airport elevation) within a few km of the airport."""
    if not ac.get("is_arrival"):
        return False
    d = ac.get("distance_to_airport_km")
    if not isinstance(d, (int, float)) or d > 4.0:
        return False
    alt = ac.get("alt_baro")
    if alt == "ground":
        return True
    return isinstance(alt, (int, float)) and alt <= getattr(cfg, "airport_elev_ft", 0.0) + 100


def _flight_phase(ac: dict) -> str | None:
    """'arrival' | 'departure' | None, from altitude + vertical rate near the home field.

    More robust than instantaneous track for vectored/level arrivals (e.g. a plane that
    overflew the field and is being turned back): a low aircraft near the field that isn't
    climbing out is almost always arriving (descending, or level on a vector); only a clear
    climb-out reads as a departure. Cruising overhead (high) -> None (overflight, leave be).
    """
    alt = ac.get("alt_baro")
    d_apt = ac.get("distance_to_airport_km")
    if not isinstance(d_apt, (int, float)) or d_apt > 60:
        return None
    if not isinstance(alt, (int, float)) or alt > 12000:
        return None
    vs = ac.get("baro_rate")
    if isinstance(vs, (int, float)) and vs > 256:           # climbing out hard
        return "departure"
    return "arrival"                                        # low + near + not climbing


def _route_plausible(featured: dict, route: dict) -> bool:
    """False if the aircraft can't be flying the adsbdb route (stale reused-callsign route).

    adsbdb is a static callsign→route cache; a reused callsign returns a wrong city pair
    (e.g. a long-haul pair like LAX→ICN for an aircraft really flying a short regional hop). We can't get
    the right route for free, but we can reject an impossible one: if the plane is far from
    BOTH endpoints AND outside the great-circle corridor between them, the pair is wrong.
    Direction-correction can't save this (both cities are wrong), so we drop the route.
    """
    o_lat, o_lon = route.get("origin_lat"), route.get("origin_lon")
    d_lat, d_lon = route.get("dest_lat"), route.get("dest_lon")
    lat, lon = featured.get("lat"), featured.get("lon")
    if None in (o_lat, o_lon, d_lat, d_lon, lat, lon):
        return True                                   # missing coords → can't judge, keep it
    d_o = haversine_km(lat, lon, o_lat, o_lon)
    d_d = haversine_km(lat, lon, d_lat, d_lon)
    seg = haversine_km(o_lat, o_lon, d_lat, d_lon)
    # On any point of a real route, dist→origin + dist→dest ≈ the route length (and stays
    # small near an endpoint). A wrong city pair (reused callsign) overshoots by thousands
    # of km. 800 km slack absorbs airway dog-legs + terminal maneuvering without false hits.
    return (d_o + d_d) - seg <= 800.0


def _correct_route_direction(featured: dict, airport: dict) -> None:
    """Flip a reversed adsbdb route using the plane's flight phase (mutates ``featured``).

    adsbdb routes are crowd-sourced and sometimes reversed; altitude + vertical rate near
    the field reliably say whether it's arriving or departing. If that contradicts which
    route end is home, flip. Conservative: only near home, only when one end IS home.
    """
    o, dst = featured.get("origin"), featured.get("destination")
    if not (o or dst):
        return
    phase = _flight_phase(featured)
    if phase is None:
        return
    home = home_codes(cfg)
    o_home = (o or "").upper() in home
    d_home = (dst or "").upper() in home
    if (phase == "arrival" and o_home and not d_home) or \
       (phase == "departure" and d_home and not o_home):
        featured["origin"], featured["destination"] = dst, o
        featured["route_corrected"] = True


async def _enrich_featured(featured: dict, client: httpx.AsyncClient, airport: dict) -> dict:
    """Enrich the featured flight: route + airframe backfill + duration estimate.

    Route comes from the callsign; airframe fields (type/reg/operator) are filled
    from adsbdb ONLY where the local aircraft.json didn't already provide them.
    Both lookups are cached in ``enrich``, so this never hammers the API.
    """
    featured = dict(featured)

    # --- Route (origin/destination/airline + coords for the duration estimate) --
    route: dict | None = None
    if featured.get("flight"):
        route = await route_for_callsign(featured["flight"].strip(), client)
        if route:
            featured["airline"] = route.get("airline")    # callsign→airline is reliable; keep it
            if _route_plausible(featured, route):
                featured["origin"] = route.get("origin")
                featured["destination"] = route.get("destination")
                # Fix adsbdb's direction with the plane's own geometry before classifying.
                _correct_route_direction(featured, airport)
            else:
                # Stale reused-callsign route — endpoints don't match where the plane is.
                # Drop it (the panel/UI fall back to type+reg) rather than show a wrong pair.
                featured["route_unreliable"] = True
            # Re-classify now that origin/destination are known (route-based match).
            featured = classify(featured, cfg)

    # --- Airframe backfill (adsbdb) ONLY for fields absent locally ---------------
    needs_backfill = not all(
        featured.get(k) for k in ("type", "type_desc", "registration", "operator"))
    if needs_backfill and featured.get("hex"):
        info = await aircraft_info(featured["hex"], client)
        if info:
            featured["type"] = featured.get("type") or info.get("type")
            featured["type_desc"] = featured.get("type_desc") or info.get("type_desc")
            featured["registration"] = featured.get("registration") or info.get("registration")
            featured["operator"] = featured.get("operator") or info.get("operator")
            # Keep local military bit (authoritative); adsbdb has none.

    # --- Duration (whole flight) + ETA (time to the home airport) ----------------
    featured["duration_est_min"] = _estimate_duration_min(featured, route)
    featured["duration_is_estimate"] = True
    featured["eta_min"] = _eta_to_airport_min(featured)

    featured["featured"] = True
    return featured


async def tick(client: httpx.AsyncClient) -> None:
    """One poll: fetch aircraft, annotate, classify, pick featured, enrich, broadcast."""
    global all_aircraft, last_featured_hex, latest_ws, active_runway_id

    r = await client.get(AIRCRAFT_JSON_URL, timeout=5)
    data = r.json()
    raw = data.get("aircraft", [])
    messages = data.get("messages")
    await watchdog.check(messages, client)

    airport = _airport_dict()
    rx_lat, rx_lon = receiver_pos()        # live GPS fix when available, else configured
    annotated = []
    for a in raw:
        ac = annotate(a, rx_lat, rx_lon, airport=airport)
        ac = classify(ac, cfg)
        ac["featured"] = False
        annotated.append(ac)
    _update_trails(annotated)              # build per-aircraft path trails for the live map

    # Landing-runway inference for arrivals on final + "passes my window" flag, then
    # roll up the airport's active landing runway from the recent arrivals.
    for ac in annotated:
        if ac.get("is_arrival") or (isinstance(ac.get("baro_rate"), (int, float))
                                    and ac["baro_rate"] < -200):
            rwy = infer_landing_runway(ac, airport, cfg.home_airport)   # confident final match
            if rwy:
                ac["landing_runway"] = rwy
                ac["window_visible"] = rwy in (cfg.visible_runways or [])
                _recent_runways.append(rwy)                 # only confident → active rollup
            elif active_runway_id and ac.get("is_arrival"):
                # vectored / not yet established on a final → the airport's active runway is
                # the best guess; flag it as a prior so it isn't logged as a confirmed landing.
                ac["landing_runway"] = active_runway_id
                ac["runway_prior"] = True
                ac["window_visible"] = active_runway_id in (cfg.visible_runways or [])
    active_runway_id = active_runway(list(_recent_runways))

    # Departure-runway inference: a plane classified as departing, climbing out aligned with a
    # runway (field behind). The runway is LOCKED per aircraft on the first confident match (the
    # straight initial climb) — once it turns onto its route the track no longer reflects the
    # departure runway, so we must not relabel it. Reuse window_visible for the panel/UI flag.
    for ac in annotated:
        if ac.get("landing_runway") or not ac.get("is_departure"):
            continue
        hx = ac.get("hex")
        rwy = _departure_runways.get(hx)
        if not rwy:
            rwy = infer_departure_runway(ac, airport, cfg.home_airport)
            if rwy and hx:
                _departure_runways[hx] = rwy        # lock it for the rest of this departure
        if rwy:
            ac["departure_runway"] = rwy
            ac["window_visible"] = rwy in (cfg.visible_runways or [])
    # Drop aircraft that have left coverage so the lock cache can't grow unbounded.
    _live_hexes = {a.get("hex") for a in annotated}
    for _hx in [h for h in _departure_runways if h not in _live_hexes]:
        del _departure_runways[_hx]

    featured = pick_featured(annotated, cfg, last_hex=last_featured_hex)
    if featured is not None:
        last_featured_hex = featured.get("hex")
        enriched = await _enrich_featured(featured, client, airport)
        # Reflect the enriched/featured flag back into the snapshot list.
        for i, ac in enumerate(annotated):
            if ac.get("hex") == enriched.get("hex"):
                annotated[i] = enriched
                break
        featured = enriched
    else:
        last_featured_hex = None

    if test_flight is not None:        # UI test-flight override (tune the display w/ fake data)
        featured = dict(test_flight)
    if test_clock:                     # UI clock-test override (force the idle flip-clock)
        featured = None

    # "Landed" detection: mark the featured arrival once it reaches the field, and remember
    # it briefly so the panel can flash LANDED even after it drops to ground / out of coverage.
    global _recent_landing
    if featured is not None and _landed(featured):
        featured["landed"] = True
        cs = (featured.get("flight") or "").strip()
        if cs and (_recent_landing is None or _recent_landing[0] != cs):
            _recent_landing = (cs, featured.get("landing_runway"), time.monotonic())

    all_aircraft = annotated
    state["featured"] = featured
    state["count"] = len(annotated)

    # --- Diagnostics ------------------------------------------------------------
    ranges = [a["distance_km"] for a in annotated if "distance_km" in a]
    diag["messages"] = messages if isinstance(messages, int) else 0
    diag["aircraft_count"] = len(annotated)
    diag["max_range_km"] = round(max(ranges), 2) if ranges else 0.0
    diag["gps_fix"] = gps.fix
    diag["gps"] = gps.status()
    diag["active_runway"] = active_runway_id

    # --- Live web-map payload: pushed over /ws each tick (real-time, no poll lag) --
    latest_ws = {
        "featured": featured,
        "count": len(annotated),
        "aircraft": annotated,
        "receiver": {"lat": rx_lat, "lon": rx_lon, "gps": gps.fix},
        "gps": gps.status(),
        "airport": airport,
        "watch": {
            "center_deg": cfg.watch.center_deg,
            "half_angle_deg": cfg.watch.half_angle_deg,
            "min_km": cfg.watch.min_km,
            "max_km": cfg.watch.max_km,
        },
        "proximity": {
            "enabled": cfg.proximity.enabled,
            "center_deg": cfg.proximity.center_deg,
            "half_angle_deg": cfg.proximity.half_angle_deg,
            "min_km": cfg.proximity.min_km,
            "max_km": cfg.proximity.max_km,
        },
        "display": _display_state(force_clock=test_clock),
        "runway": {"active": active_runway_id, "visible": cfg.visible_runways,
                   "list": [{"id": r["id"], "brg": r["brg"]}
                            for r in runways_for(cfg.home_airport)]},
        "landing": ({"callsign": _recent_landing[0], "runway": _recent_landing[1]}
                    if _recent_landing and time.monotonic() - _recent_landing[2] < LANDED_MSG_TTL
                    else None),
    }
    if history.enabled and test_flight is None:        # persist real traffic (not test data)
        await asyncio.to_thread(history.ingest, annotated, int(time.time()))
    await broadcast(latest_ws)


def _display_state(force_clock: bool = False) -> dict:
    """LED-panel command block pushed to the display over /ws (applied live).

    ``brightness`` is functional now; ``auto`` (time/sensor dimming) and ``flash``
    (one-shot notification) are wired hooks the display already understands but the
    app does not emit yet — so the panel features can be filled in without a protocol
    change.
    """
    p = cfg.panel
    return {
        "brightness": max(1, min(100, int(cfg.brightness))),
        "auto": bool(cfg.auto_brightness),
        "flash": None,
        # LED layout + scroll prefs — the display reads these fresh each frame.
        "layout": p.layout,
        "scroll_speed_px": p.scroll_speed_px,
        "scroll_gap_px": p.scroll_gap_px,
        "scroll_fields": p.scroll_fields,
        "cycle_seconds": p.cycle_seconds,
        "idle_behavior": "clock" if force_clock else p.idle_behavior,
        "idle_text": p.idle_text,
        "route_extra": p.route_extra,
    }


async def poll_loop() -> None:
    async with httpx.AsyncClient() as client:
        # Resolve the home airport coords once at startup (cache into cfg + /config)
        # so arrivals/departures + to_airport distances are anchored even if the
        # device emits no DB fields. Offline-safe: bundled dict, then optional fetch.
        await _resolve_home_airport(client)
        # Build the Airways/Navaids/Fixes overlay for this airport in the background — derived at
        # runtime from the configured airport (like coords + runways); the source is cached to
        # /config so it downloads once. No build-time variable or committed per-airport data.
        asyncio.create_task(navdata.ensure_navdata(cfg.airport_lat, cfg.airport_lon, client))
        while True:
            try:
                await tick(client)
            except Exception as exc:  # noqa: BLE001 — keep the loop alive
                print(f"[poll] {exc}")
            await asyncio.sleep(POLL_SECONDS)


async def _resolve_home_airport(client: httpx.AsyncClient) -> None:
    """Resolve cfg.home_airport → airport_lat/lon (if not cached) + its runways; persist.

    Both lookups hit OurAirports (public domain) once and cache to /config, so the geometry
    works for ANY configured airport with nothing hardcoded and no network at runtime after.
    """
    if not (cfg.airport_lat and cfg.airport_lon) or not cfg.airport_elev_ft:
        rec = await resolve_airport(cfg.home_airport, client)
        if rec:
            cfg.airport_lat = cfg.airport_lat or rec["lat"]
            cfg.airport_lon = cfg.airport_lon or rec["lon"]
            cfg.airport_elev_ft = cfg.airport_elev_ft or rec.get("elev_ft", 0.0)
            with contextlib.suppress(OSError):
                cfg.save()
    await resolve_runways(cfg.home_airport, client)   # true headings → /config cache


async def broadcast(payload: dict) -> None:
    dead = []
    for ws in list(clients):
        try:
            await ws.send_json(payload)
        except Exception:  # noqa: BLE001
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Seed the airband config on the shared volume before serving, so the airband
    # container (RTLSDRAIRBAND_CUSTOMCONFIG=true) never aborts on a missing file. Catch
    # everything + log: this must never abort startup, and a silent failure would leave
    # airband crash-looping invisibly.
    try:
        write_airband_conf(cfg.render_airband_conf())
        write_volume(cfg.volume)                # seed the speaker's volume file
    except Exception as exc:  # noqa: BLE001
        print(f"[airband] config seed FAILED: {exc}")
    history.connect()                           # open the SQLite flight history (optional)
    global active_runway_id                     # seed the active-runway prior from past landings
    for rwy in reversed(history.recent_landing_runways(40)):
        _recent_runways.append(rwy)
    active_runway_id = active_runway(list(_recent_runways))
    task = asyncio.create_task(poll_loop())
    gps_task = asyncio.create_task(gps.run())   # live receiver location from gpsd (if present)
    yield
    task.cancel()
    gps_task.cancel()
    for t in (task, gps_task):
        with contextlib.suppress(asyncio.CancelledError):
            await t
    with contextlib.suppress(Exception):
        history.close()             # final WAL checkpoint + close


app = FastAPI(title="flight-tracker", lifespan=lifespan)


@app.get("/api/current")
async def current() -> JSONResponse:
    """The featured flight + tracked count (same payload pushed over /ws)."""
    return JSONResponse(state)


@app.get("/api/aircraft")
async def aircraft() -> JSONResponse:
    """Every currently-tracked aircraft, annotated, plus receiver/airport/watch."""
    rx_lat, rx_lon = receiver_pos()
    return JSONResponse({
        "receiver": {"lat": rx_lat, "lon": rx_lon, "gps": gps.fix},
        "airport": _airport_dict(),
        "watch": {
            "center_deg": cfg.watch.center_deg,
            "half_angle_deg": cfg.watch.half_angle_deg,
            "min_km": cfg.watch.min_km,
            "max_km": cfg.watch.max_km,
        },
        "proximity": {
            "enabled": cfg.proximity.enabled,
            "center_deg": cfg.proximity.center_deg,
            "half_angle_deg": cfg.proximity.half_angle_deg,
            "min_km": cfg.proximity.min_km,
            "max_km": cfg.proximity.max_km,
        },
        "aircraft": all_aircraft,
    })


def _config_payload() -> dict:
    """The config as JSON plus the home airport's runway ids — used by GET and POST so
    the two never drift (a POST that omitted ``runways`` would wipe the UI picker)."""
    data = cfg.to_dict()
    data["runways"] = [r["id"] for r in runways_for(cfg.home_airport)]
    return data


@app.get("/api/config")
async def get_config() -> JSONResponse:
    """The full live Config as JSON, plus the home airport's runway ids (for the UI picker)."""
    return JSONResponse(_config_payload())


@app.post("/api/config")
async def post_config(request: Request) -> JSONResponse:
    """Merge a partial config from the body, persist, apply to the live cfg.

    If the home airport changed, re-resolve its coords (unless lat/lon were also
    supplied in the same request). Returns the new full config.
    """
    try:
        partial = await request.json()
    except (ValueError, TypeError):
        partial = {}

    prev_airport = cfg.home_airport
    cfg.merge(partial)

    # Re-resolve coords if the airport code changed and no explicit coords came in.
    if (cfg.home_airport != prev_airport
            and "airport_lat" not in (partial or {})
            and "airport_lon" not in (partial or {})):
        cfg.airport_lat = 0.0
        cfg.airport_lon = 0.0
        async with httpx.AsyncClient() as client:
            await _resolve_home_airport(client)

    with contextlib.suppress(OSError):
        cfg.save()
    return JSONResponse(_config_payload())


@app.get("/api/diag")
async def get_diag() -> JSONResponse:
    """Lightweight diagnostics: feed health + range + receiver position."""
    _rx = receiver_pos()        # call once (avoid a lat/lon split if the fix expires mid-build)
    return JSONResponse({
        "messages": diag["messages"],
        "aircraft_count": diag["aircraft_count"],
        "max_range_km": diag["max_range_km"],
        "gps_fix": gps.fix,
        "gps": gps.status(),
        "active_runway": diag.get("active_runway"),
        "receiver": {"lat": _rx[0], "lon": _rx[1], "gps": gps.fix},
    })


@app.get("/api/history")
async def get_history(limit: int = 50) -> JSONResponse:
    """Recent flights from the SQLite history (newest first)."""
    return JSONResponse({"flights": await asyncio.to_thread(history.recent_flights, limit)})


@app.get("/api/flight/{flight_id}/track")
async def get_flight_track(flight_id: int) -> JSONResponse:
    """The recorded position track for one flight (for replay on the map)."""
    return JSONResponse({"track": await asyncio.to_thread(history.flight_track, flight_id)})


@app.get("/api/airband")
async def get_airband() -> JSONResponse:
    """Airband (tower audio) stream status for the web UI player."""
    async with httpx.AsyncClient() as client:
        return JSONResponse(await airband_status(client))


@app.post("/api/airband/config")
async def post_airband_config(request: Request) -> JSONResponse:
    """Update scan freqs/gain, persist, write rtl_airband.conf, restart airband.

    Dedicated route (not folded into POST /api/config) because it has the heavy side
    effect of restarting the SDR + Icecast; the merge is validated in Config._merge_airband
    (clamps gain, drops bad rows, keeps last-good on an empty list)."""
    try:
        body = await request.json()
    except (ValueError, TypeError):
        body = {}
    cfg.merge({"airband": body})
    with contextlib.suppress(OSError):
        cfg.save()
    async with httpx.AsyncClient() as client:
        result = await apply_airband_config(cfg, client)
    result["airband"] = cfg.to_dict()["airband"]   # echo normalized state back to UI
    return JSONResponse(result)


@app.post("/api/volume")
async def post_volume(request: Request) -> JSONResponse:
    """Set the USB sound-card playback volume (0-100). The speaker applies it within ~2s
    (it polls the shared volume file) — no restart, so no audio drop."""
    try:
        body = await request.json()
    except (ValueError, TypeError):
        body = {}
    v = body.get("volume")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        cfg.volume = max(0, min(100, int(v)))
        write_volume(cfg.volume)
        with contextlib.suppress(OSError):
            cfg.save()
    return JSONResponse({"volume": cfg.volume})


@app.post("/api/airband/test-beep")
async def post_airband_test_beep() -> JSONResponse:
    """Restart the speaker container so it replays its startup test tone."""
    async with httpx.AsyncClient() as client:
        return JSONResponse(await test_beep(client))


@app.post("/api/matrix")
async def post_matrix(request: Request) -> JSONResponse:
    """Update LED PWM/timing tuning, persist, and restart the display to apply (init-time
    options can't be changed live). Values are clamped in Config._merge_matrix."""
    try:
        body = await request.json()
    except (ValueError, TypeError):
        body = {}
    cfg.merge({"matrix": body})
    with contextlib.suppress(OSError):
        cfg.save()
    async with httpx.AsyncClient() as client:
        restarted, msg = await restart_service(os.environ.get("DISPLAY_SERVICE", "display"), client)
    return JSONResponse({"ok": True, "matrix": cfg.to_dict()["matrix"],
                         "restarted": restarted, "detail": msg})


def _sample_test_flight() -> dict:
    """A complete fake featured flight that exercises every display field."""
    return {
        "hex": "test01", "flight": "TEST123", "origin": "JFK", "destination": "SEA",
        "airline": "Test Airlines", "operator": "Test Airlines", "type": "A320",
        "type_desc": "A320 214SL", "registration": "N320TS",
        "alt_baro": 4500, "gs": 220, "baro_rate": -640, "track": 10,
        "distance_km": 8.0, "distance_to_airport_km": 6.0, "bearing_from_me_deg": 200,
        "landing_runway": "01", "window_visible": True, "military": False,
        "is_arrival": True, "is_departure": False,
        "duration_est_min": 95, "duration_is_estimate": True, "eta_min": 4, "featured": True,
    }


@app.post("/api/test-flight")
async def set_test_flight(request: Request) -> JSONResponse:
    """Set/clear a fake featured flight for tuning the display. Body {"clear":true} turns
    it off; other body fields override the sample (e.g. {"landing_runway":"19"})."""
    global test_flight
    try:
        body = await request.json()
    except (ValueError, TypeError):
        body = {}
    if body.get("clear"):
        test_flight = None
        return JSONResponse({"active": False})
    test_flight = {**_sample_test_flight(),
                   **{k: v for k, v in (body or {}).items() if k != "clear"}}
    return JSONResponse({"active": True})


@app.post("/api/test-clock")
async def set_test_clock(request: Request) -> JSONResponse:
    """Force the idle flip-clock on the panel so it can be tested without waiting for an idle
    period. Body {"clear":true} turns it off; anything else turns it on."""
    global test_clock
    try:
        body = await request.json()
    except (ValueError, TypeError):
        body = {}
    test_clock = not body.get("clear")
    return JSONResponse({"active": test_clock})


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    clients.add(ws)
    await ws.send_json(latest_ws)
    try:
        while True:
            await ws.receive_text()   # keepalive; UI may push config later
    except WebSocketDisconnect:
        clients.discard(ws)


@app.get("/navdata.json")
async def navdata_json() -> FileResponse:
    """Serve the runtime-generated aviation overlay (from /config) if present, else the empty
    placeholder. Defined before the static mount so the generated file wins."""
    path = navdata.NAVDATA_OUT if os.path.exists(navdata.NAVDATA_OUT) else "static/navdata.json"
    return FileResponse(path, media_type="application/json")


# Static web UI. Mounted last so /api, /ws and /navdata.json take precedence.
app.mount("/", StaticFiles(directory="static", html=True), name="static")
