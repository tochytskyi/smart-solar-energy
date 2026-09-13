"""The sky: windows, the day a night window is betting on, and Open-Meteo.

Nothing here reaches the network - _fetch is driven through a fake urlopen so
the parsing, the padding and the failure paths can all be pinned down.
"""

import io
import json
import unittest
import urllib.error
from datetime import date, datetime, time as clock, timedelta
from unittest import mock

import solar_forecast
from solar_forecast import in_window, parse_hhmm, target_day, window_hours

DAY = date(2026, 9, 13)


def at(hour, minute=0):
    return clock(hour, minute)


class ParsingTimes(unittest.TestCase):

    def test_hh_mm(self):
        self.assertEqual(parse_hhmm("07:30"), at(7, 30))

    def test_midnight(self):
        self.assertEqual(parse_hhmm("00:00"), at(0))

    def test_a_bare_hour(self):
        self.assertEqual(parse_hhmm("7"), at(7))

    def test_surrounding_space(self):
        self.assertEqual(parse_hhmm("  18:00 "), at(18))


class Windows(unittest.TestCase):
    """in_window is half-open: the start is in, the end is out."""

    def test_inside(self):
        self.assertTrue(in_window(at(12), at(10), at(18)))

    def test_the_start_is_inside(self):
        self.assertTrue(in_window(at(10), at(10), at(18)))

    def test_the_end_is_outside(self):
        # So a 10:00-18:00 window is eight hourly rows, not nine.
        self.assertFalse(in_window(at(18), at(10), at(18)))

    def test_before_and_after(self):
        self.assertFalse(in_window(at(9, 59), at(10), at(18)))
        self.assertFalse(in_window(at(18, 1), at(10), at(18)))

    def test_a_window_that_wraps_past_midnight(self):
        self.assertTrue(in_window(at(23, 30), at(23), at(7)))
        self.assertTrue(in_window(at(0), at(23), at(7)))
        self.assertTrue(in_window(at(6, 59), at(23), at(7)))

    def test_outside_a_wrapping_window(self):
        self.assertFalse(in_window(at(7), at(23), at(7)))
        self.assertFalse(in_window(at(22, 59), at(23), at(7)))

    def test_a_zero_length_window_is_never_open(self):
        self.assertFalse(in_window(at(10), at(10), at(10)))


class WhichDayTheNightIsAbout(unittest.TestCase):

    def test_a_window_inside_one_date_means_today(self):
        now = datetime(2026, 9, 13, 3, 0)
        self.assertEqual(target_day(now, at(0), at(7)), DAY)

    def test_before_midnight_a_wrapping_window_bets_on_tomorrow(self):
        now = datetime(2026, 9, 13, 23, 30)
        self.assertEqual(target_day(now, at(23), at(7)), DAY + timedelta(days=1))

    def test_after_midnight_it_is_already_that_day(self):
        now = datetime(2026, 9, 13, 2, 0)
        self.assertEqual(target_day(now, at(23), at(7)), DAY)

    def test_the_switch_happens_at_the_window_start(self):
        self.assertEqual(target_day(datetime(2026, 9, 13, 23, 0), at(23), at(7)),
                         DAY + timedelta(days=1))
        self.assertEqual(target_day(datetime(2026, 9, 13, 22, 59), at(23), at(7)), DAY)


class PanelBearing(unittest.TestCase):
    """Compass degrees in, Open-Meteo's south-relative angle out."""

    def test_due_south(self):
        self.assertAlmostEqual(solar_forecast.compass_to_open_meteo(180), 0.0)

    def test_east_is_negative(self):
        self.assertAlmostEqual(solar_forecast.compass_to_open_meteo(90), -90.0)

    def test_west_is_positive(self):
        self.assertAlmostEqual(solar_forecast.compass_to_open_meteo(270), 90.0)

    def test_west_south_west(self):
        self.assertAlmostEqual(solar_forecast.compass_to_open_meteo(240), 60.0)

    def test_north_lands_on_one_end_of_the_range(self):
        self.assertAlmostEqual(solar_forecast.compass_to_open_meteo(0), -180.0)

    def test_it_is_passed_to_the_api(self):
        forecast = build(kwp=10)
        self.assertAlmostEqual(forecast._query["azimuth"], 0.0)


def build(**kwargs):
    options = dict(latitude=50.45, longitude=30.52, tilt=30, azimuth=180, kwp=10)
    options.update(kwargs)
    return solar_forecast.SolarForecast(**options)


def hour(when, kw=0.0, gti=0.0, cloud=None, rain=0.0, probability=0):
    return {
        "time": when, "kw": kw, "gti": gti, "cloud_cover": cloud,
        "precipitation": rain, "precipitation_probability": probability,
    }


