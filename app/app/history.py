"""Persistent flight history in SQLite (Phase 2 of docs/flight-history-spec.md).

Records every observed aircraft as: an airframe row, a per-arc flight row (a new flight
starts after a >FLIGHT_GAP_S gap), and a decimated position track. Writes run off the event
loop via asyncio.to_thread and are serialised by the caller (one ingest per tick). WAL +
synchronous=NORMAL spare the SD card; old positions are pruned. Fully optional: if the DB
can't be opened it disables itself and the rest of the app is unaffected.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import time

DB_PATH = os.environ.get("HISTORY_DB_PATH", "/data/flights.db")
FLIGHT_GAP_S = float(os.environ.get("HISTORY_FLIGHT_GAP_S", "900"))    # 15 min gap → new flight
MIN_FIX_INTERVAL_S = float(os.environ.get("HISTORY_MIN_FIX_S", "4"))   # decimate position rows
POSITION_RETENTION_DAYS = int(os.environ.get("HISTORY_POSITION_DAYS", "30"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS aircraft (
  hex TEXT PRIMARY KEY, registration TEXT, type TEXT, operator TEXT,
  first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS flight (
  id INTEGER PRIMARY KEY AUTOINCREMENT, hex TEXT NOT NULL, callsign TEXT,
  origin TEXT, destination TEXT, phase TEXT,
  landing_runway TEXT, departure_runway TEXT, window_visible INTEGER DEFAULT 0,
  started_at INTEGER NOT NULL, ended_at INTEGER NOT NULL,
  min_alt INTEGER, max_alt INTEGER, closest_km REAL);
CREATE TABLE IF NOT EXISTS position (
  flight_id INTEGER NOT NULL, ts INTEGER NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
  alt_baro INTEGER, gs REAL, track REAL, baro_rate INTEGER);
CREATE INDEX IF NOT EXISTS ix_position_flight ON position(flight_id, ts);
CREATE INDEX IF NOT EXISTS ix_flight_started ON flight(started_at);
CREATE INDEX IF NOT EXISTS ix_flight_hex ON flight(hex);
"""


