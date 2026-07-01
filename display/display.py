"""LED-matrix renderer — subscribes to the app's WebSocket and draws the featured flight
on a single 64×32 HUB75 panel via hzeller rpi-rgb-led-matrix.

Architecture: a WS task only STORES the latest featured + display settings into STATE;
a separate ~25 fps render loop draws every frame (advancing the scroll marquee on a
monotonic clock) and swaps via the double-buffer. This decouples smooth scrolling from
the ~1 Hz data updates. Layouts (compact/hybrid/big/ticker) are picked live from the
web UI over the /ws "display" block — no restart. ``compact`` is the byte-identical
baseline + safe fallback.

On the Pi 5 this uses the PIO backend (rp1_pio = 1, via /dev/pio0) so the RP1 hardware-times
the pixel output — immune to USB-SDR bus contention that flickered the RIO/MMIO path. Needs
EEPROM >= 2024-11-12. Set MATRIX_RP1_PIO=0 to fall back to the RIO backend (/dev/mem).
Falls back to log-only if the matrix can't init, so the container never crash-loops.
"""
from __future__ import annotations

import asyncio
import json
import os
import time

import websockets

def _matrix_cfg() -> dict:
    """PWM/timing overrides written by the app's UI to the shared config (a restart applies
    them). Falls back to env vars when absent. Keeps the live-tuning loop a display restart
    away — no code deploy."""
    try:
        with open(os.environ.get("CONFIG_PATH", "/config/config.json")) as f:
            m = (json.load(f) or {}).get("matrix") or {}
            return m if isinstance(m, dict) else {}
    except (OSError, ValueError):
        return {}


_MX = _matrix_cfg()
_MX_LIMITS = {"refresh_hz": (0, 1000), "pwm_bits": (1, 11), "pwm_lsb_ns": (50, 400),
              "gpio_slowdown": (0, 6), "pwm_dither_bits": (0, 2)}


def _mxget(key: str, env: str, default: str) -> int:
    """Matrix knob from config.json, else env, clamped to a safe range (a hand-edited
    config can't feed an out-of-range value into the matrix init and blank the panel)."""
    v = _MX.get(key)
    if not (isinstance(v, (int, float)) and not isinstance(v, bool)):
        v = os.environ.get(env, default)
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = int(default)
    lo, hi = _MX_LIMITS.get(key, (None, None))
    return max(lo, min(hi, n)) if lo is not None else n


ROWS = int(os.environ.get("MATRIX_ROWS", "32"))
COLS = int(os.environ.get("MATRIX_COLS", "64"))
CHAIN = int(os.environ.get("MATRIX_CHAIN", "1"))
WIDTH, HEIGHT = COLS * CHAIN, ROWS
APP_WS_URL = os.environ.get("APP_WS_URL", "ws://app:8080/ws")
HARDWARE = (_MX.get("hardware") if _MX.get("hardware") in ("adafruit-hat", "adafruit-hat-pwm")
            else os.environ.get("MATRIX_HARDWARE", "adafruit-hat"))   # -pwm after the solder mod
GPIO_SLOWDOWN = _mxget("gpio_slowdown", "MATRIX_GPIO_SLOWDOWN", "3")   # research-recommended default
RP1_PIO = int(os.environ.get("MATRIX_RP1_PIO", "1"))   # 1 = PIO (/dev/pio0, flicker-immune), 0 = RIO
BRIGHTNESS = max(5, min(100, int(float(os.environ.get("MATRIX_BRIGHTNESS", "60")))))
# Panel mounting rotation in degrees (180 = upside-down mount; 0 = none).
ROTATE = int(os.environ.get("MATRIX_ROTATE", "180"))
PWM_BITS = _mxget("pwm_bits", "MATRIX_PWM_BITS", "9")    # 8 beats the row scan → keep 9
# Research: cap refresh ~200 to avoid the scan beat (uncapped beat the row scan).
REFRESH_HZ = _mxget("refresh_hz", "MATRIX_REFRESH_HZ", "200")
PWM_DITHER = _mxget("pwm_dither_bits", "MATRIX_PWM_DITHER_BITS", "0")
PWM_LSB_NS = _mxget("pwm_lsb_ns", "MATRIX_PWM_LSB_NS", "200")
SHOW_REFRESH = os.environ.get("MATRIX_SHOW_REFRESH", "0") == "1"
FPS = max(5, int(os.environ.get("MATRIX_FPS", "50")))   # render-loop tick rate (redraw only on px-change)

