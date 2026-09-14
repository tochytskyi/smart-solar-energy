"""The status page's server: what each route answers, and what it refuses.

The dashboard is read-only and unauthenticated by design, so two of these
tests are guards rather than checks - nothing may write, and nothing that
belongs in .env may ever be served.
"""

import json
import shutil
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime, time as clock, timedelta
from pathlib import Path
from unittest import mock

import dashboard
import history
import main
from tests import SECRET


class Clamping(unittest.TestCase):
    """_int: a query string is a stranger's input, even on the LAN."""

    def test_a_plain_number(self):
        self.assertEqual(dashboard._int({"hours": ["12"]}, "hours", 24, 1, 100), 12)

    def test_a_missing_one_takes_the_default(self):
        self.assertEqual(dashboard._int({}, "hours", 24, 1, 100), 24)

    def test_nonsense_takes_the_default(self):
        self.assertEqual(dashboard._int({"hours": ["soon"]}, "hours", 24, 1, 100), 24)

    def test_an_empty_value_takes_the_default(self):
        self.assertEqual(dashboard._int({"hours": [""]}, "hours", 24, 1, 100), 24)

    def test_too_big_is_pulled_down(self):
        self.assertEqual(dashboard._int({"hours": ["99999"]}, "hours", 24, 1, 100), 100)

    def test_too_small_is_pulled_up(self):
        self.assertEqual(dashboard._int({"hours": ["-5"]}, "hours", 24, 1, 100), 1)

    def test_only_the_first_repeat_is_read(self):
        self.assertEqual(dashboard._int({"hours": ["3", "9"]}, "hours", 24, 1, 100), 3)


def fill(book):
    """A couple of hours of history, with one thing worth finding in it."""
    now = time.time()
    for index in range(6):
        book.sample(
            at=now - (6 - index) * 300, phase="night" if index < 3 else "solar",
            strategy="forecast", peak_kw=index * 1.0, plug_power_w=2400 if index < 3 else 0,
            socket_on=index < 3, wanted=index < 3,
            delivered_kwh=index * 0.5, free_kwh=2.0, target_kwh=4.0,
            reason='buying 4.0 kWh, "cheaply"' if index < 3 else "outside both windows")
    book.event("socket is ON", "info", "plug", at=now - 1800)
    book.event("forecast unavailable: timed out", "warn", "forecast", at=now - 900)
    for offset, pv in ((0, 9.0), (1, 4.0)):
        day = date.today() + timedelta(days=offset)
        book.forecast({
            "day": day, "pv_kwh": pv, "peak_kw": 3.0,
            "cloud_cover": 20.0, "rain_mm": 0.0,
            "hours": [{"time": datetime.combine(day, clock(10)) + timedelta(hours=i),
                       "kw": 1.5, "gti": 400, "cloud_cover": 20,
                       "precipitation": 0.0, "precipitation_probability": 5}
                      for i in range(4)],
        })


