# ARCHITECTURE

## Container layout (balena, docker-compose)

Services on the default balena bridge network, reaching each other by service name (plus an
`airband-speaker` helper that plays the airband stream to the USB sound card).

```
┌──────────┐ Beast  ┌──────────────┐   aircraft.json   ┌──────────────┐
│ airspy   │─30005─►│ ultrafeeder  │──────(HTTP)──────►│ app          │
│(airspy_  │        │ readsb(net)+ │◄── tar1090 map ───│ enrich+filter│
│ adsb)    │        │ tar1090      │    (setup/debug)  │ + web + REST │
└──────────┘        └──────┬───────┘                   │ + WebSocket  │
  Airspy · 1090            │ location                   └──────┬───────┘
                      ┌────▼─────┐                             │ WS
┌──────────┐ audio    │ gpsd     │                      ┌──────▼───────┐
│ airband  │─►USB ─►🔊 │(USB GPS) │                      │ display      │
│(rtl_     │  sound    └──────────┘                      │ LED driver   │
│ airband) │  card                                       │ (privileged) │
└──────────┘ RTL-SDR · 118–137 MHz                       └──────┬───────┘
                                                          HUB75 │ + 5V
                                                        ┌───────▼────────┐
                                                        │  64×32 panel   │
                                                        └────────────────┘
```

### Services

| Service | Image | Role | Privilege / devices |
|---------|-------|------|---------------------|
| `airspy` | `ghcr.io/sdr-enthusiasts/airspy_adsb` | Decode 1090 from the Airspy Mini, send Beast to `ultrafeeder:30005`. | USB device / privileged |
| `ultrafeeder` | `ghcr.io/sdr-enthusiasts/docker-adsb-ultrafeeder` | readsb in **net-only** mode (ingests Beast from `airspy`), serves `tar1090` map + `aircraft.json`. | — |
| `airband` | `sdr-enthusiasts/docker-rtlsdrairband` | Run `rtl_airband` on the **2nd SDR (RTL-SDR)**, monitor TWR/APP/ATIS, output audio to the USB sound card. | USB (RTL-SDR by serial) + sound device |
| `gpsd` | `ghcr.io/sdr-enthusiasts/docker-gpsd` (or `gpsd` base) | Read USB GPS, serve location on tcp 2947. | `/dev/ttyACM0` |
| `app` | custom (Python / FastAPI) | The brain + web UI: poll `aircraft.json`, compute distance/bearing, filter to watch area, enrich (route/type), pick the featured flight, serve REST + WebSocket + config UI. | none |
| `display` | custom (`rpi-rgb-led-matrix`) | Subscribe to `app` over WS, render the featured flight on the matrix. | **privileged**, `/dev/mem`, GPIO |

> *If you start with an RTL-SDR for 1090 instead of the Airspy, drop the `airspy` service
> and let `ultrafeeder` read the dongle directly — but then it needs its own serial to stay
> distinct from the airband dongle.*

> Why `app` and `display` are split: the display needs a **privileged** container for GPIO
> timing; keeping the web/enrichment logic in an unprivileged container is cleaner and lets
> the display restart independently. They talk over a WebSocket (`ws://app:8080/ws`).

---

## Data flow

1. **Decode.** `ultrafeeder` produces `aircraft.json` (~1 Hz) at
   `http://ultrafeeder/data/aircraft.json`.
2. **Locate.** `app` reads receiver lat/lon from `gpsd` (fallback: configured value).
3. **Geo-filter.** For each aircraft: haversine **distance** + initial **bearing** from
   the receiver; keep those inside the configured **watch sector/polygon**.
4. **Enrich** (cached): callsign → route (adsbdb/adsb.lol), hex → type/registration,
   callsign prefix → airline. Duration is **estimated** (see [SPEC §6](SPEC.md#6-data-sources--honesty-notes)).
5. **Select** the featured flight (default: lowest + closest, with hysteresis).
6. **Publish** the featured-flight object over WebSocket + REST `/api/current`.
7. **Render.** `display` draws it; updates the arrow ≥2 Hz, text on change.

Featured-flight object (draft):
```json
{
  "hex": "a1b2c3", "callsign": "JBU760", "airline": "JetBlue",
  "origin": "PHL", "destination": "BOS",
  "alt_ft": 4200, "vert_fpm": -768, "gs_kt": 320, "track_deg": 218,
  "distance_km": 6.2, "bearing_from_me_deg": 47, "type": "A320",
  "duration_est_min": 72, "duration_is_estimate": true,
  "rssi": -8.1, "seen_s": 0.4
}
```

---

## balena host configuration

Set as **fleet/device variables** in the balena dashboard:

| Variable | Value | Why |
|----------|-------|-----|
| `BALENA_HOST_CONFIG_dtparam` | `audio=off` | Frees the PWM hardware the matrix uses → less flicker. |
| (build label) `io.balena.features.dbus` etc. | as needed | gpsd/udev access. |

Display library by board: **Pi 5 → hzeller `rpi-rgb-led-matrix`, PIO backend** (`rp1_pio=1`,
via `/dev/pio0`; the RP1 hardware-times the panel, so no GPIO4↔18 jumper and no `isolcpus` —
needs a recent Pi 5 EEPROM) — our target. **Pi 3/4 → the same library, classic mode**, whose
flicker-free "quality" mode wants the **GPIO4↔GPIO18 jumper** soldered on the Bonnet and ideally
a dedicated core (`isolcpus=3`). Either way, **fine to start simple and tune later** if you see flicker.

USB access: simplest is `privileged: true` on the SDR/GPS containers; tighten to explicit
`devices:`/udev rules once it works.

**Audio:** because onboard audio is off, the **USB sound card** is the only sink. The
`airband` container needs the sound device (`--device /dev/snd` or privileged) and its
`rtl_airband` config points at the USB card's ALSA device. **Two SDRs:** give the RTL-SDR a
unique serial (`rtl_eeprom -s airband`) and pin it in the `airband` config so it never
grabs the Airspy.

---

## Key library / API references

- **Decoder/feeder:** [sdr-enthusiasts/docker-adsb-ultrafeeder](https://github.com/sdr-enthusiasts/docker-adsb-ultrafeeder), [airspy_adsb](https://github.com/sdr-enthusiasts/docker-airspy_adsb)
- **Airband:** [rtl-airband/RTLSDR-Airband](https://github.com/rtl-airband/RTLSDR-Airband), balena image `sdr-enthusiasts/docker-rtlsdrairband`
- **LED matrix:** [hzeller/rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix) (Python/C++ bindings) — **mind the Pi 5 caveat**.
- **Route lookup:** [adsbdb.com](https://www.adsbdb.com/) (free, no key), [adsb.lol API](https://api.adsb.lol/docs)
- **Map (setup/debug + watch-area drawing):** tar1090 (bundled) / [Leaflet](https://leafletjs.com/) in the app UI
- **balena reference app:** `balena-ads-b` on [balenaHub](https://hub.balena.io/)

See [docker-compose.yml](../docker-compose.yml) for the concrete starting point.
