# HARDWARE — the build

The actual parts this project was built with. Prices/links are **EU examples** (approximate,
VAT-incl., checked June 2026) — every part is sold worldwide, so substitute your local
retailers and currency. Read the [caveats](#caveats--gotchas) before ordering.

> Range/overload notes assume the receiver is a **short distance from the field** (≈10–20 km),
> where signals are strong — tune gain *down* rather than adding a preamp.

## Bill of materials

| Part | Role | Example product / link | ~€ |
|------|------|------------------------|---:|
| **Raspberry Pi 5** (4 GB) | The computer | Raspberry Pi 5 | 70 |
| **Official Pi 5 27 W USB-C PSU** | Powers the Pi — full USB budget for 2 SDRs + GPS | [BerryBase](https://www.berrybase.de/en/raspberry-pi-27w-usb-c-power-supply-power-supply-unit-white) | 12 |
| **Airspy Mini** | 1090 MHz ADS-B SDR — 12-bit, handles strong close-in signals without overload | [WiMo](https://www.wimo.com/en/airspy-mini) | 120 |
| **WiMo TEN-90 omni** + 5 m SMA coax | 1090 MHz antenna (whole-sky) on the sill/balcony, plugs straight into the Airspy (SMA) | [WiMo TEN-90](https://www.wimo.com/en/ten-90) + [Reichelt #88444](https://www.reichelt.com/de/en/hf-sma-plug-sma-jack-5-m-delock-88444-p179733.html) | 70 |
| **RTL-SDR Blog V3 kit** (dongle + telescopic dipole) | 2nd SDR + antenna for VHF tower comms (airband) | [WiMo](https://www.wimo.com/en/rtl-sdr-v3-kit) | 35 |
| **Adafruit 64×32 RGB LED matrix, P4** (ADA2278) ×1 | The display | [BerryBase](https://www.berrybase.de/en/64x32-rgb-led-matrix-4mm-grid) | 40 |
| **Adafruit RGB Matrix Bonnet** | Solderless driver — connects the panel to the Pi | [BerryBase](https://www.berrybase.de/en/adafruit-rgb-matrix-bonnet-for-raspberry-pi) | 20 |
| **Mean Well LRS-50-5** (5 V / 10 A) | Powers the LED panel directly (not through the Pi) | [Reichelt #202960](https://www.reichelt.com/de/en/shop/product/switching_power_supplies_50_w_5_v_10_a-202960) | 13 |
| **VK-162 USB GPS** (u-blox) | Auto-locates the receiver (2 m lead → puck at the window); else set lat/lon by hand | [Amazon B0B1ZW9XJK](https://www.amazon.de/-/en/Geekstory-Navigation-External-Receiver-Raspberry/dp/B0B1ZW9XJK) | 17 |
| **USB sound card** (USB-A → 3.5 mm) + **small powered speaker** | Tower-comms audio out (onboard audio is taken by the matrix) | UGREEN sound card + Delock 27002 speaker | 25 |
| **SanDisk Extreme 64 GB A2** microSD | Boots balenaOS | [BerryBase](https://www.berrybase.de/en/sandisk-extreme-microsdxc-a2-uhs-i-u3-v30-170mb-s-memory-card-adapter-64gb/) | 9 |
| goobay USB 2.0 extension + SMA cabling | Put the SDR/GPS at the window, away from the noisy matrix | — | 5 |
| | | **Total** | **~€435** |

A no-airband, no-GPS build (drop the RTL-SDR kit, sound card, speaker, GPS) is **~€350**.
Swap the Airspy for an RTL-SDR and a basic dipole to go cheaper still.

## Display

One **64×32 P4 panel** (256 × 128 mm, 4 mm pitch; 1/16 scan → no "E" jumper needed). The
renderer fits the flight onto 64 px with several layouts (compact / hybrid / big / scrolling
ticker) — see [the on-screen layout in SPEC §4](SPEC.md#4-display-content-design-6432).

> Want it wider? Chain a **second** 64×32 panel and set `MATRIX_CHAIN=2` (→ 128×32 ticker).
> The 5 V supply above has the headroom for two.

## Power & wiring

The panel is powered **directly from the 5 V supply**, not through the Pi/Bonnet.

- Worst-case 5 V draw (all-white; real content is far less): one 64×32 panel ≈ **2 A**; Pi 5
  under load ≈ **2.5–5 A**.
- **Simplest:** power the Pi from its own 27 W USB-C PSU and let the 5 V supply drive only the
  panel. (Alternatively, feed both from the Mean Well — panel pigtail + the Bonnet's screw
  terminal — but then don't also plug in the Pi's USB-C.) Use **thick, short** 5 V leads;
  droop = flicker. The speaker takes USB 5 V (Pi or a charger).
- **Two SDRs** draw real current — the **27 W PSU** (or a powered USB hub) avoids brownouts.

## Caveats & gotchas

1. **Pi 5 LED matrix.** Driven by **hzeller `rpi-rgb-led-matrix`'s PIO backend** (`rp1_pio=1`,
   via `/dev/pio0` — the RP1 hardware-times the panel, which kills the dim-pixel flicker the
   CPU path shows under USB/PCIe contention). Needs a **recent Pi 5 EEPROM** so `/dev/pio0`
   exists. The Bonnet works on a Pi 5; the GPIO4↔18 jumper is a Pi 3/4-only fix — skip it.
2. **Two SDRs → set serials.** Give the airband RTL-SDR a unique USB serial (`rtl_eeprom -s
   airband`) so software never grabs the Airspy by mistake.
3. **Audio path.** Onboard audio is **off** (the matrix needs the PWM) → the **USB sound card**
   is the only speaker output; the 3.5 mm jack won't work.
4. **Overload at short range.** Prefer the Airspy; **skip any LNA/preamp** (it worsens overload
   this close). Watch "% good messages" in tar1090 and reduce gain if needed. A passive
   directional panel (e.g. GNS "TEN90 with housing", SMA) is an optional A/B experiment.
5. **Airband legality is per-country.** Monitoring aviation radio is **restricted in some
   places** (e.g. Germany, §89 TKG / §5 TTDSG). Verify locally; don't record/rebroadcast.
6. **GPS indoors** can be marginal — the VK-162's 2 m lead puts the puck at the glass, or set
   the location by hand in the UI.
7. **Window glass attenuates 1090** (more if wired/frosted) — try the omni just outside on the
   sill/rail. PSUs are universal-input (100–240 V); nothing is frequency-locked.
