"""The logbook: what goes in, what comes back, and what must never happen.

The last of those is the important one - history.py is allowed to lose the
record, but never to take the socket down with it.
"""

import contextlib
import io
import json
import shutil
import sqlite3
import tempfile
import time
import unittest
from datetime import date, datetime, time as clock, timedelta
from pathlib import Path

import history


@contextlib.contextmanager
def quiet():
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        yield buffer


class Booked(unittest.TestCase):
    """A throwaway logbook per test."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="tapo-tests-"))
        self.book = history.Logbook(self.dir / "history.db", retention_days=30)

    def tearDown(self):
        self.book.close()
        shutil.rmtree(self.dir, ignore_errors=True)


class Samples(Booked):

    def test_a_sample_comes_back_as_it_went_in(self):
        self.book.sample(phase="night", target_kwh=2.4, free_kwh=2.0, reason="buying 4.0 kWh")
        row = self.book.latest_sample()
        self.assertEqual(row["phase"], "night")
        self.assertEqual(row["target_kwh"], 2.4)
        self.assertEqual(row["reason"], "buying 4.0 kWh")

    def test_booleans_are_stored_as_numbers(self):
        # SQLite has no boolean, and the page reads these as 0/1.
        self.book.sample(wanted=True, socket_on=False)
        row = self.book.latest_sample()
        self.assertEqual(row["wanted"], 1)
        self.assertEqual(row["socket_on"], 0)

    def test_an_unknown_reading_is_left_null(self):
        self.book.sample(phase="idle")
        self.assertIsNone(self.book.latest_sample()["target_kwh"])

    def test_an_unrecognised_field_is_ignored_not_fatal(self):
        # A field added to the loop before the schema must not stop recording.
        self.book.sample(phase="idle", something_new=7)
        self.assertEqual(self.book.latest_sample()["phase"], "idle")

    def test_every_declared_field_can_be_written(self):
        self.book.sample(**{name: 1 for name in history.SAMPLE_FIELDS})
        self.assertIsNotNone(self.book.latest_sample())

    def test_samples_come_back_oldest_first(self):
        now = time.time()
        self.book.sample(at=now - 10, reason="first")
        self.book.sample(at=now, reason="second")
        self.assertEqual([r["reason"] for r in self.book.samples(now - 60)],
                         ["first", "second"])

    def test_older_than_the_horizon_is_not_returned(self):
        now = time.time()
        self.book.sample(at=now - 7200, reason="old")
        self.book.sample(at=now, reason="new")
        self.assertEqual([r["reason"] for r in self.book.samples(now - 3600)], ["new"])

    def test_an_empty_logbook_reads_as_nothing(self):
        self.assertEqual(self.book.samples(0), [])
        self.assertIsNone(self.book.latest_sample())


class Events(Booked):

    def fill(self):
        now = time.time()
        self.book.event("socket is ON", "info", "plug", at=now - 30)
        self.book.event("forecast unavailable: timed out", "warn", "forecast", at=now - 20)
        self.book.event("turn ON failed", "error", "plug", at=now - 10)
        return now

    def test_newest_first(self):
        self.fill()
        self.assertEqual(self.book.events(0)[0]["message"], "turn ON failed")

    def test_filtered_by_level(self):
        self.fill()
        self.assertEqual([e["level"] for e in self.book.events(0, level="warn")], ["warn"])

    def test_an_unknown_level_does_not_filter(self):
        self.fill()
        self.assertEqual(len(self.book.events(0, level="shouting")), 3)

    def test_searched_by_message(self):
        self.fill()
        self.assertEqual(len(self.book.events(0, search="forecast")), 1)

    def test_searched_by_category(self):
        self.fill()
        self.assertEqual(len(self.book.events(0, search="plug")), 2)

    def test_paged_with_before(self):
        now = self.fill()
        self.assertEqual([e["message"] for e in self.book.events(0, before=now - 15)],
                         ["forecast unavailable: timed out", "socket is ON"])

    def test_limited(self):
        self.fill()
        self.assertEqual(len(self.book.events(0, limit=2)), 2)

    def test_a_level_nobody_recognises_is_filed_as_info(self):
        self.book.event("something", level="disaster")
        self.assertEqual(self.book.events(0)[0]["level"], "info")

    def test_the_default_is_an_info_line_about_the_system(self):
        self.book.event("started")
        row = self.book.events(0)[0]
        self.assertEqual((row["level"], row["category"]), ("info", "system"))


def outlook(day, kws, pv=None):
    base = datetime.combine(day, clock(10))
    return {
        "day": day,
        "pv_kwh": sum(kws) if pv is None else pv,
        "peak_kw": max(kws),
        "cloud_cover": 20.0,
        "rain_mm": 0.0,
        "hours": [
            {"time": base + timedelta(hours=i), "kw": kw, "gti": kw * 100,
             "cloud_cover": 20, "precipitation": 0.0, "precipitation_probability": 5}
            for i, kw in enumerate(kws)
        ],
    }


class Forecasts(Booked):

    def test_the_curve_is_kept_not_just_the_total(self):
        self.book.forecast(outlook(date(2026, 9, 13), [1.0, 2.0, 3.0]))
        row = self.book.latest_forecast()
        self.assertEqual(row["pv_kwh"], 6.0)
        self.assertEqual(len(row["hours"]), 3)
        self.assertEqual(row["hours"][1]["kw"], 2.0)

    def test_the_hours_are_stored_as_text_and_read_back_as_data(self):
        self.book.forecast(outlook(date(2026, 9, 13), [1.0]))
        self.assertIsInstance(self.book.latest_forecast()["hours"], list)

    def test_refetching_a_day_replaces_it(self):
        day = date(2026, 9, 13)
        self.book.forecast(outlook(day, [1.0], pv=1.0))
        self.book.forecast(outlook(day, [4.0], pv=4.0))
        rows = self.book._execute(lambda conn: list(conn.execute("SELECT * FROM forecasts")))
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.book.latest_forecast()["pv_kwh"], 4.0)

    def test_the_latest_day_wins(self):
        self.book.forecast(outlook(date(2026, 9, 13), [1.0], pv=1.0))
        self.book.forecast(outlook(date(2026, 9, 14), [2.0], pv=2.0))
        self.assertEqual(self.book.latest_forecast()["day"], "2026-09-14")

    def test_no_forecast_yet_reads_as_nothing(self):
        self.assertIsNone(self.book.latest_forecast())

    def test_today_and_tomorrow_come_back_oldest_first(self):
        today = date.today()
        for offset in (0, 1):
            self.book.forecast(outlook(today + timedelta(days=offset), [1.0], pv=offset + 1.0))
        rows = self.book.forecasts(2)
        self.assertEqual([row["day"] for row in rows],
                         [today.isoformat(), (today + timedelta(days=1)).isoformat()])
        self.assertEqual([row["pv_kwh"] for row in rows], [1.0, 2.0])

    def test_a_day_already_over_is_left_out(self):
        today = date.today()
        self.book.forecast(outlook(today - timedelta(days=1), [9.0], pv=9.0))
        self.book.forecast(outlook(today, [1.0], pv=1.0))
        self.assertEqual([row["day"] for row in self.book.forecasts(2)], [today.isoformat()])

    def test_the_count_is_a_limit_not_a_promise(self):
        today = date.today()
        for offset in (0, 1, 2):
            self.book.forecast(outlook(today + timedelta(days=offset), [1.0]))
        self.assertEqual(len(self.book.forecasts(2)), 2)
        self.assertEqual(len(self.book.forecasts(9)), 3)

    def test_the_hours_are_data_here_too(self):
        self.book.forecast(outlook(date.today(), [1.0, 2.0]))
        self.assertEqual(self.book.forecasts(2)[0]["hours"][1]["kw"], 2.0)

    def test_nothing_stored_is_an_empty_list(self):
        self.assertEqual(self.book.forecasts(2), [])


def noon_today():
    """Midday local, so a day's rows never straddle midnight mid-test."""
    parts = time.localtime()
    return time.mktime(parts[:3] + (12, 0, 0, 0, 0, -1))