def _i(x):
    return int(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def _phase(ac: dict):
    if ac.get("is_arrival"):
        return "arrival"
    if ac.get("is_departure"):
        return "departure"
    return "overflight"


class History:
    """Owns the write connection + the set of currently-open flight arcs (per hex)."""

    def __init__(self) -> None:
        self._conn: sqlite3.Connection | None = None
        self._open: dict[str, dict] = {}     # hex -> open-flight state
        self._last_prune = 0.0

    @property
    def enabled(self) -> bool:
        return self._conn is not None

    def connect(self) -> None:
        try:
            d = os.path.dirname(DB_PATH) or "."
            os.makedirs(d, exist_ok=True)
            self._conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=5)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA wal_autocheckpoint=1000")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            self._rehydrate()
            print(f"[history] SQLite ready at {DB_PATH} ({len(self._open)} open arc(s))")
        except Exception as exc:  # noqa: BLE001
            print(f"[history] disabled (DB open failed: {exc})")
            self._conn = None

    def _rehydrate(self) -> None:
        """Resume recently-open flight arcs after a restart so a quick bounce doesn't fork
        one real flight into two (balena redeploys/brown-outs are frequent)."""
        cutoff = int(time.time()) - int(FLIGHT_GAP_S)
        rows = self._conn.execute(
            """SELECT id,hex,ended_at,min_alt,max_alt,closest_km FROM flight
               WHERE ended_at > ?""", (cutoff,)).fetchall()
        for fid, hx, ended, mn, mx, ck in rows:
            self._open[hx] = {"id": fid, "last_ts": ended, "last_fix_ts": 0,
                              "min_alt": mn, "max_alt": mx, "closest_km": ck}

    def close(self) -> None:
        """Checkpoint + close the write connection on shutdown (keeps the -wal small)."""
        if self._conn is not None:
            with contextlib.suppress(Exception):
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._conn.close()
            self._conn = None

    def ingest(self, aircraft: list[dict], now: int) -> None:
        """One tick of aircraft → upsert airframe, open/extend the flight, decimate a fix.

        Runs in a worker thread (asyncio.to_thread); never raises into the caller.
        """
        if self._conn is None:
            return
        try:
            self._ingest(aircraft, now)
        except Exception as exc:  # noqa: BLE001
            print(f"[history] ingest error: {exc}")
            self._open.clear()    # a failed write may have rolled back open arcs → resync fresh

    def _ingest(self, aircraft: list[dict], now: int) -> None:
        cur = self._conn.cursor()
        for ac in aircraft:
            hx, lat, lon = ac.get("hex"), ac.get("lat"), ac.get("lon")
            if not hx or not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
                continue
            cur.execute(
                """INSERT INTO aircraft(hex,registration,type,operator,first_seen,last_seen)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(hex) DO UPDATE SET last_seen=excluded.last_seen,
                   registration=COALESCE(excluded.registration,aircraft.registration),
                   type=COALESCE(excluded.type,aircraft.type),
                   operator=COALESCE(excluded.operator,aircraft.operator)""",
                (hx, ac.get("registration"), ac.get("type"), ac.get("operator"), now, now))
            st = self._open.get(hx)
            if st is None or now - st["last_ts"] > FLIGHT_GAP_S:
                cur.execute("INSERT INTO flight(hex,callsign,started_at,ended_at) VALUES(?,?,?,?)",
                            (hx, (ac.get("flight") or "").strip() or None, now, now))
                st = self._open[hx] = {"id": cur.lastrowid, "last_ts": now, "last_fix_ts": 0,
                                       "min_alt": None, "max_alt": None, "closest_km": None}
            st["last_ts"] = now
            alt, dist = ac.get("alt_baro"), ac.get("distance_km")
            if isinstance(alt, (int, float)):
                st["min_alt"] = alt if st["min_alt"] is None else min(st["min_alt"], alt)
                st["max_alt"] = alt if st["max_alt"] is None else max(st["max_alt"], alt)
            if isinstance(dist, (int, float)):
                st["closest_km"] = dist if st["closest_km"] is None else min(st["closest_km"], dist)
            cur.execute(
                """UPDATE flight SET ended_at=?, callsign=COALESCE(?,callsign),
                   origin=COALESCE(?,origin), destination=COALESCE(?,destination),
                   phase=?, landing_runway=COALESCE(?,landing_runway),
                   departure_runway=COALESCE(?,departure_runway),
                   window_visible=MAX(window_visible,?), min_alt=?, max_alt=?, closest_km=?
                   WHERE id=?""",
                (now, (ac.get("flight") or "").strip() or None, ac.get("origin"),
                 ac.get("destination"), _phase(ac),
                 None if ac.get("runway_prior") else ac.get("landing_runway"),
                 ac.get("departure_runway"), 1 if ac.get("window_visible") else 0,
                 _i(st["min_alt"]), _i(st["max_alt"]), st["closest_km"], st["id"]))
            if now - st["last_fix_ts"] >= MIN_FIX_INTERVAL_S:
                cur.execute(
                    """INSERT INTO position(flight_id,ts,lat,lon,alt_baro,gs,track,baro_rate)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (st["id"], now, lat, lon, _i(alt), ac.get("gs"), ac.get("track"),
                     _i(ac.get("baro_rate"))))
                st["last_fix_ts"] = now
        self._conn.commit()
        for hx in [h for h, s in self._open.items() if now - s["last_ts"] > FLIGHT_GAP_S]:
            self._open.pop(hx, None)
        self._maybe_prune(now)

    def _maybe_prune(self, now: int) -> None:
        if now - self._last_prune < 3600:
            return
        self._last_prune = now
        with contextlib.suppress(Exception):
            cutoff = now - POSITION_RETENTION_DAYS * 86400
            while True:                       # batch the delete so it never holds the writer long
                cur = self._conn.execute(
                    "DELETE FROM position WHERE rowid IN "
                    "(SELECT rowid FROM position WHERE ts < ? LIMIT 5000)", (cutoff,))
                self._conn.commit()
                if cur.rowcount < 5000:
                    break
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # keep the -wal small

    # ---- reads (own short-lived connection; WAL allows concurrent readers) ----
    def recent_flights(self, limit: int = 50) -> list[dict]:
        return self._query(
            """SELECT f.id,f.hex,f.callsign,f.origin,f.destination,f.phase,f.landing_runway,
                      f.departure_runway,f.window_visible,f.started_at,f.ended_at,f.min_alt,
                      f.max_alt,f.closest_km,a.registration,a.type,a.operator
               FROM flight f LEFT JOIN aircraft a ON a.hex=f.hex
               ORDER BY f.started_at DESC LIMIT ?""", (max(1, min(500, limit)),))

    def recent_landing_runways(self, limit: int = 40) -> list[str]:
        """Recent CONFIRMED landing runways (newest first) — seeds the active-runway prior so
        it survives restarts. Priors aren't recorded, so this never reinforces itself."""
        rows = self._query(
            "SELECT landing_runway FROM flight WHERE landing_runway IS NOT NULL "
            "ORDER BY ended_at DESC LIMIT ?", (max(1, min(200, limit)),))
        return [r["landing_runway"] for r in rows]

    def flight_track(self, flight_id: int) -> list[dict]:
        return self._query(
            """SELECT ts,lat,lon,alt_baro,gs,track,baro_rate FROM position
               WHERE flight_id=? ORDER BY ts""", (flight_id,))

    def positions_between(self, start_ts: int, end_ts: int, limit: int = 500000) -> list[dict]:
        """All recorded (lat, lon, alt) fixes in [start_ts, end_ts] — feeds the coverage envelope."""
        return self._query(
            "SELECT lat,lon,alt_baro FROM position WHERE ts>=? AND ts<=? AND lat IS NOT NULL LIMIT ?",
            (int(start_ts), int(end_ts), int(limit)))

    def _query(self, sql: str, params: tuple) -> list[dict]:
        if self._conn is None:
            return []
        try:
            ro = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
            ro.row_factory = sqlite3.Row
            try:
                return [dict(r) for r in ro.execute(sql, params).fetchall()]
            finally:
                ro.close()
        except Exception as exc:  # noqa: BLE001
            print(f"[history] query error: {exc}")
            return []