# --- Bring up the real matrix; fall back to log-only on any failure ------------
matrix = None
canvas = None
_brightness = None        # last brightness applied live from the app (over /ws)
Image = ImageDraw = ImageFont = None
font = None               # PIL default font (the compact layout's baseline)
FONTS: dict = {}          # hzeller BDF fonts by size name
try:
    from rgbmatrix import RGBMatrix, RGBMatrixOptions
    from PIL import Image, ImageDraw, ImageFont

    opts = RGBMatrixOptions()
    opts.hardware_mapping = HARDWARE
    opts.rows = ROWS
    opts.cols = COLS
    opts.chain_length = CHAIN
    opts.parallel = 1
    opts.gpio_slowdown = GPIO_SLOWDOWN
    opts.brightness = BRIGHTNESS
    opts.pwm_bits = PWM_BITS
    if REFRESH_HZ > 0:
        opts.limit_refresh_rate_hz = REFRESH_HZ
    opts.pwm_dither_bits = PWM_DITHER
    opts.pwm_lsb_nanoseconds = PWM_LSB_NS
    opts.show_refresh_rate = SHOW_REFRESH
    # PIO backend: the RP1's PIO state machine hardware-times the pixel output via /dev/pio0,
    # so a USB-SDR's RP1 bus traffic can NO LONGER jitter it (the confirmed flicker cause).
    # Requires Pi-5 EEPROM >= 2024-11-12 (RP1 firmware mailbox API) so /dev/pio0 exists.
    opts.rp1_pio = RP1_PIO
    opts.drop_privileges = False  # stay root so /dev/mem + /dev/pio0 access keeps working
    matrix = RGBMatrix(options=opts)
    canvas = matrix.CreateFrameCanvas()   # off-screen buffer; swap on vsync = no flicker
    font = ImageFont.load_default()
    # hzeller ships BDF fonts at /opt/rgb/fonts, but PIL's ImageFont.load() reads its OWN
    # .pil format, NOT .bdf — so convert each BDF -> .pil once, then load it. (Without this
    # every font silently fell back to the ~11px default and the bottom line overflowed.)
    from PIL import BdfFontFile
    for _name, _file in (("tiny", "4x6"), ("small", "5x8"), ("med", "6x10"), ("big", "8x13")):
        try:
            with open(f"/opt/rgb/fonts/{_file}.bdf", "rb") as _fp:
                BdfFontFile.BdfFontFile(_fp).save(f"/tmp/{_file}")
            FONTS[_name] = ImageFont.load(f"/tmp/{_file}.pil")
        except Exception:  # noqa: BLE001 — any failure: fall back to the default font
            FONTS[_name] = font
    print(f"[display] matrix up: {WIDTH}x{HEIGHT} ({HARDWARE}, "
          f"{'PIO' if RP1_PIO else 'RIO'} backend), fonts={list(FONTS)}")
except Exception as exc:  # noqa: BLE001
    print(f"[display] matrix unavailable ({type(exc).__name__}: {exc}) — log-only mode")

