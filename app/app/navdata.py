"""Runtime generation of the map's aviation overlay (the Airways / Navaids / Fixes layers).

Like the home-airport coords and runways, the overlay is DERIVED at runtime from the configured
airport — no committed per-airport data, no build-time variable. On startup we fetch the open
X-Plane navdata (ptsmonteiro/x-plane-navdata, GPL v3, ~2012 cycle) once, cache the source, and
write a compact ``navdata.json`` the web UI loads. (tools/build_navdata.py does the same for
offline/CLI use.) Best-effort: any failure just leaves the overlay empty.
"""
from __future__ import annotations

import csv
import io
import json
import os

_REPO = "https://raw.githubusercontent.com/ptsmonteiro/x-plane-navdata/master"
_FILES = ("earth_fix.dat", "earth_nav.dat", "earth_awy.dat")
# Navaids come from OurAirports (public domain, CURRENT) instead of the 2012 X-Plane cycle;
# fixes + airways stay on X-Plane (no free current source for the enroute network).
_OA_NAVAIDS = "https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/navaids.csv"
_CACHE_V = 2                                                  # bump to force a rebuild of old overlays
_NAV_KIND = {2: "NDB", 3: "VOR", 13: "DME"}
_PRIO = {"VOR": 3, "NDB": 2, "DME": 1}


def _navaids_from_csv(csv_text: str, lat: float, lon: float, radius: float) -> list[dict]:
    """Current navaids in the bbox from OurAirports navaids.csv (VOR/NDB/DME, deduped)."""
    lat_min, lat_max, lon_min, lon_max = lat - radius, lat + radius, lon - radius, lon + radius

    def kind(t: str):
        t = (t or "").upper()
        if t.startswith("VOR") or t == "VORTAC":
            return "VOR"
        if t.startswith("NDB"):
            return "NDB"
        if t in ("DME", "TACAN"):
            return "DME"
        return None

    best: dict = {}
    for row in csv.DictReader(io.StringIO(csv_text)):
        try:
            la, lo = float(row["latitude_deg"]), float(row["longitude_deg"])
        except (ValueError, KeyError, TypeError):
            continue
        if not (lat_min <= la <= lat_max and lon_min <= lo <= lon_max):
            continue
        k = kind(row.get("type"))
        ident = (row.get("ident") or "").strip()
        if not k or not ident:
            continue
        if ident not in best or _PRIO[k] > _PRIO[best[ident]["kind"]]:
            best[ident] = {"id": ident, "kind": k, "lat": round(la, 5), "lon": round(lo, 5),
                           "name": (row.get("name") or "").title()}
    return sorted(best.values(), key=lambda n: n["id"])
RADIUS = 2.7                                                  # half-box (deg) around the airport
NAVDATA_OUT = os.environ.get("NAVDATA_PATH", "/config/navdata.json")
_SRC_DIR = os.environ.get("NAVDATA_SRC_DIR", "/config/.navdata-src")