def stocked(rows, **kwargs):
    """A forecast holding these rows, with the cache already warm."""
    forecast = build(**kwargs)
    forecast._hours = rows
    forecast._fetched_at = datetime.now()
    return forecast


class ArrayOutput(unittest.TestCase):

    def test_full_sun_scales_off_the_rating(self):
        # A kWp is defined at 1000 W/m2; 0.8 is the performance ratio.
        self.assertAlmostEqual(build(kwp=10)._power_kw(1000), 8.0)

    def test_half_the_irradiance_is_half_the_power(self):
        self.assertAlmostEqual(build(kwp=10)._power_kw(500), 4.0)

    def test_darkness_makes_nothing(self):
        self.assertAlmostEqual(build(kwp=10)._power_kw(0), 0.0)

    def test_the_inverter_clips_the_panels(self):
        self.assertAlmostEqual(build(kwp=10, inverter_kw=5)._power_kw(1000), 5.0)

    def test_the_clip_only_applies_at_the_top(self):
        self.assertAlmostEqual(build(kwp=10, inverter_kw=5)._power_kw(250), 2.0)


class Outlook(unittest.TestCase):

    def rows(self):
        # A row is stamped with the END of the hour it covers, so these start
        # at 09:00 to describe the hours from 08:00 onwards.
        base = datetime.combine(DAY, at(9))
        return [
            hour(base + timedelta(hours=i), kw=kw, cloud=cloud, rain=rain, probability=p)
            for i, (kw, cloud, rain, p) in enumerate([
                (0.5, 90, 0.0, 0),      # covers 08:00-09:00, before the window
                (1.0, 80, 0.0, 5),      # covers 09:00-10:00, before the window
                (2.0, 50, 0.0, 10),     # covers 10:00-11:00  <- the window starts
                (4.0, None, 0.0, 20),   # covers 11:00-12:00, cloud missing
                (3.0, 30, 0.4, 70),     # covers 12:00-13:00, wet
                (1.0, 70, 0.0, 80),     # covers 13:00-14:00, dry but likely
                (9.0, 10, 0.0, 0),      # covers 14:00-15:00, after the window
            ])
        ]

    def outlook(self, start=at(10), end=at(14)):
        return stocked(self.rows()).outlook(DAY, start, end)

    def test_only_the_window_is_counted(self):
        self.assertAlmostEqual(self.outlook()["pv_kwh"], 10.0)

    def test_each_row_is_an_hour_so_kw_sums_to_kwh(self):
        self.assertAlmostEqual(self.outlook(at(10), at(12))["pv_kwh"], 6.0)

    def test_the_peak_is_the_best_hour_in_the_window(self):
        self.assertAlmostEqual(self.outlook()["peak_kw"], 4.0)

    def test_cloud_is_averaged_over_the_hours_that_reported_it(self):
        self.assertAlmostEqual(self.outlook()["cloud_cover"], 50.0)

    def test_rain_is_totalled(self):
        self.assertAlmostEqual(self.outlook()["rain_mm"], 0.4)

    def test_the_day_comes_back_with_it(self):
        self.assertEqual(self.outlook()["day"], DAY)

    def test_the_hours_come_back_for_the_chart(self):
        self.assertEqual(len(self.outlook()["hours"]), 4)

    def test_a_row_is_stamped_with_the_end_of_the_hour_it_covers(self):
        """Open-Meteo's 11:00 row is the mean over 10:00-11:00, not a reading
        at 11:00, so a 10:00-12:00 window is the 11:00 and 12:00 rows.

        Selecting on the raw stamp instead shifts every window an hour early,
        which on a west-facing roof swaps the best afternoon hour for a dim
        morning one - worth about 5% of a day here, more when it is clear.
        """
        rows = [hour(datetime.combine(DAY, at(h)), kw=float(h)) for h in range(9, 16)]
        window = stocked(rows).outlook(DAY, at(10), at(12))
        self.assertEqual([row["time"].hour for row in window["hours"]], [11, 12])
        self.assertAlmostEqual(window["pv_kwh"], 23.0)

    def test_a_day_with_no_rows_is_an_error(self):
        with self.assertRaises(solar_forecast.ForecastError):
            stocked(self.rows()).outlook(DAY + timedelta(days=5), at(10), at(14))

    def test_an_all_cloudless_feed_reports_no_average(self):
        rows = [hour(datetime.combine(DAY, at(11)), kw=1.0)]   # covers 10:00-11:00
        self.assertIsNone(stocked(rows).outlook(DAY, at(10), at(11))["cloud_cover"])


