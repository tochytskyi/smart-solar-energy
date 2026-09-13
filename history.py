"""Durable record of what the watcher saw and did.

Three tables in one SQLite file:

  samples    one row per decision pass - the numbers that went into the call
             and the call itself. This is what the dashboard charts.
  events     the lines that used to only ever reach stdout: plug switches,
             cloud outages, start-up banners. This is what it tails.
  forecasts  the hourly solar outlook as it was fetched, one row per day, so
             the chart can draw the curve the decision was actually made on.

SQLite rather than a text file because the dashboard asks for "the last three
days" and "only the warnings" without re-reading megabytes, and because one
file is easy to copy off the Pi and easy to delete.

Writes arrive from two threads - the asyncio loop and the HTTP server - so
each thread gets its own connection and WAL keeps readers out of the writer's
way. Nothing in here is allowed to raise: a broken SD card must not take the
socket controller down with it, so every statement is wrapped and failures are
reported once and then swallowed.
"""

import json
import re
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  REAL    NOT NULL,
    phase               TEXT,
    strategy            TEXT,
    pv_forecast_kwh     REAL,
    peak_kw             REAL,
    spare_kw            REAL,
    cloud_cover         REAL,
    rain_mm             REAL,
    wet_fraction        REAL,
    plug_power_w        REAL,
    delivered_kwh       REAL,
    free_kwh            REAL,
    target_kwh          REAL,
    wanted              INTEGER,
    socket_on           INTEGER,
    reason              TEXT
);
CREATE INDEX IF NOT EXISTS samples_ts ON samples (ts);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    level       TEXT NOT NULL,
    category    TEXT NOT NULL,
    message     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);