class Days(Booked):
    """Per-day totals, integrated from the samples rather than counted."""

    def fill(self):
        noon = noon_today()
        rows = [
            # ts,              phase,   on, target, delivered
            # The solar and idle targets are deliberately larger than the
            # night's: after SOLAR_END the watcher is judging tomorrow, so
            # only a night sample says what THIS night was asked to buy.
            (noon,             "solar", 1, 3.8,  1.0),
            (noon + 300,       "solar", 1, 3.8,  2.0),
            (noon + 600,       "solar", 1, 3.8,  2.5),   # then a long gap
            (noon + 600 + 1800, "idle", 0, 3.8,  2.5),
            (noon + 3000,      "night", 1, 2.0,  2.5),
            (noon + 3300,      "night", 0, 2.0,  3.5),
        ]
        for ts, phase, on, target, delivered in rows:
            self.book.sample(at=ts, phase=phase, socket_on=on, target_kwh=target,
                             delivered_kwh=delivered, pv_forecast_kwh=9.0)
        return noon

    def test_socket_time_is_the_gap_between_samples(self):
        self.fill()
        self.assertAlmostEqual(self.book.days()[0]["on_seconds"], 900.0)

    def test_a_gap_too_long_to_trust_is_not_counted(self):
        # 30 minutes between samples is a restart, not half an hour of heating.
        self.fill()
        self.assertAlmostEqual(self.book.days()[0]["solar_seconds"], 600.0)

    def test_the_two_tariffs_are_counted_apart(self):
        self.fill()
        day = self.book.days()[0]
        self.assertAlmostEqual(day["grid_seconds"], 300.0)
        self.assertAlmostEqual(day["solar_seconds"], 600.0)

    def test_the_meter_is_the_high_water_mark_of_the_day(self):
        self.fill()
        self.assertAlmostEqual(self.book.days()[0]["delivered_kwh"], 3.5)

    def test_the_nights_grid_share_is_kept(self):
        self.fill()
        self.assertAlmostEqual(self.book.days()[0]["target_kwh"], 2.0)

    def test_a_day_that_never_entered_the_night_window_has_no_share(self):
        self.book.sample(at=noon_today(), phase="solar", target_kwh=4.0)
        self.assertIsNone(self.book.days()[0]["target_kwh"])

    def test_the_samples_are_counted(self):
        self.fill()
        self.assertEqual(self.book.days()[0]["samples"], 6)

    def test_days_are_separate_and_in_order(self):
        noon = self.fill()
        self.book.sample(at=noon - 86400, phase="night", socket_on=1)
        days = self.book.days()
        self.assertEqual(len(days), 2)
        self.assertLess(days[0]["day"], days[1]["day"])

    def test_a_day_beyond_the_count_is_not_reported(self):
        noon = self.fill()
        self.book.sample(at=noon - 20 * 86400, phase="night", socket_on=1)
        self.assertEqual(len(self.book.days(count=3)), 1)

    def test_a_day_with_no_readings_reports_nothing_rather_than_zero(self):
        self.book.sample(at=noon_today(), phase="idle", socket_on=0)
        day = self.book.days()[0]
        self.assertIsNone(day["target_kwh"])
        self.assertIsNone(day["delivered_kwh"])