class WetHours(unittest.TestCase):
    """The rain strategy's one number: how much of the window looks rainy."""

    def fraction(self, rows):
        forecast = stocked(rows)
        return forecast.wet_hour_fraction(forecast.outlook(DAY, at(10), at(14)), 0.1, 60)

    def rows(self, specs):
        # Stamped at 11:00 onwards so the four rows cover 10:00-14:00, the
        # window these tests ask about - a row's stamp is the end of its hour.
        base = datetime.combine(DAY, at(11))
        return [hour(base + timedelta(hours=i), kw=1.0, rain=rain, probability=p)
                for i, (rain, p) in enumerate(specs)]

    def test_a_dry_window(self):
        self.assertAlmostEqual(self.fraction(self.rows([(0.0, 0)] * 4)), 0.0)

    def test_a_soaked_window(self):
        self.assertAlmostEqual(self.fraction(self.rows([(2.0, 90)] * 4)), 1.0)

    def test_measurable_rain_counts(self):
        self.assertAlmostEqual(
            self.fraction(self.rows([(0.1, 0), (0.0, 0), (0.0, 0), (0.0, 0)])), 0.25)

    def test_a_likely_hour_counts_even_with_no_millimetres(self):
        self.assertAlmostEqual(
            self.fraction(self.rows([(0.0, 60), (0.0, 59), (0.0, 0), (0.0, 0)])), 0.25)

    def test_an_hour_is_only_counted_once(self):
        self.assertAlmostEqual(
            self.fraction(self.rows([(5.0, 95), (0.0, 0), (0.0, 0), (0.0, 0)])), 0.25)


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def local_offset_seconds():
    return datetime.now().astimezone().utcoffset().total_seconds()


def body(**overrides):
    payload = {
        "utc_offset_seconds": local_offset_seconds(),
        "timezone": "Europe/Kyiv",
        "hourly": {
            "time": ["2026-09-13T10:00", "2026-09-13T11:00"],
            "global_tilted_irradiance": [500, None],
            "cloud_cover": [10],                        # short on purpose
            "precipitation": [0.0, 1.2],
            "precipitation_probability": [5, 70],
        },
    }
    payload.update(overrides)
    return payload


def answering(payload=None, error=None):
    def urlopen(url, timeout=None):
        if error is not None:
            raise error
        return FakeResponse(payload)
    return mock.patch.object(solar_forecast.urllib.request, "urlopen", urlopen)


class Fetching(unittest.TestCase):

    def test_irradiance_becomes_power(self):
        with answering(body()):
            rows = build(kwp=10)._fetch()
        self.assertAlmostEqual(rows[0]["kw"], 4.0)

    def test_times_are_read_as_plain_local_times(self):
        with answering(body()):
            rows = build()._fetch()
        self.assertEqual(rows[0]["time"], datetime(2026, 9, 13, 10, 0))

    def test_a_missing_irradiance_is_darkness_not_a_crash(self):
        with answering(body()):
            rows = build()._fetch()
        self.assertEqual(rows[1]["gti"], 0.0)
        self.assertEqual(rows[1]["kw"], 0.0)

    def test_a_short_column_is_padded_rather_than_misaligned(self):
        # Open-Meteo can return fewer values than hours; the rows must still
        # line up with their timestamps.
        with answering(body()):
            rows = build()._fetch()
        self.assertEqual(rows[0]["cloud_cover"], 10)
        self.assertIsNone(rows[1]["cloud_cover"])

    def test_rain_and_probability_are_carried_through(self):
        with answering(body()):
            rows = build()._fetch()
        self.assertEqual(rows[1]["precipitation"], 1.2)
        self.assertEqual(rows[1]["precipitation_probability"], 70)

    def test_an_empty_feed_is_an_error(self):
        with answering(body(hourly={})):
            with self.assertRaises(solar_forecast.ForecastError):
                build()._fetch()

    def test_an_unreachable_api_is_an_error(self):
        with answering(error=OSError("nodename nor servname provided")):
            with self.assertRaisesRegex(solar_forecast.ForecastError, "unreachable"):
                build()._fetch()

    def test_an_http_error_carries_the_code(self):
        failure = urllib.error.HTTPError(
            "url", 429, "Too Many Requests", {}, io.BytesIO(b"slow down"))
        with answering(error=failure):
            with self.assertRaisesRegex(solar_forecast.ForecastError, "429"):
                build()._fetch()