CREATE TABLE IF NOT EXISTS forecasts (
    day         TEXT PRIMARY KEY,
    ts          REAL NOT NULL,
    pv_kwh      REAL,
    peak_kw     REAL,
    cloud_cover REAL,
    rain_mm     REAL,
    hours       TEXT
);
"""

# Every column a sample may carry, in insert order. Anything absent is NULL.
SAMPLE_FIELDS = (
    "phase", "strategy", "pv_forecast_kwh", "peak_kw", "spare_kw",
    "cloud_cover", "rain_mm", "wet_fraction", "plug_power_w", "delivered_kwh",
    "free_kwh", "target_kwh", "wanted", "socket_on", "reason",
)

LEVELS = ("info", "warn", "error")

# A gap longer than this between two samples is downtime, not socket time, so
# the daily totals stop counting across it.
MAX_ATTRIBUTED_GAP = 900.0


class Logbook:
    """The on-disk history, and every question the dashboard asks of it."""

    def __init__(self, path, retention_days=30):
        self.path = Path(path)
        self.retention_days = float(retention_days)
        self._local = threading.local()
        self._lock = threading.Lock()       # one writer at a time, many readers
        self._broken = None                 # last error, reported once
        self._run_started = time.time()
        self._execute(self._create)

    def _create(self, conn):
        conn.executescript(SCHEMA)
        self._migrate(conn)

    def _migrate(self, conn):
        """Bring a database written by an older version up to SCHEMA.

        CREATE TABLE IF NOT EXISTS does nothing to a table that already
        exists, so a column added to SCHEMA would stay missing on every Pi
        that has been running - and the dashboard would show a blank where
        the new number should be. Adding the columns here means a new field
        appears on the next restart, with history before it left NULL, and
        no one has to delete the file.
        """
        for table, columns in _schema_columns().items():
            have = {row["name"] for row in conn.execute("PRAGMA table_info(%s)" % table)}
            if not have:
                continue                # the CREATE above made it; nothing to add
            for name, kind in columns:
                if name not in have:
                    conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, kind))
                    print("[history] added %s.%s" % (table, name), flush=True)

    # -- plumbing -----------------------------------------------------------

    def _conn(self):
        """This thread's connection, opened on first use."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            if not self.path.parent.exists():
                self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return conn

    def _execute(self, work, default=None):
        """Run one unit of database work, never letting it escape as an error."""
        try:
            result = work(self._conn())
        except (sqlite3.Error, OSError) as exc:
            # Repeats are expected once the disk is unhappy; say it once.
            # A read-only volume or a full card must not stop the socket from
            # being switched, so the failure is reported and then swallowed.
            if str(exc) != self._broken:
                self._broken = str(exc)
                print("[history] %s: %s - not recording" % (self.path, exc), flush=True)
            return default
        if self._broken is not None:
            print("[history] writing again", flush=True)
            self._broken = None
        return result

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- writing ------------------------------------------------------------

    def event(self, message, level="info", category="system", at=None):
        """Record one log line."""
        row = (at or time.time(), level if level in LEVELS else "info", category, message)
        with self._lock:
            self._execute(lambda conn: conn.execute(
                "INSERT INTO events (ts, level, category, message) VALUES (?, ?, ?, ?)", row))

    def sample(self, at=None, **fields):
        """Record one decision pass. Unknown keys are ignored, missing ones NULL."""
        values = [at or time.time()]
        for name in SAMPLE_FIELDS:
            value = fields.get(name)
            values.append(int(value) if isinstance(value, bool) else value)
        columns = ", ".join(("ts",) + SAMPLE_FIELDS)
        holders = ", ".join("?" * (len(SAMPLE_FIELDS) + 1))
        with self._lock:
            self._execute(lambda conn: conn.execute(
                "INSERT INTO samples (%s) VALUES (%s)" % (columns, holders), values))

    def forecast(self, outlook):
        """Store the hourly outlook for a day, replacing any earlier fetch."""
        hours = [
            {
                "time": hour["time"].isoformat(timespec="minutes"),
                "kw": round(hour["kw"], 3),
                "gti": round(hour["gti"] or 0.0, 1),
                "cloud_cover": hour["cloud_cover"],
                "precipitation": hour["precipitation"],
                "precipitation_probability": hour["precipitation_probability"],
            }
            for hour in outlook["hours"]
        ]
        row = (
            outlook["day"].isoformat(), time.time(), outlook["pv_kwh"], outlook["peak_kw"],
            outlook["cloud_cover"], outlook["rain_mm"], json.dumps(hours),
        )
        with self._lock:
            self._execute(lambda conn: conn.execute(
                "INSERT INTO forecasts (day, ts, pv_kwh, peak_kw, cloud_cover, rain_mm, hours)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(day) DO UPDATE SET"
                " ts=excluded.ts, pv_kwh=excluded.pv_kwh, peak_kw=excluded.peak_kw,"
                " cloud_cover=excluded.cloud_cover, rain_mm=excluded.rain_mm,"
                " hours=excluded.hours", row))

    def prune(self):
        """Drop everything past the retention horizon and reclaim the pages."""
        if self.retention_days <= 0:
            return
        cutoff = time.time() - self.retention_days * 86400.0

        def work(conn):
            conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
            conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            conn.execute("DELETE FROM forecasts WHERE ts < ?", (cutoff,))
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        with self._lock:
            self._execute(work)

    # -- reading ------------------------------------------------------------

    def samples(self, since, max_points=1500):
        """Samples newer than `since`, thinned to at most max_points rows.

        Thinning keeps one row per time bucket, and always keeps a row where
        the socket changed state - a five-minute boost must not vanish just
        because the view is showing a month.
        """
        rows = self._execute(lambda conn: [
            dict(row) for row in conn.execute(
                "SELECT * FROM samples WHERE ts >= ? ORDER BY ts", (since,))
        ], default=[])
        return _thin(rows, max_points)

    def events(self, since, level=None, search=None, limit=400, before=None):
        """Newest-first events, optionally filtered by level and free text."""
        where = ["ts >= ?"]
        args = [since]
        if level in LEVELS:
            where.append("level = ?")
            args.append(level)
        if search:
            where.append("(message LIKE ? OR category LIKE ?)")
            args += ["%" + search + "%", "%" + search + "%"]
        if before:
            where.append("ts < ?")
            args.append(before)
        args.append(int(limit))
        sql = ("SELECT * FROM events WHERE %s ORDER BY ts DESC LIMIT ?" % " AND ".join(where))
        return self._execute(
            lambda conn: [dict(row) for row in conn.execute(sql, args)], default=[])

    def latest_sample(self):
        return self._execute(lambda conn: _one(
            conn.execute("SELECT * FROM samples ORDER BY ts DESC LIMIT 1")))

    def latest_forecast(self):
        row = self._execute(lambda conn: _one(
            conn.execute("SELECT * FROM forecasts ORDER BY day DESC LIMIT 1")))
        if row and row.get("hours"):
            row["hours"] = json.loads(row["hours"])
        return row

    def forecasts(self, count=2, first=None):
        """Today's curve and the next ones, oldest first.

        Days that have already finished are left out: the page wants what is
        still ahead, and what the roof actually did that morning is in the
        samples already.
        """
        day = first or time.strftime("%Y-%m-%d", time.localtime())
        rows = self._execute(lambda conn: [dict(row) for row in conn.execute(
            "SELECT * FROM forecasts WHERE day >= ? ORDER BY day LIMIT ?",
            (day, int(count)))], default=[])
        for row in rows:
            if row.get("hours"):
                row["hours"] = json.loads(row["hours"])
        return rows

    def days(self, count=14):
        """Per-day totals: how long the socket ran, what it took, how the day looked.

        Socket time is integrated from the samples themselves rather than
        counted in rows, because the sample interval is not constant and a
        restart leaves a hole that must not be counted as anything.
        """
        since = time.time() - count * 86400.0
        rows = self._execute(lambda conn: [
            dict(row) for row in conn.execute(
                "SELECT ts, phase, spare_kw, socket_on, delivered_kwh, pv_forecast_kwh"
                " FROM samples WHERE ts >= ? ORDER BY ts", (since,))
        ], default=[])

        days = {}
        for index, row in enumerate(rows):
            day = time.strftime("%Y-%m-%d", time.localtime(row["ts"]))
            entry = days.setdefault(day, {
                "day": day, "on_seconds": 0.0, "grid_seconds": 0.0, "solar_seconds": 0.0,
                "delivered_kwh": None, "spare_max": None,
                "pv_forecast_kwh": None, "samples": 0,
            })
            entry["samples"] += 1
            if index + 1 < len(rows):
                gap = rows[index + 1]["ts"] - row["ts"]
                if row["socket_on"] and 0 < gap <= MAX_ATTRIBUTED_GAP:
                    entry["on_seconds"] += gap
                    key = "grid_seconds" if row["phase"] == "night" else "solar_seconds"
                    entry[key] += gap
            if row["delivered_kwh"] is not None:
                entry["delivered_kwh"] = max(entry["delivered_kwh"] or 0.0, row["delivered_kwh"])
            if row["pv_forecast_kwh"] is not None:
                entry["pv_forecast_kwh"] = row["pv_forecast_kwh"]
            if row["spare_kw"] is not None:
                entry["spare_max"] = row["spare_kw"] if entry["spare_max"] is None \
                    else max(entry["spare_max"], row["spare_kw"])
        return [days[key] for key in sorted(days)]

    def stats(self):
        """Size of the record, for the footer."""
        counts = self._execute(lambda conn: _one(conn.execute(
            "SELECT (SELECT COUNT(*) FROM samples) AS samples,"
            "       (SELECT COUNT(*) FROM events) AS events,"
            "       (SELECT MIN(ts) FROM samples) AS oldest")), default={})
        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0
        return {
            "samples": (counts or {}).get("samples", 0),
            "events": (counts or {}).get("events", 0),
            "oldest": (counts or {}).get("oldest"),
            "bytes": size,
            "path": str(self.path),
            "retention_days": self.retention_days,
            "run_started": self._run_started,
        }


def _schema_columns():
    """{table: [(column, type), ...]} as declared in SCHEMA, in order."""
    tables = {}
    for match in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", SCHEMA, re.S):
        name, body = match.group(1), match.group(2)
        columns = []
        for line in body.strip().splitlines():
            parts = line.strip().rstrip(",").split()
            # Skip table constraints; a column line starts with its own name.
            if len(parts) >= 2 and parts[0].isidentifier() and parts[0].upper() not in ("PRIMARY", "UNIQUE"):
                columns.append((parts[0], parts[1]))
        tables[name] = columns
    return tables


def _one(cursor):
    row = cursor.fetchone()
    return dict(row) if row else None


def _thin(rows, max_points):
    """Keep at most max_points rows, preserving every socket transition."""
    if max_points <= 0 or len(rows) <= max_points:
        return rows

    span = rows[-1]["ts"] - rows[0]["ts"]
    bucket = max(span / float(max_points), 1.0)
    kept = []
    slot = None
    previous_state = None
    for row in rows:
        state = (row["socket_on"], row["wanted"])
        row_slot = int(row["ts"] // bucket)
        if state != previous_state or row_slot != slot or row is rows[-1]:
            kept.append(row)
            slot = row_slot
            previous_state = state
    return kept