class Pruning(Booked):

    def test_old_rows_go(self):
        now = time.time()
        self.book.retention_days = 1
        self.book.sample(at=now - 2 * 86400, reason="ancient")
        self.book.event("ancient", at=now - 2 * 86400)
        self.book.forecast(outlook(date(2026, 9, 1), [1.0]))
        self.book._execute(lambda conn: conn.execute(
            "UPDATE forecasts SET ts = ?", (now - 2 * 86400,)))
        self.book.sample(at=now, reason="fresh")
        self.book.prune()
        self.assertEqual([r["reason"] for r in self.book.samples(0)], ["fresh"])
        self.assertEqual(self.book.events(0), [])
        self.assertIsNone(self.book.latest_forecast())

    def test_keeping_nothing_means_keeping_everything(self):
        # retention_days of 0 is "never prune", not "delete it all".
        self.book.retention_days = 0
        self.book.sample(at=time.time() - 400 * 86400, reason="ancient")
        self.book.prune()
        self.assertEqual(len(self.book.samples(0)), 1)


class Thinning(unittest.TestCase):
    """_thin: fewer points for a wide view, without losing what happened."""

    def rows(self, count, on_at=()):
        return [{"ts": 1000.0 + i * 60, "socket_on": 1 if i in on_at else 0, "wanted": 0}
                for i in range(count)]

    def test_a_short_run_is_left_alone(self):
        rows = self.rows(10)
        self.assertIs(history._thin(rows, 100), rows)

    def test_no_limit_means_no_thinning(self):
        # The CSV export asks for every row.
        rows = self.rows(500)
        self.assertIs(history._thin(rows, 0), rows)

    def test_a_long_run_is_thinned(self):
        kept = history._thin(self.rows(1000), 50)
        self.assertLess(len(kept), 200)

    def test_the_ends_are_kept(self):
        rows = self.rows(1000)
        kept = history._thin(rows, 50)
        self.assertIs(kept[0], rows[0])
        self.assertIs(kept[-1], rows[-1])

    def test_a_single_boost_in_a_month_survives(self):
        # The whole point: a five-minute run must not vanish into a bucket.
        rows = self.rows(1000, on_at={500})
        kept = history._thin(rows, 20)
        self.assertIn(rows[500], kept)
        self.assertIn(rows[501], kept)      # and the moment it went off again