# Pin the render thread to core 2 so it never shares a core with hzeller's real-time PWM
# refresh thread (hard-pinned to core 3). With docker-compose cpuset "2,3" this keeps core
# 3 exclusively for the refresh -> no starvation -> no flicker even with a 25 fps marquee.
if matrix is not None:
    try:
        os.sched_setaffinity(0, {2})
        print("[display] render pinned to core 2 (refresh owns core 3)")
    except (OSError, AttributeError) as exc:  # noqa: BLE001
        print(f"[display] core-2 pin skipped ({exc})")


# --- live shared state: WS task writes, render loop reads -----------------------
STATE: dict = {"featured": None, "display": {}}
_last_featured: dict | None = None   # retained for the "last" idle behaviour
# scroll marquee state (advanced once/frame in the render loop)
_scroll_x = 0.0
_scroll_key = None        # (hex, layout) — rebuild the scroll content only when this changes
_scroll_str = ""
_scroll_w = 0
_ticker_segs: list = []   # [(text, fill)] for the ticker layout
_ticker_w = 0
_last_frame = None
_ws_ver = 0               # bumped on each WS message (data change)
_drawn_ver = -1           # last version drawn — skip redraw of unchanged static frames
_last_scroll_pos = -1     # last integer scroll px drawn — redraw only when it ticks
_FLIP_DUR = 0.42          # seconds for one flip-clock digit to flip
_clock_shown = None       # the 4 digits currently settled on the flip clock
_clock_flips: dict = {}   # pos -> (old_char, start_monotonic) for in-progress flips


# ---------- small formatters ----------
def _compass(b) -> str:
    if b is None:
        return ""
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][round(b / 45) % 8]


def _font(name):
    return FONTS.get(name) or font


def _fmt_dur(m):
    if not isinstance(m, (int, float)):
        return None
    h, mm = divmod(int(m), 60)
    return f"{h}h{mm:02d}m" if h else f"{mm}m"


def _callsign_color(featured) -> tuple:
    return (255, 40, 40) if featured.get("military") else (255, 200, 0)


def _route_extra_text(featured, mode):
    """The small right-aligned field on the route line, per panel.route_extra.

    auto = your airport's arrivals/departures show altitude (ft), everything else the type.
    """
    alt = featured.get("alt_baro")
    has_alt = isinstance(alt, (int, float)) and not isinstance(alt, bool)
    fl = f"FL{int(alt) // 100:03d}" if has_alt else ""
    ft = f"{int(alt)}ft" if has_alt else ""
    typ = (featured.get("type") or "")[:5]
    if mode == "none":
        return ""
    if mode == "fl":
        return fl
    if mode == "alt":
        return ft
    if mode == "flalt":
        return f"{fl} {int(alt)}" if has_alt else typ
    if mode == "flft":          # FL at/above 10k ft, plain feet below
        return ("" if not has_alt else fl if alt >= 10000 else ft)
    if mode == "type":
        return typ
    if mode == "dist":
        dk = featured.get("distance_km")
        return f"{int(dk)}km" if isinstance(dk, (int, float)) else ""
    if mode == "speed":
        gs = featured.get("gs")
        return f"{int(gs)}kt" if isinstance(gs, (int, float)) else ""
    # auto
    if featured.get("is_arrival") or featured.get("is_departure"):
        return ft or typ
    return typ


def _badge_runway(featured):
    """Runway to show on the badge: the landing runway (arrival, always), or the departure
    runway ONLY while still in its takeoff/climbout phase — it clears past 10k with depart_phase."""
    if featured.get("landing_runway"):
        return featured.get("landing_runway")
    if featured.get("departure_runway") and featured.get("depart_phase"):
        return featured.get("departure_runway")
    return None


def _runway_badge_color(featured):
    if not _badge_runway(featured):
        return None
    if featured.get("window_visible"):
        return (0, 200, 0)            # PASSING BY your window
    return (255, 170, 0)              # using a runway on the OTHER SIDE


