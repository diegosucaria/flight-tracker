# CLAUDE.md — flight-tracker

Project instructions for AI sessions. Kept **PII-free** (no real airport, coordinates, device,
network, or personal details) so it is safe in this public repo. Use placeholders in all
examples: `KSEA`, `47.45 / -122.31`, `example.com`.

> **This is a personal Raspberry Pi hobby project** — a Python + JS app running on balenaOS.
> There is no cloud, server, or infrastructure component to design or deploy.

## What it is
A Raspberry Pi 5 watches the aircraft around a **configurable** home airport via ADS-B and shows
the single most relevant flight on a **64×32 HUB75 RGB LED matrix** (callsign, route FROM›TO, a
direction arrow, altitude, ETA). It also serves a live **Leaflet web map UI**, keeps **flight
history** in SQLite, and can play **VHF tower audio** to a USB speaker. Runs as Docker containers
on **balenaOS**.

**Airport-agnostic — nothing is hardcoded to any one airport.** The home airport (ICAO),
receiver position, airport coords/runways, watch sector, and map overlays are all resolved at
**runtime** from config + open data. Keep it that way; never commit location- or person-specific
data.

## Architecture (containers, `docker-compose.yml`)
- **`airspy`** — decodes ADS-B from an Airspy SDR → Beast to ultrafeeder.
- **`ultrafeeder`** — sdr-enthusiasts image; computes positions, serves tar1090 + `aircraft.json`.
- **`app`** — the brain + web UI (**FastAPI**). Polls `aircraft.json`, enriches route/airframe,
  picks the featured flight, serves REST/WS + the static UI, resolves airport coords, runways,
  and the navdata overlay at startup.
- **`display`** — renders the featured flight on the LED panel using **hzeller rpi-rgb-led-matrix,
  PIO backend on the Pi 5** (NOT Adafruit PioMatter). Subscribes to the app over WS.
- **`gpsd`** *(optional)* — live receiver position from a USB GPS.
- **`airband` / `airband-speaker`** *(optional)* — VHF tower audio via a 2nd SDR (`rtl_airband`
  + a bundled Icecast) → USB sound card.

## Repo layout
- `app/app/` — FastAPI backend. Key modules: `main.py` (poll loop `tick()`, endpoints,
  featured enrichment), `selector.py` (featured pick, watch + proximity sectors, arrival/
  departure `classify`), `config.py` (dataclass config, persistence, env overrides),
  `runways.py` (runtime runway inference), `airports.py` (OurAirports resolve), `navdata.py`
  (airways/navaids/fixes overlay), `metar.py`, `flights.py` (arrivals/departures),
  `openaip.py` (airspace), `enrich.py` (routes), `history.py` (SQLite), `geo.py`, `gps.py`,
  `watchdog.py`.
- `app/static/` — web UI: `app.js` (Leaflet map + all layers + featured card + config panel),
  `index.html`, `style.css`.
- `display/display.py` — LED panel renderer.
- `airband/` — `rtl_airband` config template. `tools/build_navdata.py` — offline overlay builder.
- `docs/` — SPEC, ARCHITECTURE, HARDWARE, PRIOR-ART. `.github/workflows/balena-deploy.yml`.
- A companion 3D-printed **enclosure** (OpenSCAD) lives in a sibling directory, tracked separately.

## Config & key concepts
- Config is a dataclass (`config.py`), persisted as JSON on the `app_config` balena volume
  (`/config/config.json`). `load()` merges persisted values over code defaults **then applies env
  overrides**. Editable live from the web UI (`POST /api/config`).
- **Env vars override the UI** when set (`HOME_AIRPORT`, `READSB_LAT/LON`, `AIRPORT_LAT/LON/ELEV_FT`,
  `ROUTE_API`); the matching UI field goes read-only (exposed as `_env_locked`).
- **Featured flight** = lowest + closest inside the **watch sector**, with hysteresis. A close/low
  **proximity sector** overrides the clutter filters (hide-no-callsign / hide-GA) and takes
  priority — the "it passed my window" case.
