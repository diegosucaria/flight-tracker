# SPEC — ADS-B Approach Display

## 1. Goal

When you hear or see a plane out the window, the LED panel already shows **what it
is and where it's going** — so you can look up and know "that's the JetBlue from
Philadelphia, on approach, 6 km out and descending."

The airport is some distance away (e.g. ~10–20 km), in a known direction from the window.
Aircraft line up on approach in that direction before turning to final, plus the occasional
overflight from other directions.

### Non-goals (v1)
- Not a full radar/map replacement (tar1090 already does that — we run it for setup/debug).
- Not feeding aggregators (FlightAware/ADSBexchange) — optional later, trivial to add.
- Not tracking every aircraft on screen — the panel shows **one featured flight** at a time.

---

## 2. Primary use case

> *I hear a plane. I glance at the panel. It shows the callsign, the FROM › TO airports,
> an arrow pointing to where the plane is relative to me, its altitude/descent, and how
> long the flight has been going.*

Supporting use cases:
- **Configure once:** set my location (auto via GPS, or pin on a map) and define the
  patch of sky I care about (the approach corridor toward the airport).
- **A/B the antenna:** compare indoor vs outdoor, and omni vs directional, from the UI
  (message rate, aircraft count, max range).
- **Hear the tower:** when a plane is in the watch area, play the tower/approach frequency
  through the speaker (§3.8).
- **Idle behaviour:** when no aircraft is in the watched area, show a clock / "no traffic"
  / last seen flight.

---

## 3. Functional requirements

### 3.1 Receive & decode
- Receive 1090 MHz ADS-B via the SDR and decode to a live aircraft list (position,
  altitude, ground speed, track/heading, vertical rate, callsign, ICAO hex, squawk, RSSI).
- Standard tool: **readsb** (inside the `ultrafeeder` image), exposing `aircraft.json`.

### 3.2 Two sets of coordinates
- **Receiver location** (where the device/antenna is): from **GPS** (`gpsd`) when available,
  else a value set in the web UI / `READSB_LAT/LON`. Used for distance + bearing *from you*.
- **Home airport** (the field you watch, e.g. SEA/KSEA): its lat/lon, **resolved from
  OurAirports by the configured `HOME_AIRPORT` ICAO/IATA code** at startup (override via
  `AIRPORT_LAT/LON`). Used for the *arrivals* filter (§3.5) and the *distance-to-airport* readout.
- **Display distance mode** (a setting): show distance **from you**, **to the airport**, or
  **both** — chosen once it's on the panel.

### 3.3 Filter to the watched area
- Compute, per aircraft, **distance and bearing from the receiver** (haversine + initial
  bearing).
- Keep only aircraft inside the user-defined **watch area**, expressed as either:
  - a **bearing sector** (center bearing ± half-angle) with min/max distance, or
  - a **polygon** drawn on the map.
- The corridor toward the airport is the typical sector.

### 3.4 Enrich
- **Route (FROM › TO):** look up the callsign → origin/destination airports via a route
  database (adsbdb / adsb.lol; see §6). Cache aggressively — routes rarely change.
- **Aircraft type & registration:** ICAO hex → type/registration (adsbdb or local DB).
- **Airline:** derive from callsign prefix (e.g. `JBU` → JetBlue) for a label/logo.
- **Flight time / duration:** **estimated** — see honesty note in §6. Options: elapsed
  since first seen overhead, or great-circle origin→destination ÷ typical groundspeed,
  or a scheduled-time API later.

### 3.5 Pick the featured flight ("the one I hear")
- **Traffic mode** (a setting — what to consider):
  - `all nearby` (default) — anything in the watch area you might hear/see.
  - **`arrivals` — landing at the home airport:** destination == home airport (from the
    route lookup, e.g. `XXX>SEA`) **OR** *behavioural approach detection*: descending +
    low + distance-to-airport shrinking + roughly runway-aligned (works even with no route
    data). This is the "planes I see turning to final from the window" set.
  - `departures` — origin == home airport, or climbing out near the field.
- Among the filtered aircraft, pick by a rule, default **lowest + closest** (the plane you
  actually hear is low and near), with hysteresis so the display doesn't flip between two.
  Other rules: *closest*, *strongest signal*, *on final approach*.
- Needs a configured **home airport** (ICAO/IATA code); its lat/lon is resolved automatically.

### 3.6 Drive the display
- Render the featured flight on the 64×32 matrix (layout in §4), update ≥2 Hz for the
  arrow, refresh text on flight change. Brightness day/night schedule. Graceful "idle"
  screen.

