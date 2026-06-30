#!/usr/bin/env python3
"""Generate the map's aviation overlay (`app/static/navdata.json`) for YOUR airport.

Extracts the airways, navaids and enroute fixes around an airport from open X-Plane navdata
(ptsmonteiro/x-plane-navdata, GPL v3, ~2012 cycle) and writes a compact JSON the web UI loads
as toggleable "Airways / Navaids / Fixes" map layers. Run once during setup:

    python tools/build_navdata.py KSEA                      # by ICAO (coords from OurAirports)
    python tools/build_navdata.py --lat 47.45 --lon -122.31 # by explicit coordinates
    python tools/build_navdata.py EGLL --radius 2.5 --out app/static/navdata.json

Source data (~10 MB) is downloaded once into a cache dir next to this script. The overlay is
OPTIONAL — if navdata.json is absent the app simply shows empty Airways/Navaids/Fixes layers.

Data is the 2012 cycle, so terminal/procedure waypoints may differ from current charts; the
enroute airway network is stable. Generated output inherits the source's GPL-v3 license.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.request

_REPO = "https://raw.githubusercontent.com/ptsmonteiro/x-plane-navdata/master"
_SRC = {f: f"{_REPO}/{f}" for f in ("earth_fix.dat", "earth_nav.dat", "earth_awy.dat")}
_AIRPORTS_CSV = "https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/airports.csv"
_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".navdata-cache")
_NAV_KIND = {2: "NDB", 3: "VOR", 13: "DME"}
_PRIO = {"VOR": 3, "NDB": 2, "DME": 1}


def _download(url: str, path: str) -> None:
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        return
    print(f"  downloading {os.path.basename(path)} …")
    urllib.request.urlretrieve(url, path)


def _airport_coords(icao: str) -> tuple[float, float] | None:
    """Look up an ICAO in OurAirports (public domain) -> (lat, lon)."""
    icao = icao.strip().upper()
    path = os.path.join(_CACHE, "airports.csv")
    _download(_AIRPORTS_CSV, path)
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ident = (row.get("icao_code") or row.get("ident") or "").strip().upper()
            if ident == icao:
                return float(row["latitude_deg"]), float(row["longitude_deg"])
    return None


def build(lat: float, lon: float, radius: float, out_path: str) -> dict:
    lat_min, lat_max = lat - radius, lat + radius
    lon_min, lon_max = lon - radius, lon + radius

    def in_box(la: float, lo: float) -> bool:
        return lat_min <= la <= lat_max and lon_min <= lo <= lon_max

    os.makedirs(_CACHE, exist_ok=True)
    for name, url in _SRC.items():
        _download(url, os.path.join(_CACHE, name))

    def _path(name: str) -> str:
        return os.path.join(_CACHE, name)

    # --- fixes (earth_fix.dat): "lat lon ident region" ---
    fixes = []
    with open(_path("earth_fix.dat"), encoding="utf-8", errors="replace") as fh:
        for ln in fh:
            p = ln.split()
            if len(p) < 3:
                continue
            try:
                la, lo = float(p[0]), float(p[1])
            except ValueError:
                continue
            if in_box(la, lo):
                fixes.append({"id": p[2], "lat": round(la, 5), "lon": round(lo, 5)})

    # --- navaids (earth_nav.dat): "type lat lon elev freq range var ident NAME…" ---
    navaids = []
    with open(_path("earth_nav.dat"), encoding="utf-8", errors="replace") as fh:
        for ln in fh:
            p = ln.split()
            if len(p) < 9:
                continue
            try:
                t = int(p[0]); la = float(p[1]); lo = float(p[2])
            except ValueError:
                continue
            if t in _NAV_KIND and in_box(la, lo):
                navaids.append({"id": p[7], "kind": _NAV_KIND[t], "lat": round(la, 5),
                                "lon": round(lo, 5), "name": " ".join(p[8:]).title()})

    # --- airways (earth_awy.dat): "id1 lat1 lon1 id2 lat2 lon2 layer base top NAME" ---
    # This cycle carries inline endpoint coords, so no ident->coord resolution is needed.
    seg: dict = {}
    with open(_path("earth_awy.dat"), encoding="utf-8", errors="replace") as fh:
        for ln in fh:
            p = ln.split()
            if len(p) < 10:
                continue
            try:
                la1, lo1, la2, lo2 = float(p[1]), float(p[2]), float(p[4]), float(p[5])
            except ValueError:
                continue
            if not (in_box(la1, lo1) or in_box(la2, lo2)):
                continue
            names = set(p[-1].replace("/", "-").split("-"))     # "UW6-UN857" -> {UW6, UN857}
            key = tuple(sorted([(round(la1, 5), round(lo1, 5)), (round(la2, 5), round(lo2, 5))]))
            if key in seg:
                seg[key]["names"] |= names
            else:
                seg[key] = {"a": [round(la1, 5), round(lo1, 5)],
                            "b": [round(la2, 5), round(lo2, 5)], "names": names}

    airways = [{"name": "/".join(sorted(s["names"])), "a": s["a"], "b": s["b"]}
               for s in seg.values()]

    # Dedupe navaids that share an ident (a station's NDB + co-located VOR both read alike):
    # keep the most useful (VOR > NDB > DME).
    best: dict = {}
    for n in navaids:
        if n["id"] not in best or _PRIO[n["kind"]] > _PRIO[best[n["id"]]["kind"]]:
            best[n["id"]] = n
    navaids = sorted(best.values(), key=lambda n: n["id"])

    # Keep only ENROUTE fixes — those that are an endpoint of some airway segment. Drops the
    # terminal SID/STAR clutter, leaving the stable fixes that lie on the airways.
    awy_pts = set()
    for s in seg.values():
        awy_pts.add((round(s["a"][0], 2), round(s["a"][1], 2)))
        awy_pts.add((round(s["b"][0], 2), round(s["b"][1], 2)))
    fixes = [f for f in fixes if (round(f["lat"], 2), round(f["lon"], 2)) in awy_pts]

    out = {
        "meta": {"cycle": "2012.08", "source": "X-Plane navdata (GPL v3)",
                 "bbox": [round(lat_min, 4), round(lat_max, 4), round(lon_min, 4), round(lon_max, 4)]},
        "navaids": navaids, "fixes": fixes, "airways": airways,
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print(f"wrote {out_path}: {len(navaids)} navaids, {len(fixes)} enroute fixes, "
          f"{len(airways)} airway segments")
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Build the map navdata overlay for an airport.")
    ap.add_argument("icao", nargs="?", help="airport ICAO, e.g. KSEA (coords via OurAirports)")
    ap.add_argument("--lat", type=float, help="airport latitude (instead of ICAO)")
    ap.add_argument("--lon", type=float, help="airport longitude (instead of ICAO)")
    ap.add_argument("--radius", type=float, default=2.7, help="half-box in degrees (default 2.7)")
    default_out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "app", "static", "navdata.json")
    ap.add_argument("--out", default=default_out, help="output path (default app/static/navdata.json)")
    args = ap.parse_args(argv)

    if args.lat is not None and args.lon is not None:
        lat, lon = args.lat, args.lon
    elif args.icao:
        coords = _airport_coords(args.icao)
        if not coords:
            print(f"airport {args.icao!r} not found in OurAirports", file=sys.stderr)
            return 2
        lat, lon = coords
        print(f"{args.icao.upper()} → {lat:.4f}, {lon:.4f}")
    else:
        ap.error("give an ICAO (e.g. KSEA) or both --lat and --lon")
        return 2

    build(lat, lon, args.radius, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
