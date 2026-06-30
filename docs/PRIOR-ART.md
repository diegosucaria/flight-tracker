# Prior art & reuse notes

Resources we studied, and what we'll borrow. **Reimplement ideas; don't copy GPL code.**

## ColinWaddell/FlightTracker — https://github.com/ColinWaddell/FlightTracker
A near-twin: a Pi flight tracker that shows overhead aircraft on a **64×32 HUB75 matrix**.
**100% Python. License: GPL-3.0** → we can *study* it freely, but pasting non-trivial code
makes our project GPL too. We reimplement from these notes instead.

**Matches our scaffold already:**
- Pulls a local **tar1090 `aircraft.json`**, filters by a home bounding box + altitude,
  sorts nearest-first, takes the top N — same as our [`selector.py`](../app/app/selector.py).
- Route lookup via **adsbdb** `GET /v0/callsign/{cs}` → `flightroute.origin/destination.iata_code`,
  cached ~8 h — same API as our [`enrich.py`](../app/app/enrich.py) (good confirmation).

**Worth borrowing (reimplemented) when we build the display (Phase 3):**
- **Scene/layout pattern** for 64×32: route line on top (`ORIGIN → DEST`), callsign in the
  middle (digits vs letters in different colours), aircraft type scrolling along the bottom.
- **Horizontal scroll loop** for text wider than 64 px (decrement x each frame, reset at width).
- **hzeller `RGBMatrixOptions` baseline** (Pi 3/4 only): `pwm_bits=11`, `pwm_lsb_nanoseconds=130`,
  `gpio_slowdown=1`, `hardware_mapping="adafruit-hat-pwm"`. Reference values, not our driver.
- The classic **BDF fonts** (`4x6`, `5x8`, `6x12`, `8x13`).

**Not in it:** no airline logos, no aircraft photos, no compass bearing arrow (theirs is a
static `→`). It predates Pi 5 support — we run the **same hzeller library** but with its newer
**PIO backend** (`rp1_pio=1`, via `/dev/pio0`), which drives the panel cleanly on the Pi 5.

Key files to read: `utilities/overhead_tar1090.py` (ingest + adsbdb), `scenes/journey.py`,
`scenes/flightdetails.py`, `scenes/planedetails.py`, `display/__init__.py`, `setup/fonts.py`.

## Driving a 64×32 HUB75 from a Pi — library + tuning
- **Pi 5 → hzeller `rpi-rgb-led-matrix`, PIO backend** (`rp1_pio=1`, via `/dev/pio0`; the RP1
  hardware-times the panel — needs a recent Pi 5 EEPROM). **Our path.**
- **Pi 3/4 → hzeller `rpi-rgb-led-matrix`, classic mode** (the proven standard; needs the
  GPIO4↔18 jumper for flicker-free `adafruit-hat-pwm`).
- **Flicker/power discipline (applies to both):** `dtparam=audio=off` (onboard audio shares
  the PWM timer); a **dedicated 5 V supply** for the panel with a **common ground** to the Pi;
  cap brightness. (hzeller-only extras: `--led-slowdown-gpio`, `isolcpus=3`.)