### 3.7 Web configuration UI
- Set/confirm **location** (use GPS button, or drag a map pin).
- Draw/define the **watch area** (sector or polygon) on a Leaflet map with a live
  aircraft overlay.
- Choose the **traffic mode** (all nearby / arrivals to home / departures), the
  **featured-flight rule**, **display fields**, and **brightness/schedule**.
- **Live preview** mirroring the matrix (a matrix-sized canvas in the browser).
- **Diagnostics:** message rate, aircraft count, max range, SDR gain, GPS fix — for the
  antenna A/B test.

### 3.8 Tower comms (VHF airband) — optional subsystem
- A **second SDR** runs `rtl_airband` monitoring configured airport frequencies (TWR / APP
  / ATIS) in the 118–137 MHz AM band, with squelch, output to the **USB sound card → speaker**.
- The web UI lists the frequencies and lets you enable/disable channels and set volume.
- Optional coordination: only un-mute / raise volume when the featured aircraft is in the
  watch area, so it stays quiet until something is actually arriving.
- Audio **must** use the USB sound card — onboard audio is disabled for the matrix.
- **Legality is per-country** (see [HARDWARE caveats](HARDWARE.md#caveats--gotchas)) — check your local rules before enabling.

---

## 4. Display content design (64×32)

A single 64×32 P4 panel. With only 64 px of width the panel uses compact, partly-scrolled
layouts rather than a fixed dashboard: callsign + a direction arrow, the route (FROM › TO),
and a rotating detail row (altitude/▼rate, speed, distance, ETA). The renderer ships several
layouts — **compact / hybrid / big / scrolling ticker** — selectable in the UI.

```
64 × 32  (single panel)
┌────────────────────────────────┐
│       ✈ JBU760           ◆►     │   callsign + where-to-look arrow
│   PHL ──► BOS                   │   route (scrolls if too wide)
│   4200ft ↓768   320kt   6.2km   │   rotating detail row
└────────────────────────────────┘
```

Notes:
- **The arrow is the key feature** — it points in the real-world compass direction of the
  aircraft *from your window*, so "I hear it → glance → look that way."
- **Wider display (optional):** chain a 2nd 64×32 panel (`MATRIX_CHAIN=2` → 128×32) for a
  roomier ticker; a **stacked 64×64** gives a square compass-over-text layout instead.
- Idle (no aircraft in the watch area): clock + "no traffic" + last-seen flight.
- Richer fields (airline logo, aircraft photo, scheduled time) can be added if a data
  source provides them.

---

## 5. Non-functional requirements

- **Reliability:** survives reboots and power cuts unattended (balena auto-restart,
  read-mostly config, RTC optional via the Matrix HAT).
- **Remote update:** push new container releases over-the-air via balena; no need to
  physically touch the Pi.
- **Power:** single mains feed; 5 V supply drives the panel (and optionally the Pi). See
  power budget in [HARDWARE.md](HARDWARE.md#power--wiring).
- **Portability of config:** location + watch area live in a config volume, editable from
  the UI, exportable.
- **No secrets in the image:** any API keys via balena device/fleet variables.

---

## 6. Data sources & honesty notes

| Data | Source | Notes |
|------|--------|-------|
| Position, alt, speed, track, callsign, hex | **ADS-B itself** (decoded by readsb) | Always available, free, local. |
| Route (FROM › TO) | **[adsbdb.com](https://www.adsbdb.com/)** `/v0/callsign/{cs}`, and/or **[adsb.lol](https://api.adsb.lol/docs)** routeset | Free, no key. Good airline coverage; **patchy for general aviation**. Cache results. |
| Aircraft type / registration | adsbdb `/v0/aircraft/{hex}`, or local tar1090 DB | Free. |
| Airline name/logo | Callsign prefix table | Local lookup. |
| **Flight duration** | **Estimated** — not broadcast | ADS-B carries **no route or schedule**. We estimate from elapsed time and/or origin→destination distance ÷ groundspeed. True scheduled duration needs a paid schedule API (e.g. FlightAware AeroAPI). **Label it as an estimate on screen.** |

> **Be clear with yourself on this:** "FROM › TO" and "duration" are *derived/enriched*,
> not received off the air. Coverage is excellent for scheduled airliners (your approach
> traffic) and weak for private/GA. The arrow, altitude, speed, callsign and distance are
> always exact because they come straight from the aircraft.