def _build(fix_txt: str, nav_txt: str, awy_txt: str, lat: float, lon: float, radius: float) -> dict:
    lat_min, lat_max, lon_min, lon_max = lat - radius, lat + radius, lon - radius, lon + radius

    def inbox(la: float, lo: float) -> bool:
        return lat_min <= la <= lat_max and lon_min <= lo <= lon_max

    fixes = []
    for ln in fix_txt.splitlines():
        p = ln.split()
        if len(p) < 3:
            continue
        try:
            la, lo = float(p[0]), float(p[1])
        except ValueError:
            continue
        if inbox(la, lo):
            fixes.append({"id": p[2], "lat": round(la, 5), "lon": round(lo, 5)})

    navaids = []
    for ln in nav_txt.splitlines():
        p = ln.split()
        if len(p) < 9:
            continue
        try:
            t, la, lo = int(p[0]), float(p[1]), float(p[2])
        except ValueError:
            continue
        if t in _NAV_KIND and inbox(la, lo):
            navaids.append({"id": p[7], "kind": _NAV_KIND[t], "lat": round(la, 5),
                            "lon": round(lo, 5), "name": " ".join(p[8:]).title()})

    seg: dict = {}
    for ln in awy_txt.splitlines():
        p = ln.split()
        if len(p) < 10:
            continue
        try:
            la1, lo1, la2, lo2 = float(p[1]), float(p[2]), float(p[4]), float(p[5])
        except ValueError:
            continue
        if not (inbox(la1, lo1) or inbox(la2, lo2)):
            continue
        names = set(p[-1].replace("/", "-").split("-"))
        key = tuple(sorted([(round(la1, 5), round(lo1, 5)), (round(la2, 5), round(lo2, 5))]))
        if key in seg:
            seg[key]["names"] |= names
        else:
            seg[key] = {"a": [round(la1, 5), round(lo1, 5)],
                        "b": [round(la2, 5), round(lo2, 5)], "names": names}
    airways = [{"name": "/".join(sorted(s["names"])), "a": s["a"], "b": s["b"]} for s in seg.values()]

    best: dict = {}
    for n in navaids:                                        # one marker per ident (VOR > NDB > DME)
        if n["id"] not in best or _PRIO[n["kind"]] > _PRIO[best[n["id"]]["kind"]]:
            best[n["id"]] = n
    navaids = sorted(best.values(), key=lambda n: n["id"])

    awy_pts = set()                                          # keep only enroute fixes (on an airway)
    for s in seg.values():
        awy_pts.add((round(s["a"][0], 2), round(s["a"][1], 2)))
        awy_pts.add((round(s["b"][0], 2), round(s["b"][1], 2)))
    fixes = [f for f in fixes if (round(f["lat"], 2), round(f["lon"], 2)) in awy_pts]

    return {
        "meta": {"v": _CACHE_V, "cycle": "2012.08", "source": "X-Plane fixes/airways + OurAirports navaids",
                 "bbox": [round(lat_min, 4), round(lat_max, 4), round(lon_min, 4), round(lon_max, 4)]},
        "navaids": navaids, "fixes": fixes, "airways": airways,
    }


def _cached_for(out: str, lat: float, lon: float) -> bool:
    """True if `out` was already generated for ~this airport (bbox centred on lat/lon)."""
    try:
        with open(out) as f:
            meta = json.load(f).get("meta", {})
        bb = meta.get("bbox")
        return (meta.get("v") == _CACHE_V and bool(bb)     # old-version caches rebuild once
                and abs((bb[0] + bb[1]) / 2 - lat) < 0.05 and abs((bb[2] + bb[3]) / 2 - lon) < 0.05)
    except (OSError, ValueError, TypeError):
        return False


async def ensure_navdata(lat, lon, client, out: str = NAVDATA_OUT, src_dir: str = _SRC_DIR,
                         radius: float = RADIUS) -> None:
    """Build the overlay for (lat, lon) into `out` unless already cached for this airport.

    Downloads the X-Plane source once (cached in src_dir). Never raises — run as a startup task.
    """
    if not lat or not lon or _cached_for(out, lat, lon):
        return
    try:
        os.makedirs(src_dir, exist_ok=True)
        txt = {}
        for f in _FILES:
            p = os.path.join(src_dir, f)
            if not (os.path.exists(p) and os.path.getsize(p) > 1000):
                r = await client.get(f"{_REPO}/{f}", timeout=120)
                r.raise_for_status()
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write(r.text)
            with open(p, encoding="utf-8", errors="replace") as fh:
                txt[f] = fh.read()
        data = _build(txt["earth_fix.dat"], txt["earth_nav.dat"], txt["earth_awy.dat"], lat, lon, radius)
        # swap in CURRENT navaids from OurAirports (best-effort; keep the X-Plane ones on failure)
        try:
            p = os.path.join(src_dir, "navaids.csv")
            if not (os.path.exists(p) and os.path.getsize(p) > 1000):
                r = await client.get(_OA_NAVAIDS, timeout=120)
                r.raise_for_status()
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write(r.text)
            with open(p, encoding="utf-8", errors="replace") as fh:
                data["navaids"] = _navaids_from_csv(fh.read(), lat, lon, radius)
        except Exception as exc:  # noqa: BLE001
            print(f"[navdata] OurAirports navaids skipped (keeping X-Plane): {exc}")
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        tmp = out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, separators=(",", ":"))
        os.replace(tmp, out)
        print(f"[navdata] overlay built for {lat:.3f},{lon:.3f}: "
              f"{len(data['navaids'])} navaids, {len(data['fixes'])} fixes, {len(data['airways'])} airways")
    except Exception as exc:  # noqa: BLE001 — overlay is optional; never break startup
        print(f"[navdata] overlay build skipped: {exc}")
