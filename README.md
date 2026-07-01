# ✈ flight-tracker

A Raspberry Pi that watches the aircraft around **your** airport and shows the single most
relevant flight on an RGB LED matrix — **callsign, route (FROM › TO), a direction arrow telling
you where to look, altitude, and time-to-airport** — so when you hear a plane you glance at the
panel and know what it is. It also serves a live **map web UI** (with an aviation-chart overlay),
keeps **flight history**, and can play **tower-comms audio** as a plane arrives.

Everything is configured for your location at runtime — set your airport's ICAO code and the
receiver's position, and the airport coordinates, runways, watch sector and map all follow. No
code is specific to any one airport.

Runs as a few Docker containers on **[balenaOS](https://www.balena.io/os)**, managed from a
small web UI.

```
  ANTENNAS (window/balcony)        SDRs (USB)        Raspberry Pi · balenaOS · docker-compose
  ┌────────────────────────┐ 1090 ┌────────────┐
  │ 1090 MHz omni           │─────►│ Airspy Mini│─┐
  └────────────────────────┘      └────────────┘ │  ┌────────────────────────────────┐
  ┌────────────────────────┐ 118– ┌────────────┐ ├─►│ ultrafeeder ─► app ──► display  │
  │ airband dipole (opt.)   │─137─►│ RTL-SDR    │─┘  │  (decode)   (enrich·filter·    │
  └────────────────────────┘ MHz  └────────────┘    │   gpsd ──►   pick the plane·UI) │
                                                     │      rtl_airband ─► USB audio   │
                                                     └────────┼────────────┼──────────┘
                                                      HUB75+5V│            │USB
                                                       ┌──────▼───────┐ ┌──▼────┐
                                                       │ 64×32 panel  │ │🔊 spkr│
                                                       └──────────────┘ └───────┘
```

## Features

- **Picks the one flight you'd notice** — nearest/lowest in a configurable watch sector, with
  hysteresis so it doesn't flicker between planes.
- **Route + enrichment** — FROM › TO city pair, aircraft type, operator, registration, military
  flag, looked up from open route APIs and cached.
- **Runway awareness** — infers the active landing runway from ADS-B tracks and tells you whether
  an arrival passes your window or lands on the far side. Runway geometry is resolved at runtime
  for your airport (no hardcoded tables).
- **Live map web UI** — Leaflet map with aircraft, trails, your watch sector, runways + extended
  centerlines, a **Weather (METAR)** layer showing airport wind + the head/cross-wind on each
  runway (which one the wind favours), and toggleable **Airways / Navaids / Fixes** overlays.
- **Flight history** — every observed flight saved to SQLite; browse, filter, and replay tracks.
- **Tower-comms audio** *(optional)* — a 2nd SDR runs `rtl_airband` over your airport's VHF
  frequencies and plays it to a USB speaker.
- **LED panel** — a 64×32 HUB75 panel with several layouts, tunable live from the UI.

## How it works

| Container | What it does |
|-----------|--------------|
| **`airspy`** | Decodes ADS-B from the Airspy SDR and forwards Beast messages to `ultrafeeder`. |
| **`ultrafeeder`** | [sdr-enthusiasts](https://github.com/sdr-enthusiasts) image — computes positions and publishes a live map (tar1090) + `aircraft.json`. |
| **`app`** | The brain + web UI (FastAPI). Polls `aircraft.json`, computes distance/bearing from your location, looks up routes, picks the featured flight, serves the UI/REST/WebSocket, and resolves your airport's coords + runways from [OurAirports](https://ourairports.com) at startup. |
| **`display`** | Renders the featured flight on the LED panel ([hzeller rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix), PIO backend on the Pi 5). |
| **`gpsd`** | *(optional)* Live receiver location from a USB GPS. |
| **`airband`, `airband-speaker`** | *(optional)* VHF tower audio via a 2nd SDR + USB sound card. |

## Bill of materials

Approximate USD; every part is sold worldwide. See **[docs/HARDWARE.md](docs/HARDWARE.md)** for a
detailed buying guide with specific products, caveats, and example retailer links.

**Core build**

| Part | Role | Qty | ~USD |
|------|------|----:|-----:|
| Raspberry Pi 5 (2 GB+) | The computer | 1 | 60–80 |
| **Airspy Mini** SDR | 1090 MHz ADS-B receiver (great overload handling at close range) | 1 | 100–130 |
| 1090 MHz omni antenna + coax | Receives aircraft | 1 | 40–70 |
| **Adafruit 64×32 RGB LED matrix (P4)** | The display | 1 | 40 |
| Adafruit RGB Matrix Bonnet | Drives the panel from the Pi | 1 | 20 |
| 5 V / 10 A PSU (e.g. Mean Well LRS‑50‑5) | Powers the panel | 1 | 13 |
| Official Pi 5 27 W USB‑C PSU | Powers the Pi (full USB budget for the SDRs) | 1 | 12 |
| microSD 32 GB+ (A2) | Boots balenaOS | 1 | 9 |
| **Core subtotal** | | | **~300** |

**Optional add‑ons**

| Part | Role | ~USD |
|------|------|-----:|
| VK‑162 USB GPS (u‑blox) | Auto‑locates the receiver (else set lat/lon by hand) | 15 |
| RTL‑SDR Blog V3 kit (dongle + dipole) | 2nd SDR for VHF tower comms | 35 |
| USB sound card + small powered speaker | Audio out (onboard audio is taken by the panel) | 25 |

> A budget build (RTL‑SDR instead of Airspy, a basic dipole antenna, no airband/GPS extras)
> runs closer to **~$120**. Want a wider display? Chain a 2nd 64×32 panel and set `MATRIX_CHAIN=2`.

## Setup

You need a free [balenaCloud](https://www.balena.io) account (or any way to run
`docker-compose` on the Pi).

1. **Create a fleet** in balenaCloud (Raspberry Pi 5 / 64‑bit), add a device, and flash the
   downloaded balenaOS image to the microSD ([balenaEtcher](https://etcher.balena.io)).
2. **Set variables** (Device/Fleet → Variables) — *optional*: everything here can also be set
   in the web UI. **Any variable you set overrides the matching UI field** (which then greys out
   with a 🔒). The common ones to get started:
   | Variable | Example | Meaning |
   |----------|---------|---------|
   | `HOME_AIRPORT` | `KSEA` | Your airport's ICAO — coords + runways are resolved from it. |
   | `READSB_LAT` / `READSB_LON` | `47.45` / `-122.31` | Receiver position (fallback if no GPS). |
   | `READSB_ALT` | `40m` | Receiver altitude. |
   | `TZ` | `America/Los_Angeles` | Your timezone. |

   The full list — including how to pin the airport coordinates and route API — is in
   **[Environment variables](#environment-variables)** below.
3. **Deploy:** clone this repo and `balena push <you>/<fleet>` (or wire up the GitHub Action
   below). The containers build and start.
4. **Configure in the browser:** open `http://<device-ip>` and set your watch sector, display
   layout, traffic mode, and (optional) airband frequencies. Everything is live.
5. **Aviation overlay:** the Airways/Navaids/Fixes layers build themselves on first boot from
   your configured airport (nothing to do) — details below.

### Wiring (quick notes)

- Panels are powered from the **5 V PSU directly** (not through the Pi). Keep leads short/thick.
- On the Pi 5, the panel uses the hardware **PIO** backend; no GPIO solder mod is needed.
- Two SDRs draw real current — use the **27 W PSU** (or a powered USB hub) to avoid brownouts.
- Full wiring, antenna placement, and connector notes are in [docs/HARDWARE.md](docs/HARDWARE.md).

### Aviation map overlay

The **Airways / Navaids / Fixes** layers are generated **automatically at runtime** from your
configured airport: on startup the app fetches open navdata once, caches it to `/config`, and
serves the overlay — no API key, no committed per-airport data, nothing to configure. (To
pre-generate it offline instead — e.g. a device with no internet — run the same builder locally;
it writes `app/static/navdata.json`:)

```bash
python3 tools/build_navdata.py KSEA          # by ICAO
python3 tools/build_navdata.py --lat 47.45 --lon -122.31
```

> Source data is the open X‑Plane navdata (~2012 cycle), so a few recent terminal waypoints may
> differ from current charts; the enroute airway network is stable.

### Auto‑deploy with GitHub Actions (optional)

[`.github/workflows/balena-deploy.yml`](.github/workflows/balena-deploy.yml) pushes a release on
every push to `main`. In your repo settings add:

- Secret **`BALENA_TOKEN`** — a balenaCloud API key.
- Variable **`BALENA_FLEET`** — your fleet slug, e.g. `youruser/flight-tracker`.

## Environment variables

Every app setting can be configured in the **web UI** and is stored on the device. Setting the
matching **environment variable overrides the UI** for that field — the UI then shows it
read-only (greyed out, 🔒). Leave a variable unset to manage it from the browser. Most people set
none of these beyond the getting-started ones and configure everything in the UI.

**App configuration** — override the corresponding UI field when set:

| Variable | UI field | Meaning |
|----------|----------|---------|
| `HOME_AIRPORT` | Home airport | Airport ICAO; coords + runways resolve from [OurAirports](https://ourairports.com). |
| `READSB_LAT`, `READSB_LON` | Receiver lat / lon | Receiver position (live GPS overrides this when enabled). `RECEIVER_LAT` / `RECEIVER_LON` are also accepted. |
| `AIRPORT_LAT`, `AIRPORT_LON`, `AIRPORT_ELEV_FT` | Airport lat / lon | Pin the airport location/elevation instead of resolving it from the ICAO. |
| `ROUTE_API` | — | Route-enrichment source (`adsbdb`, the default). |
| `READSB_ALT` | — | Receiver altitude (used by the decoder). |
| `TZ` | — | Timezone, e.g. `America/Los_Angeles`. |

**Deploy** (GitHub Action — see below):

| Variable | Meaning |
|----------|---------|
| `BALENA_TOKEN` *(secret)* | balenaCloud API key. |
| `BALENA_FLEET` | Fleet slug, e.g. `youruser/flight-tracker`. |

**Advanced / internal** (sensible defaults; rarely changed): `SHOW_PANEL_TUNING` (set to reveal
the LED **Panel tuning** card — the PWM/flicker knobs, hidden by default), `POLL_SECONDS`,
`AIRCRAFT_JSON_URL` (point the app at a tar1090 feed for local dev), `GPSD_HOST` / `GPSD_PORT`,
`CONFIG_PATH`, `NAVDATA_PATH`, `HISTORY_DB_PATH` / `HISTORY_POSITION_DAYS`, plus the airband/watchdog
service knobs. See [`.env.example`](.env.example) for the annotated full set.

## Local development

The `app` service runs without any hardware (it just needs an `aircraft.json` feed):

```bash
cd app
pip install -r requirements.txt
HOME_AIRPORT=KSEA AIRCRAFT_JSON_URL=http://<a-tar1090-host>/data/aircraft.json \
  uvicorn app.main:app --reload --port 8080
# open http://localhost:8080
```

Copy [`.env.example`](.env.example) to `.env` for the full set of variables.

## Repo layout

| Path | What |
|------|------|
| [`app/`](app/) | FastAPI brain + web UI: poll → enrich → pick the featured flight → REST/WS. |
| [`display/`](display/) | LED-matrix renderer (subscribes to `app`). |
| [`airband/`](airband/) | `rtl_airband` config template for the tower-comms SDR. |
| [`tools/build_navdata.py`](tools/build_navdata.py) | Generates the map aviation overlay for an airport. |
| [`docker-compose.yml`](docker-compose.yml) | The balena multi-container stack. |
| [`.env.example`](.env.example) | Configuration template. |
| [`docs/`](docs/) | [SPEC](docs/SPEC.md) · [ARCHITECTURE](docs/ARCHITECTURE.md) · [HARDWARE](docs/HARDWARE.md) · [PRIOR-ART](docs/PRIOR-ART.md). |

## Credits & data

- **[OurAirports](https://ourairports.com)** — airport + runway data (public domain).
- **X‑Plane navdata** ([ptsmonteiro/x-plane-navdata](https://github.com/ptsmonteiro/x-plane-navdata), GPL‑3.0) — airways/navaids/fixes overlay (generated locally; not redistributed here).
- **[hzeller/rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix)** — LED panel driver.
- **[sdr-enthusiasts](https://github.com/sdr-enthusiasts)** — the ultrafeeder, airspy_adsb, and rtl_airband container images.
- Route enrichment via [adsbdb](https://www.adsbdb.com) / [adsb.lol](https://api.adsb.lol).

> ⚠️ **Airband legality is per‑country** — listening to aviation radio is restricted in some
> places (e.g. Germany). Check your local rules before enabling the tower-comms feature.

## License

[MIT](LICENSE) © flight-tracker contributors.
