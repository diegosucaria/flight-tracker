"""Runtime configuration: receiver location, watch area, display prefs.

Loaded from a JSON file on a Docker/balena volume so the web UI can edit it live.
TODO: wire the web UI save/load + validation; support polygon watch areas.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass, field, fields

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/config.json")


def _env_float(*names: str, default: float = 0.0) -> float:
    """First parseable value among the given env var names, else default.

    Location is read from READSB_LAT/LON (the same vars ultrafeeder uses) so the
    receiver position is set once as a balena variable; RECEIVER_* is a fallback.
    """
    for name in names:
        val = os.environ.get(name)
        if val:
            try:
                return float(val)
            except ValueError:
                pass
    return default


@dataclass
class WatchSector:
    """Sector of sky to care about: center bearing ± half-angle, distance band."""
    center_deg: float = 0.0
    half_angle_deg: float = 180.0   # 180 = whole circle until you narrow it
    min_km: float = 0.0
    max_km: float = 60.0


@dataclass
class ProximitySector:
    """A close 'right in front of me' zone, aimed at your window.

    Aircraft inside it are featured even with NO callsign / even if GA (the clutter
    filters are bypassed), and take PRIORITY over the broad watch sector — the "it
    just passed my window" case. The ``max_agl_ft`` gate keeps a high overflight that
    happens to share the compass direction out of it; ``traffic_mode`` still applies.
    Defaults to a close full circle (any low+near plane) until you aim it at the window.
    """
    enabled: bool = True
    center_deg: float = 0.0
    half_angle_deg: float = 180.0   # full circle until you aim it at the window
    min_km: float = 0.0
    max_km: float = 8.0             # only genuinely close traffic
    max_agl_ft: float = 6000.0      # only genuinely low / visible traffic


@dataclass
class PanelConfig:
    """LED panel layout + scroll prefs, pushed live to the display over /ws."""
    layout: str = "hybrid"            # compact | hybrid | big | ticker
    scroll_speed_px: float = 30.0     # marquee speed in px/SECOND (display treats <=6 as legacy px/frame)
    scroll_gap_px: int = 12
    scroll_fields: list = field(default_factory=lambda: [
        "operator", "type", "registration", "fl", "speed", "vspeed", "dist", "eta"])
    cycle_seconds: int = 0            # big/compact footer alternation; 0 = off
    idle_behavior: str = "message"    # blank | clock | last | message
    idle_text: str = "no traffic"
    route_extra: str = "auto"         # extra field by the route: auto|fl|alt|flalt|type|dist|speed|none


def _render_airband_conf(freqs_mhz: list, gain: float,
                         mountpoint: str = "atc.mp3", squelch_snr: float = 0.0) -> str:
    """Render an rtl_airband custom config matching the verified working baseline.

    Single rtlsdr device, SCAN mode over ``freqs_mhz`` (values in MHz, exactly as the
    image's live config uses), one icecast output to the bundled server
    (127.0.0.1:8000, source/rtlsdrairband) on ``mountpoint``. centerfreq, sample_rate
    and the icecast creds are pinned to the proven values so the network stream and the
    airband-speaker container keep working. NB: no ALSA output — the speaker consumes
    the Icecast network stream, not a sound device in the airband container.
    """
    freq_list = ",".join(f"{f:.3f}" for f in freqs_mhz)
    squelch_line = (f"        squelch_snr_threshold = {squelch_snr:g};\n"
                    if squelch_snr and squelch_snr > 0 else "")
    return f"""stats_filepath = "/tmp/rtl_airband_stats.txt";
fft_size = 1024;
log_scan_activity = false;
devices: (
  {{
    type = "rtlsdr";
    index = 0;
    gain = {gain:g};
    centerfreq = 123.9;
    sample_rate = 2.56;
    mode = "scan";
    channels:
    (
      {{
        freqs = ( {freq_list} );
{squelch_line}        highpass = 100;
        lowpass = 0;
        outputs: (
          {{
            type = "icecast";
            server = "127.0.0.1";
            port = 8000;
            mountpoint = "{mountpoint}";
            name = "Tower";
            genre = "ATC";
            description = "Air traffic feed";
            username = "source";
            password = "rtlsdrairband";
            send_scan_freq_tags = true;
          }}
        )
      }}
    );
  }}
);
"""


@dataclass
class AirbandConfig:
    """VHF airband scan freqs + RF gain; rendered to rtl_airband.conf on a shared volume."""
    # Example VHF airband freqs — replace with your airport's tower/approach/ground in the UI.
    freqs: list = field(default_factory=lambda: [
        {"mhz": 118.300, "label": "TWR"},
        {"mhz": 119.100, "label": "APP"},
        {"mhz": 121.750, "label": "GND"},
    ])
    gain: float = 33.0
    squelch_snr: float = 9.0           # dB above noise floor (≈ rtl_airband's prior auto default); 0 = auto/off
    mountpoint: str = "atc.mp3"        # keep fixed; the speaker + stream URL depend on it


@dataclass
class MatrixConfig:
    """LED-panel PWM/timing knobs read by the display AT STARTUP (a restart applies them).

    Defaults ARE the research-recommended anti-flicker set (a deliberate change from the old
    uncapped-refresh baseline): cap refresh ~200 to avoid the scan beat, raise gpio_slowdown
    + lsb_ns, keep pwm_bits=9 (lowering it beats the row scan -> rolling lines). If 200 Hz is
    eye-visible, set refresh_hz=0 (uncapped) in the UI. The real fixes are a clean 5V PSU +
    the Bonnet PWM solder mod (adafruit-hat-pwm). brightness is live and stays in Config.
    """
    refresh_hz: int = 200        # research-recommended anti-flicker defaults (cap the scan beat)
    pwm_bits: int = 9
    pwm_lsb_ns: int = 200
    gpio_slowdown: int = 3
    pwm_dither_bits: int = 0
    hardware: str = "adafruit-hat"   # "adafruit-hat-pwm" after the GPIO4-GPIO18 solder mod


@dataclass
class Config:
    lat: float = field(default_factory=lambda: _env_float("READSB_LAT", "RECEIVER_LAT"))
    lon: float = field(default_factory=lambda: _env_float("READSB_LON", "RECEIVER_LON"))
    use_gps: bool = True                  # receiver location: GPS (gpsd) when available, else lat/lon
    watch: WatchSector = field(default_factory=WatchSector)
    proximity: ProximitySector = field(default_factory=ProximitySector)  # close "in front of me" override zone
    select_rule: str = "lowest_closest"   # lowest_closest | closest | strongest
    hide_no_callsign: bool = True         # don't feature aircraft with no flight ID (hex-only)
    hide_general_aviation: bool = False   # don't feature light GA / rotorcraft / gliders (ADS-B category)
    route_api: str = field(default_factory=lambda: os.environ.get("ROUTE_API", "adsbdb"))

    # Home airport (the one you watch) — set HOME_AIRPORT to your ICAO (e.g. KSEA, EGLL, RJTT)
    # or pick it in the UI. Coords + runways are resolved from OurAirports at startup and
    # cached, so nothing here is airport-specific. Leave coords 0.0 to resolve from the code.
    home_airport: str = field(default_factory=lambda: os.environ.get("HOME_AIRPORT", ""))
    airport_lat: float = field(default_factory=lambda: _env_float("AIRPORT_LAT", default=0.0))
    airport_lon: float = field(default_factory=lambda: _env_float("AIRPORT_LON", default=0.0))
    airport_elev_ft: float = field(default_factory=lambda: _env_float("AIRPORT_ELEV_FT", default=0.0))
    traffic_mode: str = "all"             # all | arrivals | departures | arrdep | runway
    distance_mode: str = "from_me"        # from_me | to_airport | both
    # Runways whose final approach passes your window (so we can flag "passing by" vs
    # "other side"). Set these to the runway id(s) you can actually see from your location.
    visible_runways: list = field(default_factory=lambda: [])

    # --- LED panel (pushed to the display over /ws, applied live) ------------------
    brightness: int = 60                  # 0-100; live-adjustable from the UI
    volume: int = 100                     # USB sound-card playback volume (0-100); applied by the speaker
    auto_brightness: bool = False         # future: dim by time-of-day / ambient sensor
    notify_flash: bool = False            # future: white flash when a plane enters the watch
    panel: PanelConfig = field(default_factory=PanelConfig)   # LED layout + scroll prefs
    airband: AirbandConfig = field(default_factory=AirbandConfig)   # VHF scan freqs + gain
    matrix: MatrixConfig = field(default_factory=MatrixConfig)      # LED PWM/timing tuning

    # Top-level fields the API is allowed to set via POST /api/config.
    _MERGEABLE = (
        "lat", "lon", "use_gps", "traffic_mode", "distance_mode", "select_rule",
        "route_api", "home_airport", "airport_lat", "airport_lon", "airport_elev_ft",
        "brightness", "volume", "auto_brightness", "notify_flash", "visible_runways",
        "hide_no_callsign", "hide_general_aviation",
    )
    # Watch sub-fields the API is allowed to set (under the "watch" key).
    _WATCH_MERGEABLE = ("center_deg", "half_angle_deg", "min_km", "max_km")
    # Proximity sub-fields (under the "proximity" key).
    _PROXIMITY_MERGEABLE = ("enabled", "center_deg", "half_angle_deg", "min_km", "max_km", "max_agl_ft")
    # Panel sub-fields (under the "panel" key).
    _PANEL_MERGEABLE = ("layout", "scroll_speed_px", "scroll_gap_px", "scroll_fields",
                        "cycle_seconds", "idle_behavior", "idle_text", "route_extra")
    # Airband sub-fields (under the "airband" key); validated specially in _merge_airband.
    _AIRBAND_MERGEABLE = ("freqs", "gain")
    # Matrix PWM/timing sub-fields (under "matrix"); clamped in _merge_matrix.
    _MATRIX_LIMITS = {"refresh_hz": (0, 1000), "pwm_bits": (1, 11), "pwm_lsb_ns": (50, 400),
                      "gpio_slowdown": (0, 6), "pwm_dither_bits": (0, 2)}

    def to_dict(self) -> dict:
        """Plain-JSON view of the config (nested watch included) for the API."""
        return asdict(self)

    def render_airband_conf(self) -> str:
        """The rtl_airband custom-config text for the current airband freqs + gain.

        Coerces stored values defensively — a legacy/hand-edited stringy mhz or gain must
        never raise here, or it would abort the startup seed and crash-loop the airband.
        """
        freqs = []
        for r in self.airband.freqs:
            if isinstance(r, dict) and "mhz" in r:
                try:
                    freqs.append(float(r["mhz"]))
                except (TypeError, ValueError):
                    continue
        try:
            gain = float(self.airband.gain)
        except (TypeError, ValueError):
            gain = 33.0
        try:
            squelch = float(getattr(self.airband, "squelch_snr", 0.0))
        except (TypeError, ValueError):
            squelch = 0.0
        return _render_airband_conf(freqs or [123.9], gain, self.airband.mountpoint, squelch)

    def merge(self, partial: dict) -> "Config":
        """Apply a partial config (in place) from a POST body; returns self.

        Only known top-level fields and ``watch`` sub-fields are accepted; unknown
        keys are ignored so a malformed body can't inject attributes. The ``watch``
        sub-dict is merged field-by-field (existing values are preserved).
        """
        if not isinstance(partial, dict):
            return self
        for key in self._MERGEABLE:
            if key in partial:
                setattr(self, key, partial[key])
        watch = partial.get("watch")
        if isinstance(watch, dict):
            for key in self._WATCH_MERGEABLE:
                if key in watch:
                    setattr(self.watch, key, watch[key])
        proximity = partial.get("proximity")
        if isinstance(proximity, dict):
            for key in self._PROXIMITY_MERGEABLE:
                if key in proximity:
                    setattr(self.proximity, key, proximity[key])
        panel = partial.get("panel")
        if isinstance(panel, dict):
            for key in self._PANEL_MERGEABLE:
                if key in panel:
                    setattr(self.panel, key, panel[key])
        airband = partial.get("airband")
        if isinstance(airband, dict):
            self._merge_airband(airband)
        matrix = partial.get("matrix")
        if isinstance(matrix, dict):
            self._merge_matrix(matrix)
        return self

    def _merge_matrix(self, matrix: dict) -> None:
        """Clamp + apply matrix PWM/timing knobs (they drive hardware init, so bound them)."""
        for key, (lo, hi) in self._MATRIX_LIMITS.items():
            if key in matrix and not isinstance(matrix[key], bool):
                try:
                    setattr(self.matrix, key, max(lo, min(hi, int(matrix[key]))))
                except (TypeError, ValueError):
                    pass
        if matrix.get("hardware") in ("adafruit-hat", "adafruit-hat-pwm"):
            self.matrix.hardware = matrix["hardware"]

    def _merge_airband(self, airband: dict) -> None:
        """Validated merge of the airband sub-config (drives RF, so clamp + sanitize).

        Bad rows are dropped, gain is clamped to 0-50, freqs are constrained to the VHF
        airband band, and an empty/all-invalid freq list is rejected (keeps last-good) so
        a malformed save can't brick the scan.
        """
        if "gain" in airband and not isinstance(airband["gain"], bool):
            try:
                g = float(airband["gain"])
            except (TypeError, ValueError):
                g = None
            if g is not None and math.isfinite(g):
                self.airband.gain = max(0.0, min(50.0, g))
        if "squelch_snr" in airband and not isinstance(airband["squelch_snr"], bool):
            try:
                s = float(airband["squelch_snr"])
            except (TypeError, ValueError):
                s = None
            if s is not None and math.isfinite(s):
                self.airband.squelch_snr = max(0.0, min(50.0, s))
        if isinstance(airband.get("freqs"), list):
            clean = []
            for row in airband["freqs"]:
                if not isinstance(row, dict):
                    continue
                try:
                    mhz = float(row.get("mhz"))
                except (TypeError, ValueError):
                    continue
                if not (108.0 <= mhz <= 137.0):     # VHF airband only
                    continue
                clean.append({"mhz": round(mhz, 3), "label": str(row.get("label", ""))[:8]})
            if clean:                               # never accept empty — keep last-good
                self.airband.freqs = clean

    def save(self, path: str = CONFIG_PATH) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(asdict(self), f, indent=2)

    @classmethod
    def load(cls, path: str = CONFIG_PATH) -> "Config":
        try:
            with open(path) as f:
                data = json.load(f)
        except FileNotFoundError:
            return cls()
        except ValueError as exc:
            print(f"[config] {path} unreadable ({exc}); using defaults")
            return cls()

        def _only(dc, d):     # keep only fields the dataclass declares
            names = {f.name for f in fields(dc)}
            return {k: v for k, v in (d or {}).items() if k in names}

        try:
            watch = WatchSector(**_only(WatchSector, data.pop("watch", {})))
            proximity = ProximitySector(**_only(ProximitySector, data.pop("proximity", {})))
            panel = PanelConfig(**_only(PanelConfig, data.pop("panel", {})))
            airband = AirbandConfig(**_only(AirbandConfig, data.pop("airband", {})))
            # The Icecast mountpoint is a FIXED internal name the speaker + stream URL depend on
            # — never honour a value persisted from an older release, or a renamed mount silently
            # de-syncs airband (server) from the speaker (client). Always use the code default.
            airband.mountpoint = AirbandConfig.mountpoint
            matrix = MatrixConfig(**_only(MatrixConfig, data.pop("matrix", {})))
            # Drop unknown/stale top-level keys too, so a future field rename can't make
            # __init__ raise and silently wipe the entire saved config back to defaults.
            return cls(watch=watch, proximity=proximity, panel=panel, airband=airband,
                       matrix=matrix, **_only(cls, data))
        except (TypeError, ValueError) as exc:
            print(f"[config] {path} incompatible ({exc}); using defaults")
            return cls()