class Routes(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp(prefix="tapo-tests-"))
        cls.book = history.Logbook(cls.dir / "history.db", retention_days=30)
        fill(cls.book)
        cls.server = dashboard.serve("127.0.0.1", 0, cls.book, main.settings)
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.book.close()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def get(self, path, data=None):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        try:
            with urllib.request.urlopen(url, data=data, timeout=10) as response:
                return response.status, response.read().decode("utf-8"), response.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8"), exc.headers

    def json(self, path):
        status, body, _ = self.get(path)
        self.assertEqual(status, 200)
        return json.loads(body)

    # -- the page ---------------------------------------------------------

    def test_the_page_is_served(self):
        status, body, headers = self.get("/")
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith("<!doctype html>"))
        self.assertIn("text/html", headers["Content-Type"])

    # -- state ------------------------------------------------------------

    def test_state_carries_the_latest_of_everything(self):
        state = self.json("/api/state")
        self.assertEqual(state["sample"]["phase"], "solar")
        self.assertEqual(state["forecast"]["pv_kwh"], 9.0)
        self.assertEqual(state["settings"]["device_daily_kwh"], 6.0)
        self.assertEqual(state["stats"]["samples"], 6)
        self.assertTrue(state["recent"])

    def test_the_forecast_curve_comes_with_it(self):
        self.assertEqual(len(self.json("/api/state")["forecast"]["hours"]), 4)

    def test_tomorrow_comes_with_it_too(self):
        curves = self.json("/api/state")["forecasts"]
        self.assertEqual([curve["day"] for curve in curves],
                         [date.today().isoformat(),
                          (date.today() + timedelta(days=1)).isoformat()])

    def test_the_single_forecast_field_is_the_day_in_progress(self):
        state = self.json("/api/state")
        self.assertEqual(state["forecast"]["day"], date.today().isoformat())
        self.assertEqual(state["forecast"]["pv_kwh"], 9.0)

    def test_the_chart_is_given_both_curves_as_well(self):
        self.assertEqual(len(self.json("/api/samples?hours=6")["forecasts"]), 2)

    def test_a_trailing_slash_is_the_same_route(self):
        self.assertIn("sample", self.json("/api/state/"))

    # -- samples ----------------------------------------------------------

    def test_samples_are_returned_for_the_asked_window(self):
        payload = self.json("/api/samples?hours=2")
        self.assertEqual(payload["hours"], 2)
        self.assertEqual(len(payload["samples"]), 6)

    def test_a_silly_window_is_clamped_rather_than_refused(self):
        self.assertEqual(self.json("/api/samples?hours=999999")["hours"], 24 * 90)

    def test_the_window_is_reported_back_with_the_rows(self):
        # The page draws its x-axis from `since`, not from the first sample.
        payload = self.json("/api/samples?hours=6")
        self.assertAlmostEqual(payload["since"], time.time() - 6 * 3600, delta=5)

    # -- events -----------------------------------------------------------

    def test_events_come_back_newest_first(self):
        events = self.json("/api/events")["events"]
        self.assertEqual(events[0]["level"], "warn")

    def test_events_filter_by_level(self):
        events = self.json("/api/events?level=warn")["events"]
        self.assertEqual(len(events), 1)

    def test_events_filter_by_text(self):
        self.assertEqual(len(self.json("/api/events?q=forecast")["events"]), 1)

    def test_a_blank_search_is_not_a_filter(self):
        self.assertEqual(len(self.json("/api/events?q=")["events"]), 2)

    # -- days -------------------------------------------------------------

    def test_days_are_totalled(self):
        days = self.json("/api/days?days=2")["days"]
        self.assertEqual(len(days), 1)
        self.assertGreater(days[0]["on_seconds"], 0)

    # -- the export -------------------------------------------------------

    def test_the_csv_header_is_the_declared_columns(self):
        status, body, headers = self.get("/api/export.csv")
        self.assertEqual(status, 200)
        self.assertEqual(body.splitlines()[0], ",".join(dashboard.CSV_COLUMNS))
        self.assertIn("text/csv", headers["Content-Type"])
        self.assertIn("attachment", headers["Content-Disposition"])

    def test_every_sample_is_exported_not_just_the_thinned_ones(self):
        _, body, _ = self.get("/api/export.csv?hours=2")
        self.assertEqual(len(body.splitlines()), 7)         # header + six rows

    def test_the_timestamp_is_written_for_a_human(self):
        _, body, _ = self.get("/api/export.csv?hours=2")
        stamp = body.splitlines()[1].split(",")[0]
        self.assertRegex(stamp, r'^"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"$')

    def test_a_reason_with_commas_and_quotes_survives(self):
        _, body, _ = self.get("/api/export.csv?hours=2")
        self.assertIn('"buying 4.0 kWh, ""cheaply"""', body)

    # -- the guards -------------------------------------------------------

    def test_an_unknown_route_is_a_404(self):
        status, body, _ = self.get("/api/secrets")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"], "not found")

    def test_nothing_can_be_written_through_it(self):
        # The page is read-only by design: there is no handler for a POST.
        status, _, _ = self.get("/api/state", data=b"socket_on=1")
        self.assertEqual(status, 501)

    def test_no_credential_is_ever_served(self):
        for route in ("/api/state", "/api/samples", "/api/events", "/api/days"):
            _, body, _ = self.get(route)
            self.assertNotIn(SECRET, body)
            self.assertNotIn("@", body)

    def test_a_broken_query_answers_500_instead_of_killing_the_watcher(self):
        with mock.patch.object(self.book, "days", side_effect=RuntimeError("disk on fire")):
            status, body, _ = self.get("/api/days")
        self.assertEqual(status, 500)
        self.assertIn("disk on fire", json.loads(body)["error"])
        self.assertIn("sample", self.json("/api/state"))     # and it carries on


class Binding(unittest.TestCase):

    def test_the_reverse_dns_lookup_is_skipped(self):
        # HTTPServer.server_bind() calls getfqdn(), which stalls for seconds on
        # a LAN with no reverse DNS - before the watcher's first pass.
        book = None
        directory = Path(tempfile.mkdtemp(prefix="tapo-tests-"))
        try:
            book = history.Logbook(directory / "history.db")
            with mock.patch("socket.getfqdn", side_effect=AssertionError("looked up")):
                server = dashboard.serve("127.0.0.1", 0, book, main.settings)
            self.assertEqual(server.server_name, "127.0.0.1")
            server.shutdown()
            server.server_close()
        finally:
            if book:
                book.close()
            shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