def _field_text(featured, f):
    """One scroll/ticker token's text for field name ``f`` (or None to skip)."""
    if f == "operator":
        return featured.get("operator") or featured.get("airline")
    if f == "type":
        return featured.get("type")
    if f == "registration":
        return featured.get("registration")
    if f == "fl":
        alt = featured.get("alt_baro")
        return f"FL{int(alt) // 100:03d}" if isinstance(alt, (int, float)) else None
    if f == "speed":
        gs = featured.get("gs")
        return f"{int(gs)}kt" if isinstance(gs, (int, float)) else None
    if f == "vspeed":
        r = featured.get("baro_rate")
        return f"{int(r):+d}fpm" if isinstance(r, (int, float)) and abs(r) > 50 else None
    if f == "dist":
        dkm = featured.get("distance_km")
        comp = _compass(featured.get("bearing_from_me_deg"))
        return f"{int(dkm)}km {comp}".strip() if isinstance(dkm, (int, float)) else None
    if f == "eta":
        d = _fmt_dur(featured.get("eta_min"))      # time TO the airport, not whole-flight duration
        return f"ETA {d}" if d else None
    if f == "route":
        o, dd = featured.get("origin"), featured.get("destination")
        return f"{o or '?'}>{dd or '?'}" if (o or dd) else None
    if f == "rwy":
        r = featured.get("landing_runway")
        return f"RWY{r}" if r else None
    return None


def _scroll_string(featured, fields) -> str:
    parts = [t for t in (_field_text(featured, f) for f in (fields or [])) if t]
    phase = featured.get("depart_phase")              # TAKING OFF -> TOOK OFF -> (gone over 10k)
    if phase:
        dep = featured.get("departure_runway")
        verb = "TAKING OFF" if phase == "takeoff" else "TOOK OFF"
        parts.insert(0, verb + (f" {dep}" if dep else ""))
    return "   ".join(str(p) for p in parts)


def _push(img) -> None:
    """Rotate to the panel's mounting, then swap in via the off-screen canvas.

    MUST stay unsafe=False: unsafe=True segfaulted (exit 139) on this rpi-rgb-led-matrix
    build. The flicker that unsafe=False causes at 25 fps comes from the render sharing a
    CPU core with the PWM refresh thread — that's solved at the docker-compose level by
    giving the display container two cores (cpuset "2,3"): hzeller pins the refresh thread
    to core 3, the Python render runs on core 2, so they no longer contend."""
    global canvas
    if ROTATE:
        img = img.rotate(ROTATE)
    canvas.SetImage(img, unsafe=False)
    canvas = matrix.SwapOnVSync(canvas)


def _apply_display(cmd: dict | None) -> None:
    """Live panel commands from the app: brightness now; flash is a wired hook."""
    global _brightness
    if not cmd or matrix is None:
        return
    b = cmd.get("brightness")
    if isinstance(b, (int, float)) and int(b) != _brightness:
        _brightness = int(b)
        try:
            matrix.brightness = max(1, min(100, _brightness))
        except Exception as exc:  # noqa: BLE001
            print(f"[display] brightness set failed: {exc}")
    # future: if cmd.get("flash"): briefly push white frame(s) as a notification


# ---------- layouts ----------
def render_compact(d, featured, disp) -> None:
    """The original 3-line static layout (byte-identical baseline + safe fallback)."""
    cs = (featured.get("flight") or "????").strip()
    origin = featured.get("origin") or "?"
    dest = featured.get("destination") or "?"
    dist = featured.get("distance_km")
    comp = _compass(featured.get("bearing_from_me_deg"))
    alt = featured.get("alt_baro")
    d.text((1, 0), cs[:11], font=font, fill=(255, 200, 0))
    d.text((1, 11), f"{origin}>{dest}"[:11], font=font, fill=(0, 220, 0))
    dkm = f"{int(dist)}km" if isinstance(dist, (int, float)) else "?km"
    fl = f"FL{int(alt) // 100:03d}" if isinstance(alt, (int, float)) else ""
    d.text((1, 22), f"{dkm} {comp} {fl}"[:13], font=font, fill=(120, 180, 255))


