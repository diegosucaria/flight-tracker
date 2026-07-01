"""Home-airport METAR + per-runway wind.

Fetches the airport observation from aviationweather.gov (open, no key, worldwide
ICAO coverage) and computes the head/cross-wind on each runway so the map can show
which runway the wind favours. Cached with a short TTL — METARs update ~hourly.
"""
from __future__ import annotations

import math
import time

_METAR_URL = "https://aviationweather.gov/api/data/metar"
_TTL_S = 600.0                      # refresh at most every 10 min
_cache: dict = {"t": 0.0, "obs": None, "icao": None}


def _runway_winds(wdir, wspd, runways: list[dict]) -> list[dict]:
    """Head/cross-wind (kt) on each runway for wind ``wspd`` FROM ``wdir`` degrees.

    headwind = wspd·cos(wind − runway heading): + favours landing/taking off that way,
    − is a tailwind. crosswind = wspd·sin(…): magnitude + which side it pushes.
    The runway with the most headwind is flagged ``favored``.
    """
    out: list[dict] = []
    if not isinstance(wdir, (int, float)) or not isinstance(wspd, (int, float)):
        return out
    for r in runways:
        brg = r.get("brg")
        if not isinstance(brg, (int, float)):
            continue
        ang = math.radians(wdir - brg)
        head = wspd * math.cos(ang)
        cross = wspd * math.sin(ang)
        out.append({
            "id": r.get("id"), "brg": brg,
            "headwind": round(head), "tailwind": head < -0.5,
            # cross = wspd·sin(wind − heading): +θ means wind is clockwise of the nose,
            # i.e. blowing FROM the right; −θ is from the left.
            "crosswind": round(abs(cross)), "cross_from": "R" if cross > 0 else "L",
        })
    if out:
        max(out, key=lambda x: x["headwind"])["favored"] = True
    return out


async def get_metar(icao: str, runways: list[dict], client) -> dict | None:
    """Return {icao, raw, obs_time, wind_dir, wind_speed_kt, gust_kt, variable,
    temp_c, qnh_hpa, runways:[…]} for ``icao``, or None. Never raises."""
    icao = (icao or "").strip().upper()
    if not icao:
        return None

    now = time.monotonic()
    fresh = (_cache["obs"] is not None and _cache["icao"] == icao
             and now - _cache["t"] < _TTL_S)
    if not fresh:
        try:
            r = await client.get(_METAR_URL, params={"ids": icao, "format": "json"}, timeout=15)
            r.raise_for_status()
            rows = r.json()
        except Exception:
            rows = None
        if rows:
            m = rows[0]
            wdir = m.get("wdir")
            _cache.update(t=now, icao=icao, obs={
                "icao": icao, "raw": m.get("rawOb"), "obs_time": m.get("obsTime"),
                # wdir is "VRB" (a string) when winds are variable
                "wind_dir": wdir if isinstance(wdir, (int, float)) else None,
                "variable": not isinstance(wdir, (int, float)) and wdir is not None,
                "wind_speed_kt": m.get("wspd") if isinstance(m.get("wspd"), (int, float)) else None,
                "gust_kt": m.get("wgst") if isinstance(m.get("wgst"), (int, float)) else None,
                "temp_c": m.get("temp"), "qnh_hpa": m.get("altim"),
            })
        elif _cache["icao"] != icao:
            return None                      # no data and nothing cached for this field

    obs = _cache["obs"]
    if not obs or obs.get("icao") != icao:
        return None
    return {**obs, "runways": _runway_winds(obs.get("wind_dir"), obs.get("wind_speed_kt"), runways)}