class Caching(unittest.TestCase):
    """The loop asks every minute; Open-Meteo is asked once an hour."""

    def test_a_warm_cache_is_not_refetched(self):
        with answering(body()):
            forecast = build(refresh_seconds=3600)
            forecast.hours()
            with mock.patch.object(forecast, "_fetch", side_effect=AssertionError):
                self.assertEqual(len(forecast.hours()), 2)

    def test_a_cold_cache_fetches(self):
        calls = []
        forecast = build()
        with mock.patch.object(forecast, "_fetch", lambda: calls.append(1) or []):
            forecast.hours()
        self.assertEqual(len(calls), 1)

    def test_a_stale_cache_refetches(self):
        calls = []
        forecast = build(refresh_seconds=0)
        with mock.patch.object(forecast, "_fetch", lambda: calls.append(1) or []):
            forecast.hours()
            forecast.hours()
        self.assertEqual(len(calls), 2)


class ClockCheck(unittest.TestCase):
    """Every time here is naive local time, so a container on UTC must be caught."""

    def test_a_matching_clock_is_quiet(self):
        with answering(body()):
            forecast = build()
            forecast._fetch()
        self.assertIsNone(forecast.clock_skew)

    def test_a_clock_two_hours_out_is_flagged(self):
        with answering(body(utc_offset_seconds=local_offset_seconds() + 7200)):
            forecast = build()
            forecast._fetch()
        self.assertAlmostEqual(forecast.clock_skew["hours"], 2.0)
        self.assertEqual(forecast.clock_skew["forecast_timezone"], "Europe/Kyiv")

    def test_a_few_seconds_of_drift_is_not_a_skew(self):
        with answering(body(utc_offset_seconds=local_offset_seconds() + 30)):
            forecast = build()
            forecast._fetch()
        self.assertIsNone(forecast.clock_skew)

    def test_a_feed_without_an_offset_says_nothing(self):
        payload = body()
        del payload["utc_offset_seconds"]
        with answering(payload):
            forecast = build()
            forecast._fetch()
        self.assertIsNone(forecast.clock_skew)


class WindowLength(unittest.TestCase):
    """window_hours turns HOUSE_DAYTIME_KWH into the kW the house takes."""

    def test_an_ordinary_window(self):
        self.assertAlmostEqual(window_hours(at(10), at(18)), 8.0)

    def test_a_window_that_wraps_past_midnight(self):
        self.assertAlmostEqual(window_hours(at(23), at(7)), 8.0)

    def test_minutes_count(self):
        self.assertAlmostEqual(window_hours(at(10), at(18, 30)), 8.5)

    def test_a_window_that_starts_where_it_ends_is_the_whole_day(self):
        # Not zero: dividing the house by it must never blow up, and "all day"
        # is the only sane reading of 00:00-00:00.
        self.assertAlmostEqual(window_hours(at(0), at(0)), 24.0)


class TheHourWeAreStandingIn(unittest.TestCase):
    """hour_row: the forecast row covering a moment, for the daytime decision.

    A row is stamped with the END of its hour, so the 11:00 row is what the
    decision reads at 10:00 and at 10:59 - and not at 11:00.
    """

    def outlook(self):
        rows = [hour(datetime.combine(DAY, at(h)), kw=float(h)) for h in range(11, 15)]
        return stocked(rows).outlook(DAY, at(10), at(14))

    def row_at(self, hour_, minute=0):
        row = solar_forecast.hour_row(self.outlook(), datetime.combine(DAY, at(hour_, minute)))
        return None if row is None else row["kw"]

    def test_the_start_of_an_hour_reads_that_hour(self):
        self.assertAlmostEqual(self.row_at(10), 11.0)

    def test_the_last_minute_still_reads_the_same_hour(self):
        self.assertAlmostEqual(self.row_at(10, 59), 11.0)

    def test_the_next_hour_moves_on(self):
        self.assertAlmostEqual(self.row_at(11), 12.0)

    def test_a_moment_before_the_window_has_no_row(self):
        self.assertIsNone(self.row_at(9, 59))

    def test_a_moment_after_the_window_has_no_row(self):
        # Which is how the loop knows there is no surplus to speak of outside
        # the solar window, rather than guessing at one.
        self.assertIsNone(self.row_at(14))

    def test_another_day_has_no_row(self):
        row = solar_forecast.hour_row(
            self.outlook(), datetime.combine(DAY + timedelta(days=1), at(12)))
        self.assertIsNone(row)


class Mean(unittest.TestCase):

    def test_it_skips_the_gaps(self):
        self.assertAlmostEqual(solar_forecast._mean([10, None, 20]), 15.0)

    def test_nothing_but_gaps_is_nothing(self):
        self.assertIsNone(solar_forecast._mean([None, None]))

    def test_an_empty_run_is_nothing(self):
        self.assertIsNone(solar_forecast._mean([]))


if __name__ == "__main__":
    unittest.main()