- **Map layers** (all toggleable, top-right): Watch sector, Proximity, Runways, Flight trails,
  Weather (METAR + per-runway wind), Flights (recent OpenSky *or* scheduled AeroDataBox),
  ADS-B coverage (reception envelope from history, presets + custom date range), Airspace
  (OpenAIP), Airways / Navaids / Fixes. Corner "boxes" are Leaflet controls.
- **Data sources** (open / free-tier): OurAirports (airports, runways, **navaids** — public
  domain), X-Plane navdata (fixes/airways, GPL, ~2012 cycle), aviationweather.gov (METAR),
  OpenSky (recent arr/dep, free), AeroDataBox (scheduled arr/dep, **keyed**), OpenAIP (airspace,
  **keyed**), adsbdb / adsb.lol (route enrichment). No paid AIRAC navdata (Navigraph et al.).

## Environment variables (see `.env.example` + README §Environment variables)
- App (override the UI): `HOME_AIRPORT`, `READSB_LAT`/`READSB_LON`, `READSB_ALT`,
  `AIRPORT_LAT`/`AIRPORT_LON`/`AIRPORT_ELEV_FT`, `ROUTE_API`, `TZ`.
- Keyed features: `FLIGHTS_API_KEY` (RapidAPI → AeroDataBox scheduled flights),
  `OPENAIP_API_KEY` (airspace layer).
- Advanced: `SHOW_PANEL_TUNING` (reveal the PWM panel-tuning card), `POLL_SECONDS`,
  `AIRCRAFT_JSON_URL`, `GPSD_HOST/PORT`, `CONFIG_PATH`, `NAVDATA_PATH`, `HISTORY_*`.
- Deploy: `BALENA_TOKEN` (secret), `BALENA_FLEET`.

## Dev & deploy
- **Local dev** (no hardware): `cd app && pip install -r requirements.txt &&
  HOME_AIRPORT=KSEA AIRCRAFT_JSON_URL=http://<a-tar1090-host>/data/aircraft.json
  uvicorn app.main:app --reload --port 8080`.
- **Deploy = push to `main`** → the "Deploy to balena" GitHub Action builds + pushes a release to
  the fleet. **main = production; there is no staging.** Commit/push **only when the user asks**,
  and keep every commit message / PR body **PII-free**.
- **On-device verification** is done over balena host SSH + `balena-engine exec <app-container> …`
  (connection details live in the private session memory, not committed here). After the deploy
  Action succeeds the device still takes **~1–2 min to pull the new image** — poll for the change
  (e.g. `grep` a new symbol in the container) before verifying.

## Verification workflow & gotchas
- **Validate before deploy:** `python3 -m py_compile <files>` (backend) and
  `node --check app/static/app.js` (frontend). Prefer inline python heredoc unit tests for pure
  logic; deploy + on-device check for anything hardware/data/network-dependent.
- New map **corner-box layers must kick an initial fetch on page load** — the layer control's
  `overlayadd` does **not** fire for layers restored from saved prefs (localStorage).
- `config.load()` **force-resets the airband mountpoint** to the code default (a persisted rename
  silently de-syncs the Icecast server from the speaker client).
- Airband auto-apply must update **only the summary line, never rebuild the freq rows** — a
  rebuild steals input focus mid-typing.
- **Keyed integrations** (AeroDataBox, OpenAIP) can't be tested without the user's key — implement
  defensively (`.get()` everywhere, graceful empty), deploy, and verify with the user; be ready to
  fix field mappings from a sample response.
- macOS/BSD `git grep` does **not** support `\b` (silently matches nothing) — use `-w` or a
  substring when scrubbing for leaked strings.
- Departure phase (`_departure_phase` in `main.py`): TAKING OFF → TOOK OFF → gone is
  **distance-first** (flips once >3.5 km from the field or past ~1200 ft AGL), so slow climbers /
  bad altitude reads don't get stuck.
