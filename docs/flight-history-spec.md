# Spec: Flight path trails + SQLite flight history

**Status:** design notes (implemented — kept for reference)
**Goal:** (1) draw each aircraft's recent **path/trail** on the map, and (2) persist **all observed flights** to a local SQLite DB so we can browse/replay history and compute stats (busiest hours, most-seen operators, daily counts, the airport's runway-usage history).

---

## 1. Motivation

Today the map shows only the *current* position dot per aircraft; when a plane leaves coverage its data is gone. Two asks:

- **Live trails** — see where each plane came from / is going (a fading polyline behind the dot), and especially the curved vectored approaches (a looping turn onto the active runway).
- **Durable history** — keep every flight so we can answer "what flew over today?", replay a track, and back the runway-inference with real data instead of a rolling in-memory deque.

These are two layers of the same data (a time series of positions per aircraft); build the live layer first, then persist it.

---

## 2. Data model

### 2.1 In-memory (live trails) — Phase 1

Per tracked `hex`, keep a bounded deque of recent fixes:

```python
# main.py module state
trails: dict[str, deque] = defaultdict(lambda: deque(maxlen=TRAIL_MAX_POINTS))  # hex -> [(ts, lat, lon, alt), ...]
```

- Append `(monotonic_or_epoch, lat, lon, alt_baro)` each tick for every aircraft that has a fix.
- `TRAIL_MAX_POINTS` ≈ 200 (a few minutes at ~1 Hz). Drop a hex from `trails` when it ages out of `all_aircraft` (no fix for > `TRAIL_TTL_SECONDS`, e.g. 120 s).
- Expose in `/api/aircraft` (and optionally `/ws`) as `trail: [[lat,lon],...]` per aircraft, decimated (see §5) to keep payloads small.

### 2.2 Persistent (SQLite) — Phase 2

Three tables. A **flight** = one continuous observation arc of one airframe (gap > `FLIGHT_GAP_MIN`, e.g. 15 min, starts a new flight row).

```sql
-- airframes seen (slowly-changing dimension)
CREATE TABLE aircraft (
  hex          TEXT PRIMARY KEY,          -- ICAO 24-bit, lowercase
  registration TEXT,
  type         TEXT,                      -- ICAO type (A320, ...)
  operator     TEXT,
  first_seen   INTEGER NOT NULL,          -- epoch s
  last_seen    INTEGER NOT NULL
);

-- one observation arc
CREATE TABLE flight (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  hex           TEXT NOT NULL REFERENCES aircraft(hex),
  callsign      TEXT,
  origin        TEXT,                     -- as enriched (may be null/corrected)
  destination   TEXT,
  route_corrected INTEGER DEFAULT 0,
  phase         TEXT,                     -- arrival | departure | overflight | unknown
  landing_runway   TEXT,                  -- inferred, if any
  departure_runway TEXT,                  -- inferred, if any (see runway spec)
  started_at    INTEGER NOT NULL,         -- epoch s of first fix in arc
  ended_at      INTEGER NOT NULL,
  min_alt       INTEGER,
  max_alt       INTEGER,
  closest_km    REAL,                     -- min distance to receiver
  window_visible INTEGER DEFAULT 0        -- did it pass the visible runway(s)
);

-- the track (one row per fix; the bulk of the data)
CREATE TABLE position (
  flight_id   INTEGER NOT NULL REFERENCES flight(id) ON DELETE CASCADE,
  ts          INTEGER NOT NULL,           -- epoch s
  lat         REAL NOT NULL,
  lon         REAL NOT NULL,
  alt_baro    INTEGER,
  gs          REAL,
  track       REAL,
  baro_rate   INTEGER
);
CREATE INDEX ix_position_flight ON position(flight_id, ts);
CREATE INDEX ix_flight_started  ON flight(started_at);
CREATE INDEX ix_flight_hex      ON flight(hex);
```

---

## 3. Ingestion

- A small `history.py` module owning a single `sqlite3` connection (check_same_thread=False; serialize writes through one asyncio task/queue since the app is single-process).
- In `tick()`, after annotation, enqueue each aircraft's fix. A dedicated writer drains the queue and:
  1. upsert `aircraft` (update `last_seen`, fill reg/type/operator when known),
  2. find/extend the open `flight` for that hex (or open a new one if the last fix is older than `FLIGHT_GAP_MIN`), update its aggregates,
  3. insert a `position` row (decimated — see §5).
- Close out a flight (set `phase`, runways, aggregates final) when its hex ages out.
- **Batch** inserts (executemany every N seconds or M rows) to limit SD-card writes.

---

## 4. Storage on the Pi (important — SD-card wear)

- DB lives on a **named volume** (e.g. `app_data:/data`, file `/data/flights.db`), NOT the image layer.
- `PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;` — fewer fsyncs, good crash-safety tradeoff.
- **Retention/pruning**: a daily job deletes `position` rows older than `POSITION_RETENTION_DAYS` (e.g. 30) but keeps `flight`/`aircraft` summary rows longer (e.g. 365 d). `VACUUM` weekly (or `auto_vacuum=INCREMENTAL`).
- Rough sizing: ~1 fix/s, ~40 aircraft peak, ~50 B/position → ~150 MB/month before pruning; decimation (§5) cuts this 3–5×.

---

## 5. Decimation (keep tracks light)

Don't store/emit every 1 Hz fix verbatim:

- **Time**: at most 1 fix / `MIN_FIX_INTERVAL_S` (e.g. 4 s) when straight & level.
- **Geometry**: keep a fix if heading changed > X° or alt changed > Y ft since the last *kept* fix (Douglas–Peucker-lite / "keep on turn") — preserves the shape of curved approaches while dropping straight-line redundancy.
- Live trail payloads use the same kept points.

---

## 6. API

```
GET /api/aircraft            -> add `trail: [[lat,lon],...]` per live aircraft (Phase 1)
GET /api/history?from=&to=&hex=&callsign=&phase=&visible=   -> paged flight list
GET /api/flight/{id}/track   -> full position array for replay
GET /api/stats?day=          -> counts by hour/operator/runway (Phase 3)
```

---

## 7. UI

- **Phase 1**: draw a `L.polyline` per aircraft from its `trail`, colored by altitude, fading toward the tail; remove with the dot. Toggle "show trails" in the UI.
- **Phase 2**: a History panel (table of flights, filterable) + click a row to draw its full track on the map (replay = animate the polyline by `ts`).
- **Phase 3**: a small stats view (today's count, busiest hour, runway split, top operators).

---

## 8. Phasing

1. **Live trails (in-memory only)** — `trails` deque + `/api/aircraft` `trail` field + map polylines. No DB. Immediately useful, zero storage risk.
2. **SQLite persistence** — `history.py`, schema, batched writer on a volume, WAL, retention. `/api/history` + `/api/flight/{id}/track` + replay.
3. **Stats + runway history** — aggregate queries; feed the runway inference from the DB instead of the in-memory deque.

---

## 9. Open questions

- Persist **all** aircraft, or only those entering the watch sector? (All gives better stats; sector-only saves space.) Lean: persist all with aggressive decimation + pruning.
- Replay UX: scrub bar vs auto-play.
- Do we want a `runway_usage` rollup table for fast "active runway by hour/day"?