def render_hybrid(d, featured, disp) -> None:
    """Static callsign + route + runway badge on top, one scrolling detail line below."""
    global _scroll_str, _scroll_w, _scroll_key, _scroll_x
    med, small, tiny = _font("med"), _font("small"), _font("tiny")
    # Row A: callsign + runway badge (badge drawn last to overlay the callsign tail)
    cs = (featured.get("flight") or "????").strip()
    d.text((1, 0), cs[:9], font=med, fill=_callsign_color(featured))
    col = _runway_badge_color(featured)
    if col:
        rwy = str(_badge_runway(featured) or "")[:2]
        bw = 14
        bx = WIDTH - bw
        d.rectangle((bx, 0, WIDTH - 1, 9), fill=col)
        bf = _font("med")
        # Render the faux-bold digits to a temp mask, measure the ACTUAL ink with getbbox,
        # and paste it dead-centre — the bold's extra px makes textbbox width unreliable.
        tmp = Image.new("L", (bw + 4, 12), 0)
        td = ImageDraw.Draw(tmp)
        td.text((0, 0), rwy, font=bf, fill=255)
        td.text((1, 0), rwy, font=bf, fill=255)              # faux-bold
        ink = tmp.getbbox()
        if ink:
            glyph = tmp.crop(ink)
            gw, gh = glyph.size
            d.bitmap((bx + (bw - gw) // 2, (10 - gh) // 2), glyph, fill=(0, 0, 0))
    # Row B: route (left) + a configurable extra (right, tiny) — panel.route_extra picks
    # what fills the spare space (auto = altitude for your airport's arr/dep, else type).
    o, dd = featured.get("origin"), featured.get("destination")
    has_route = bool(o or dd)
    route = f"{o}>{dd}" if has_route else " ".join(
        str(x) for x in (featured.get("type"), featured.get("registration")) if x)
    extra = _route_extra_text(featured, disp.get("route_extra", "auto")) if has_route else ""
    tinyf = _font("tiny")
    ew = int(d.textlength(extra, font=tinyf)) if extra else 0
    rmax = (WIDTH - ew - 3) if ew else WIDTH       # clip route so it can't run under the extra
    rclip = route
    while rclip and int(d.textlength(rclip, font=small)) > rmax:
        rclip = rclip[:-1]
    d.text((1, 11), rclip, font=small, fill=(0, 220, 0))
    if extra:
        rb = d.textbbox((0, 11), "0", font=small)[3]      # bottom of the route line
        eh = d.textbbox((0, 0), extra, font=tinyf)[3]     # extra glyph height
        d.text((WIDTH - ew, rb - eh), extra, font=tinyf, fill=(120, 180, 255))   # bottom-aligned
    # Bottom: a "LANDED" flash for an arrival that reached the field, else the scroll line
    if featured.get("landed"):
        d.text((17, 22), "LANDED", font=small, fill=(0, 230, 90))
        return
    key = (featured.get("hex"), "hybrid")            # reset the scroll POSITION only on a new flight
    s = _scroll_string(featured, disp.get("scroll_fields"))
    if s != _scroll_str or key != _scroll_key:       # refresh TEXT whenever it changes (phase, dist, eta…)
        _scroll_str = s
        _scroll_w = int(d.textlength(s, font=small)) if s else 0
        if key != _scroll_key:                       # new flight → restart the marquee from the right
            _scroll_x = 0.0
            _scroll_key = key
    if _scroll_str and _scroll_w:
        gap = int(disp.get("scroll_gap_px", 12))
        total = _scroll_w + gap
        off = int(_scroll_x) % total
        d.text((WIDTH - off, 22), _scroll_str, font=small, fill=(120, 180, 255))
        d.text((WIDTH - off + total, 22), _scroll_str, font=small, fill=(120, 180, 255))


def render_big(d, featured, disp) -> None:
    """Hero callsign (read across the room) + a status pip + a small footer."""
    cs = (featured.get("flight") or "????").strip()
    hero = _font("big") if len(cs) <= 7 else _font("med")
    w = int(d.textlength(cs, font=hero))
    d.text((max(0, (WIDTH - w) // 2), 1), cs, font=hero, fill=_callsign_color(featured))
    # status pip top-right
    pip = _runway_badge_color(featured) or ((255, 40, 40) if featured.get("military") else None)
    if pip:
        d.rectangle((WIDTH - 5, 0, WIDTH - 1, 4), fill=pip)
    # footer: route (left) + dist/dir (right)
    small = _font("small")
    o, dd = featured.get("origin"), featured.get("destination")
    if o or dd:
        d.text((1, 22), f"{o or '?'}>{dd or '?'}"[:9], font=small, fill=(0, 220, 0))
    dkm = featured.get("distance_km")
    if isinstance(dkm, (int, float)):
        rt = f"{int(dkm)}km {_compass(featured.get('bearing_from_me_deg'))}".strip()
        rw = int(d.textlength(rt, font=small))
        d.text((WIDTH - rw - 1, 22), rt, font=small, fill=(120, 180, 255))


_TICKER_FIELDS = ("rwy", "route", "type", "registration", "fl", "speed", "vspeed", "dist", "eta")


def render_ticker(d, featured, disp) -> None:
    """One continuous, colour-segmented marquee — the whole flight, no truncation."""
    global _ticker_segs, _ticker_w, _scroll_key, _scroll_x
    med = _font("med")
    key = (featured.get("hex"), "ticker")
    if key != _scroll_key:
        segs = [((featured.get("flight") or "????").strip(), _callsign_color(featured))]
        for f in _TICKER_FIELDS:
            t = _field_text(featured, f)
            if not t:
                continue
            if f == "rwy":
                col = _runway_badge_color(featured) or (0, 220, 0)
            elif f == "route":
                col = (0, 220, 0)
            elif f in ("fl", "speed", "dist"):
                col = (120, 180, 255)
            elif f == "vspeed":
                r = featured.get("baro_rate") or 0
                col = (255, 90, 90) if r < 0 else (90, 220, 120)
            elif f == "eta":
                col = (150, 150, 150)
            else:
                col = (230, 230, 230)
            segs.append((str(t), col))
        _ticker_segs = segs
        _ticker_w = sum(int(d.textlength(t + "  ", font=med)) for t, _ in segs)
        _scroll_x = 0.0
        _scroll_key = key
    if not _ticker_segs or not _ticker_w:
        return
    gap = 24
    total = _ticker_w + gap
    base = WIDTH - (int(_scroll_x) % total)
    for rep in (0, total):                 # draw twice for a seamless wrap
        x = base + rep
        for txt, col in _ticker_segs:
            seg = txt + "  "
            sw = int(d.textlength(seg, font=med))
            if x + sw > 0 and x < WIDTH:    # only draw visible segments
                d.text((x, 9), seg, font=med, fill=col)
            x += sw


def _draw_clock_digit(d, x, y0, tw, th, ch, scale=1.0) -> None:
    """One bold digit SCALED UP to nearly fill the tile, vertically squashed by ``scale``
    (scale<1 = mid-flip) and anchored at the tile's centre seam so it folds there.

    The 8x13 font is the largest we have (~11px ink) but the tile is 24px tall, so we
    crop to the ink and resize it up (NEAREST keeps it crisp on the LED) to fill the height.
    """
    big = _font("big")
    tmp = Image.new("L", (tw + 4, th), 0)
    td = ImageDraw.Draw(tmp)
    bb = td.textbbox((0, 0), ch, font=big)
    ox, oy = 2 - bb[0], -bb[1]
    td.text((ox, oy), ch, font=big, fill=255)
    td.text((ox + 1, oy), ch, font=big, fill=255)      # faux-bold (getbbox re-centres below)
    ink = tmp.getbbox()
    if not ink:
        return
    g = tmp.crop(ink)
    gw, gh = g.size
    # Scale the digit with 4px vertical + 2px side padding, scaling the two axes
    # independently so wide digits stay tall instead of shrinking to fit the width.
    bh_ = th - 8                                       # 4px top + 4px bottom padding
    bw_ = min(tw - 4, max(1, int(round(gw * bh_ / gh))))   # 2px side padding
    g = g.resize((bw_, bh_), Image.NEAREST)
    sh = max(1, int(round(bh_ * scale)))               # flip squash on top of the base scale
    if sh != bh_:
        g = g.resize((bw_, sh), Image.NEAREST)
    d.bitmap((x + (tw - bw_) // 2, y0 + th // 2 - sh // 2), g, fill=(232, 236, 245))


def _render_flip_clock(d) -> None:
    """HH:MM split-flap airport board WITH a real flip when a digit changes: a changed
    digit collapses to the centre seam, then the new one unfolds from it. Dark tiles, a
    black seam (off = no PWM flicker) and an amber colon."""
    global _clock_shown, _clock_flips
    now = time.monotonic()
    tgt = time.strftime("%H:%M")
    tgt = tgt[:2] + tgt[3:]                 # 4 digits; colon drawn separately
    if _clock_shown is None:
        _clock_shown = tgt
    shown = list(_clock_shown)
    for i in range(4):
        if tgt[i] != shown[i] and i not in _clock_flips:
            _clock_flips[i] = (shown[i], now)
    tile_w, tile_h = 13, 24
    y0 = (HEIGHT - tile_h) // 2
    # centred as a group on the 64px width, symmetric about the colon at x=31/32:
    # HH 3-15,17-29 | colon 31,32 | MM 34-46,48-60  (3px margin each side)
    xs = (3, 17, 34, 48)
    for i in range(4):
        x = xs[i]
        d.rectangle((x, y0, x + tile_w - 1, y0 + tile_h - 1), fill=(60, 64, 80))  # visible but not too bright
        # seam UNDER the digit so it never erases a digit's middle stroke (6/8/9/0)
        d.line((x, y0 + tile_h // 2, x + tile_w - 1, y0 + tile_h // 2), fill=(0, 0, 0))
        if i in _clock_flips:
            old_ch, start = _clock_flips[i]
            p = (now - start) / _FLIP_DUR
            if p >= 1.0:
                shown[i] = tgt[i]
                del _clock_flips[i]
                _draw_clock_digit(d, x, y0, tile_w, tile_h, tgt[i])
            elif p < 0.5:                   # old digit folds down to the seam
                _draw_clock_digit(d, x, y0, tile_w, tile_h, old_ch, 1.0 - p * 2)
            else:                           # new digit unfolds from the seam
                _draw_clock_digit(d, x, y0, tile_w, tile_h, tgt[i], (p - 0.5) * 2)
        else:
            _draw_clock_digit(d, x, y0, tile_w, tile_h, shown[i])
    cx = 31                                 # colon between HH and MM
    d.rectangle((cx, y0 + 6, cx + 1, y0 + 7), fill=(235, 200, 80))
    d.rectangle((cx, y0 + tile_h - 8, cx + 1, y0 + tile_h - 7), fill=(235, 200, 80))
    _clock_shown = "".join(shown)


def _render_idle(d, disp) -> None:
    landing = STATE.get("landing")          # a recent touchdown flashes over the idle view
    if landing:
        cs = str(landing.get("callsign") or "")[:9]
        rwy = landing.get("runway")
        d.text((2, 1), cs, font=_font("med"), fill=(0, 230, 90))
        d.text((2, 13), "LANDED" + (f" {rwy}" if rwy else ""), font=_font("small"), fill=(0, 200, 80))
        return
    beh = disp.get("idle_behavior", "message")
    if beh == "blank":
        return
    if beh == "clock":
        _render_flip_clock(d)
        return
    if beh == "last" and _last_featured:
        render_hybrid(d, _last_featured, disp)
        return
    d.text((2, 12), (disp.get("idle_text") or "no traffic")[:12], font=_font("small"), fill=(150, 150, 165))


_LAYOUTS = {"compact": render_compact, "hybrid": render_hybrid,
            "big": render_big, "ticker": render_ticker}


# ---------- the two cooperating tasks ----------
async def render_loop() -> None:
    """Fast tick loop that advances the marquee on a monotonic clock and redraws ONLY when
    the integer scroll position changes (or data changes).

    Smooth bitmap scrolling needs even integer-pixel steps timed by a real clock — not one
    advance per rendered frame (which judders when per-frame px is fractional or frame
    timing jitters). So we accumulate sub-pixel at a px/SECOND rate and emit a frame exactly
    when the whole-pixel position ticks; the high tick rate just lets each step land on time.
    """
    global _last_frame, _scroll_x, _drawn_ver, _last_scroll_pos
    frame_dt = 1.0 / FPS
    while True:
        now = time.monotonic()
        if _last_frame is None:
            _last_frame = now
        disp = STATE["display"] or {}
        featured = STATE["featured"]
        layout = disp.get("layout") or "hybrid"
        scrolling = (layout == "ticker"
                     or (layout == "hybrid" and bool(disp.get("scroll_fields"))))
        # px/SECOND accumulator (frame-rate independent). Legacy saved values were px/frame
        # (slider 0.5-3); treat anything <=6 as legacy and scale by the old 25 fps.
        spd = float(disp.get("scroll_speed_px", 30.0))
        if spd <= 6.0:
            spd *= 25.0
        dt = min(now - _last_frame, 0.1)        # clamp stalls so we never jump a big gap
        _scroll_x += spd * dt
        _last_frame = now
        pos = int(_scroll_x)
        # keep redrawing while a flip-clock digit is mid-animation
        clock_anim = (bool(_clock_flips) and not featured
                      and disp.get("idle_behavior") == "clock")
        if matrix is not None and ((scrolling and pos != _last_scroll_pos)
                                   or _ws_ver != _drawn_ver or clock_anim):
            img = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
            d = ImageDraw.Draw(img)
            if not featured:
                _render_idle(d, disp)
            else:
                _LAYOUTS.get(layout, render_hybrid)(d, featured, disp)
            _push(img)
            _drawn_ver = _ws_ver
            _last_scroll_pos = pos
        await asyncio.sleep(max(0.0, frame_dt - (time.monotonic() - now)))


async def ws_task() -> None:
    """Subscribe to the app's /ws; only STORE the latest state (no rendering here)."""
    global STATE, _last_featured, _ws_ver
    async for ws in websockets.connect(APP_WS_URL):       # auto-reconnect
        try:
            async for msg in ws:
                data = json.loads(msg)
                feat = data.get("featured")
                STATE = {"featured": feat, "display": data.get("display") or {},
                         "landing": data.get("landing")}
                _ws_ver += 1
                if feat:
                    _last_featured = feat
                    print(f"[display] {feat.get('flight', '?')} "
                          f"{feat.get('origin', '?')}>{feat.get('destination', '?')} "
                          f"rwy={feat.get('landing_runway') or feat.get('departure_runway')} "
                          f"vis={feat.get('window_visible')}")
                _apply_display(data.get("display"))
        except websockets.ConnectionClosed:
            continue


async def main() -> None:
    print(f"[display] connecting to {APP_WS_URL} "
          f"(panel {WIDTH}x{HEIGHT}, matrix={'yes' if matrix else 'no'}, {FPS}fps)")
    await asyncio.gather(ws_task(), render_loop())


if __name__ == "__main__":
    asyncio.run(main())