class Migration(unittest.TestCase):
    """A Pi that has been running gets new columns, not a blank page."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="tapo-tests-"))
        self.path = self.dir / "history.db"

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def old_database(self):
        conn = sqlite3.connect(str(self.path))
        conn.executescript("""
            CREATE TABLE samples (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      REAL NOT NULL,
                soc     REAL,
                reason  TEXT
            );
            INSERT INTO samples (ts, soc, reason) VALUES (1.0, 50.0, 'from an older build');
        """)
        conn.commit()
        conn.close()

    def columns(self, book, table="samples"):
        return book._execute(lambda conn: {
            row["name"] for row in conn.execute("PRAGMA table_info(%s)" % table)})

    def test_missing_columns_are_added_on_startup(self):
        self.old_database()
        with quiet():
            book = history.Logbook(self.path)
        self.assertTrue(set(history.SAMPLE_FIELDS) <= self.columns(book))
        book.close()

    def test_the_old_rows_survive_with_a_blank_for_the_new_field(self):
        self.old_database()
        with quiet():
            book = history.Logbook(self.path)
        row = book.samples(0)[0]
        self.assertEqual(row["reason"], "from an older build")
        self.assertIsNone(row["free_kwh"])
        book.close()

    def test_the_addition_is_announced(self):
        self.old_database()
        with quiet() as log:
            history.Logbook(self.path).close()
        self.assertIn("added samples.free_kwh", log.getvalue())

    def test_a_missing_table_is_simply_created(self):
        self.old_database()
        with quiet():
            book = history.Logbook(self.path)
        self.assertIn("hours", self.columns(book, "forecasts"))
        book.close()

    def test_a_retired_column_is_left_where_it_is(self):
        # soc was dropped from SCHEMA when the inverter stopped being read.
        # Migration only ever adds, so an existing Pi keeps the column and its
        # old rows; nothing writes it again and it simply goes NULL.
        self.old_database()
        with quiet():
            book = history.Logbook(self.path)
        self.assertIn("soc", self.columns(book))
        self.assertEqual(book.samples(0)[0]["soc"], 50.0)
        book.close()

    def test_an_up_to_date_database_is_left_alone(self):
        with quiet():
            history.Logbook(self.path).close()
        with quiet() as log:
            history.Logbook(self.path).close()
        self.assertNotIn("added", log.getvalue())


class SchemaReading(unittest.TestCase):

    def test_every_table_is_found(self):
        self.assertEqual(set(history._schema_columns()), {"samples", "events", "forecasts"})

    def test_the_declared_fields_all_exist_on_samples(self):
        columns = {name for name, _ in history._schema_columns()["samples"]}
        self.assertTrue(set(history.SAMPLE_FIELDS) <= columns)

    def test_the_types_come_with_the_names(self):
        self.assertIn(("target_kwh", "REAL"), history._schema_columns()["samples"])


class BrokenDisk(unittest.TestCase):
    """A full or read-only card must not stop the socket being switched."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="tapo-tests-"))
        blocker = self.dir / "not-a-directory"
        blocker.write_text("this is a file, so nothing can live inside it")
        self.path = blocker / "history.db"

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_opening_it_does_not_raise(self):
        with quiet():
            history.Logbook(self.path)

    def test_writing_to_it_does_not_raise(self):
        with quiet():
            book = history.Logbook(self.path)
            book.sample(phase="night", target_kwh=2.4)
            book.event("socket is ON", "info", "plug")
            book.forecast(outlook(date(2026, 9, 13), [1.0]))
            book.prune()

    def test_reading_from_it_gives_empty_answers(self):
        with quiet():
            book = history.Logbook(self.path)
            self.assertEqual(book.samples(0), [])
            self.assertEqual(book.events(0), [])
            self.assertEqual(book.days(), [])
            self.assertIsNone(book.latest_sample())
            self.assertIsNone(book.latest_forecast())

    def test_the_trouble_is_reported_once_not_every_minute(self):
        with quiet() as log:
            book = history.Logbook(self.path)
            for _ in range(5):
                book.sample(phase="night")
        self.assertEqual(log.getvalue().count("not recording"), 1)

    def test_the_footer_still_has_something_to_show(self):
        with quiet():
            stats = history.Logbook(self.path).stats()
        self.assertEqual(stats["bytes"], 0)
        self.assertEqual(stats["samples"], 0)


class Stats(Booked):

    def test_it_counts_what_is_there(self):
        self.book.sample(phase="idle")
        self.book.sample(phase="idle")
        self.book.event("hello")
        stats = self.book.stats()
        self.assertEqual(stats["samples"], 2)
        self.assertEqual(stats["events"], 1)
        self.assertGreater(stats["bytes"], 0)

    def test_it_carries_the_horizon_and_the_oldest_row(self):
        now = time.time()
        self.book.sample(at=now - 100, phase="idle")
        stats = self.book.stats()
        self.assertEqual(stats["retention_days"], 30.0)
        self.assertAlmostEqual(stats["oldest"], now - 100, places=3)

    def test_it_survives_json(self):
        json.dumps(self.book.stats(), default=str)


if __name__ == "__main__":
    unittest.main()
