"""The call itself: what main.py decides, and what it does with the answer.

The constants are the ones tests/__init__.py put in the environment: 6.0
kWh/day owed to the load, which draws 2.0 kW, and an 8.0 kWh house spread
across an eight-hour solar window - so the house takes exactly 1.0 kW off
every forecast hour before the load sees any of it, and the surplus in every
test below is simply "that hour's kW, minus one".
"""

import contextlib
import io
import time
import types
import unittest
from unittest import mock

import main


def constants(**values):
    """Run a block with main's module-level constants temporarily changed."""
    return mock.patch.multiple(main, **values)


@contextlib.contextmanager
def quiet():
    """Swallow the watcher's log lines, and hand back what it said."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        yield buffer


def outlook(*kws):
    """An outlook built from one forecast kW figure per hour of the window.

    The house takes 1.0 kW of each, so outlook(3.0) is one hour with 2.0 kW
    spare - exactly on the ON threshold.
    """
    return {"pv_kwh": sum(kws), "hours": [{"kw": kw} for kw in kws]}


class SpareRoof(unittest.TestCase):
    """spare_kw: the roof the rest of the house is not already taking."""

    def test_the_house_is_served_out_of_the_hour_first(self):
        self.assertAlmostEqual(main.spare_kw(3.0), 2.0)

    def test_an_hour_the_house_outgrows_goes_negative(self):
        self.assertAlmostEqual(main.spare_kw(0.4), -0.6)


class FreeSolar(unittest.TestCase):
    """free_solar_kwh: what today can give away before the grid is touched."""

    def test_it_counts_only_the_hours_that_clear_the_on_threshold(self):
        # 4.0 kW leaves 3.0 spare, which is over the 2.0 threshold, but the
        # load can only swallow its own 2.0 kW in the hour.
        self.assertAlmostEqual(main.free_solar_kwh(outlook(4.0, 4.0, 4.0)), 6.0)

    def test_the_on_threshold_is_inclusive(self):
        self.assertAlmostEqual(main.free_solar_kwh(outlook(3.0, 3.0)), 4.0)

    def test_an_hour_just_under_the_threshold_gives_nothing(self):
        # 1.9 kW spare would not switch the socket on, so it must not be
        # counted here either, or the night under-buys on a promise the
        # afternoon never keeps.
        self.assertAlmostEqual(main.free_solar_kwh(outlook(2.9, 2.9)), 0.0)

    def test_a_thin_day_hands_the_load_nothing_however_long_it_lasts(self):
        # 8 hours x 1.5 kW is 12 kWh on the roof and not one of them free.
        self.assertAlmostEqual(main.free_solar_kwh(outlook(*([1.5] * 8))), 0.0)

    def test_a_dark_hour_is_never_a_negative_contribution(self):
        self.assertAlmostEqual(main.free_solar_kwh(outlook(0.0, 4.0)), 2.0)


class NightTarget(unittest.TestCase):
    """night_target_kwh: only the shortfall, and never more than a day's worth."""

    def test_a_day_that_covers_the_load_buys_nothing(self):
        self.assertAlmostEqual(main.night_target_kwh(outlook(4.0, 4.0, 4.0)), 0.0)

    def test_only_the_part_the_sun_will_not_manage(self):
        # 4.0 kWh free of the 6.0 owed -> buy 2.0.
        self.assertAlmostEqual(main.night_target_kwh(outlook(3.0, 3.0)), 2.0)

    def test_never_more_than_the_daily_budget(self):
        self.assertAlmostEqual(main.night_target_kwh(outlook(0.0, 0.0)), 6.0)

    def test_a_day_with_more_than_enough_still_buys_nothing(self):
        self.assertAlmostEqual(main.night_target_kwh(outlook(*([5.0] * 8))), 0.0)


class NightDecision(unittest.TestCase):
    """decide_night under the forecast strategy."""

    # Two hours at 3.0 kW: 4.0 kWh free, so 2.0 kWh to buy.
    SHORTFALL = (3.0, 3.0)
    PLENTY = (4.0, 4.0, 4.0)

    def decide(self, kws=SHORTFALL, wet=0.0, delivered=0.0):
        return main.decide_night(None if kws is None else outlook(*kws), wet, delivered)

    def test_a_spent_budget_ends_the_night(self):
        on, why = self.decide(delivered=6.0)
        self.assertFalse(on)
        self.assertIn("already had", why)

    def test_a_spent_budget_outranks_a_missing_forecast(self):
        # With the budget full there is no arithmetic left to do, whatever
        # the sky says.
        on, why = self.decide(None, delivered=6.0)
        self.assertFalse(on)
        self.assertIn("already had", why)

    def test_no_forecast_holds_off(self):
        on, why = self.decide(None)
        self.assertFalse(on)
        self.assertIn("no forecast", why)

    def test_a_day_that_covers_it_waits_for_the_sun(self):
        on, why = self.decide(self.PLENTY)
        self.assertFalse(on)
        self.assertIn("waiting for sun", why)

    def test_a_shortfall_buys_only_the_shortfall(self):
        on, why = self.decide()
        self.assertTrue(on)
        self.assertIn("buying 2.0 kWh", why)

    def test_the_grid_share_stops_at_the_target_not_the_budget(self):
        # 2.0 kWh bought of a 6.0 kWh day: the rest is the sun's job, so the
        # socket goes off even though the budget is not full.
        on, why = self.decide(delivered=2.0)
        self.assertFalse(on)
        self.assertIn("grid share of 2.0 kWh delivered", why)

    def test_part_way_through_the_grid_share_keeps_going(self):
        on, _ = self.decide(delivered=1.9)
        self.assertTrue(on)

    def test_a_missing_meter_reading_counts_as_nothing_delivered(self):
        # A P100 has no meter. Rather than refuse to run, treat it as empty:
        # the window is the only limit then.
        on, _ = self.decide(delivered=None)
        self.assertTrue(on)

    def test_the_reason_carries_the_whole_sum(self):
        _, why = self.decide()
        self.assertIn("6.0 kWh sun", why)         # two hours at 3.0 kW
        self.assertIn("house takes 8.0", why)
        self.assertIn("4.0 kWh reaches the load free", why)


class NightDecisionRainStrategy(unittest.TestCase):
    """decide_night under the simpler 'is it going to rain' strategy."""

    def decide(self, wet, delivered=0.0):
        with constants(BOOST_STRATEGY="rain"):
            return main.decide_night(outlook(1.0), wet, delivered)

    def test_a_wet_day_buys_grid(self):
        on, why = self.decide(wet=0.8)
        self.assertTrue(on)
        self.assertIn("buying grid", why)

    def test_a_dry_day_waits_for_the_sun(self):
        on, why = self.decide(wet=0.2)
        self.assertFalse(on)
        self.assertIn("dry enough", why)

    def test_half_the_window_wet_is_wet_enough(self):
        # RAIN_HOURS_FRACTION is 0.5 and the comparison is inclusive.
        on, _ = self.decide(wet=0.5)
        self.assertTrue(on)

    def test_it_never_looks_at_how_much_energy_the_day_makes(self):
        # The whole point of this strategy, and the reason it is not default.
        on, _ = self.decide(wet=1.0)
        self.assertTrue(on)

    def test_the_budget_still_ends_the_night(self):
        on, why = self.decide(wet=1.0, delivered=6.0)
        self.assertFalse(on)
        self.assertIn("already had", why)


class SolarDecision(unittest.TestCase):
    """decide_solar: run on spare roof, with hysteresis so a grazing hour
    does not chatter."""

    def test_a_spent_budget_ends_the_day(self):
        on, why = main.decide_solar(4.0, 6.0, True)
        self.assertFalse(on)
        self.assertIn("already had", why)

    def test_a_spent_budget_outranks_a_missing_forecast(self):
        on, why = main.decide_solar(None, 6.0, True)
        self.assertFalse(on)
        self.assertIn("already had", why)

    def test_no_forecast_for_this_hour_holds_off(self):
        on, why = main.decide_solar(None, 0.0, True)
        self.assertFalse(on)
        self.assertIn("no forecast", why)

    def test_a_roof_ahead_of_the_house_runs_the_load(self):
        on, why = main.decide_solar(3.0, 0.0, False)
        self.assertTrue(on)
        self.assertIn("heating on free solar", why)

    def test_the_on_threshold_is_inclusive(self):
        on, _ = main.decide_solar(2.0, 0.0, False)
        self.assertTrue(on)

    def test_a_roof_the_house_needs_stops_the_load(self):
        on, why = main.decide_solar(0.5, 0.0, True)
        self.assertFalse(on)
        self.assertIn("leaving the roof to the house", why)

    def test_the_off_threshold_is_inclusive(self):
        on, _ = main.decide_solar(1.0, 0.0, True)
        self.assertFalse(on)

    def test_a_negative_surplus_stops_the_load(self):
        on, _ = main.decide_solar(-0.6, 0.0, True)
        self.assertFalse(on)

    def test_inside_the_band_a_running_socket_keeps_running(self):
        on, why = main.decide_solar(1.5, 0.0, True)
        self.assertTrue(on)
        self.assertIn("holding", why)

    def test_inside_the_band_a_stopped_socket_stays_stopped(self):
        on, _ = main.decide_solar(1.5, 0.0, False)
        self.assertFalse(on)

    def test_inside_the_band_an_unknown_relay_stays_off(self):
        # plug.state is None after a failed switch: the safe reading of
        # "leave it as it is" is then "leave it off".
        on, _ = main.decide_solar(1.5, 0.0, None)
        self.assertFalse(on)


class NoDailyBudget(unittest.TestCase):
    """DEVICE_DAILY_KWH of 0 means 'no budget', not 'a budget of nothing'."""

    def test_the_solar_window_ignores_the_meter(self):
        with constants(DEVICE_DAILY_KWH=0.0):
            on, _ = main.decide_solar(3.0, 99.0, False)
        self.assertTrue(on)

    def test_the_night_window_buys_nothing(self):
        # Nothing is owed, so the shortfall is nothing and the grid is not
        # touched - the daytime window is then the only one that acts.
        with constants(DEVICE_DAILY_KWH=0.0):
            on, _ = main.decide_night(outlook(0.0), 1.0, 99.0)
        self.assertFalse(on)


METER = {"today_energy": 1234}         # 1.234 kWh through the socket today


class FakeDevice:
    """A P110 that can be told to fail its next few commands.

    energy=None is a plug with no meter at all, like a P100.
    """

    def __init__(self, failures=0, energy=METER, power=2410):
        self.failures = failures
        self.energy = energy
        self.power = power
        self.calls = []

    async def on(self):
        self._switch(True)

    async def off(self):
        self._switch(False)

    def _switch(self, state):
        self.calls.append(state)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("DeviceError: HostUnreachable")

    async def get_energy_usage(self):
        if self.energy is None:
            raise RuntimeError("this model has no meter")
        return types.SimpleNamespace(to_dict=lambda: dict(self.energy))

    async def get_current_power(self):
        if self.energy is None:
            raise RuntimeError("this model has no meter")
        return types.SimpleNamespace(to_dict=lambda: {"current_power": self.power})


class FakeApi:
    def __init__(self, device):
        self.device = device
        self.connects = 0

    async def p110(self, ip):
        self.connects += 1
        return self.device


async def no_sleep(_seconds):
    """The retry pause, without the wait."""


class PlugSwitching(unittest.IsolatedAsyncioTestCase):

    def plug(self, **kwargs):
        device = FakeDevice(**kwargs)
        api = FakeApi(device)
        return main.Plug(api, "10.0.0.5"), api, device

    async def test_the_first_switch_connects_and_commands(self):
        plug, api, device = self.plug()
        with quiet():
            self.assertTrue(await plug.set_state(True))
        self.assertEqual(api.connects, 1)
        self.assertEqual(device.calls, [True])
        self.assertIs(plug.state, True)

    async def test_a_repeat_is_not_sent_again(self):
        plug, _, device = self.plug()
        with quiet():
            await plug.set_state(True)
            await plug.set_state(True)
        self.assertEqual(device.calls, [True])

    async def test_the_opposite_state_is_sent(self):
        plug, api, device = self.plug()
        with quiet():
            await plug.set_state(True)
            await plug.set_state(False)
        self.assertEqual(device.calls, [True, False])
        self.assertEqual(api.connects, 1)        # the session is reused

    async def test_a_failure_is_retried_once(self):
        plug, api, device = self.plug(failures=1)
        with mock.patch.object(main.asyncio, "sleep", no_sleep), quiet():
            self.assertTrue(await plug.set_state(True))
        self.assertEqual(device.calls, [True, True])
        self.assertEqual(api.connects, 2)        # a fresh handshake in between
        self.assertIs(plug.state, True)

    async def test_two_failures_give_up_and_forget_the_state(self):
        plug, _, device = self.plug(failures=2)
        with mock.patch.object(main.asyncio, "sleep", no_sleep), quiet() as log:
            self.assertFalse(await plug.set_state(True))
        self.assertEqual(len(device.calls), 2)
        # After a failure the relay's real position is unknown, so it must not
        # be remembered as anything - the next pass has to send the command.
        self.assertIsNone(plug.state)
        self.assertIn("no IP route to the plug", log.getvalue())

    async def test_a_failed_switch_forgets_where_the_relay_was(self):
        # The dangerous case: the socket is on, turning it off fails, and the
        # next pass wants it on again. If the failure had left "on" remembered
        # the command would be skipped as already-done - and the relay, whose
        # real position nobody knows, would be left wherever it fell.
        plug, _, device = self.plug()
        with mock.patch.object(main.asyncio, "sleep", no_sleep), quiet():
            await plug.set_state(True)
            device.failures = 2
            self.assertFalse(await plug.set_state(False))
            self.assertIsNone(plug.state)
            device.failures = 0
            self.assertTrue(await plug.set_state(True))
        self.assertEqual(device.calls, [True, False, False, True])

    async def test_a_403_says_what_to_do_about_it(self):
        plug, _, device = self.plug(failures=2)
        device._switch = mock.Mock(side_effect=RuntimeError("403 Forbidden"))
        with mock.patch.object(main.asyncio, "sleep", no_sleep), quiet() as log:
            await plug.set_state(True)
        self.assertIn("Third-Party", log.getvalue())

    async def test_the_meter_is_read_in_kwh(self):
        plug, _, _ = self.plug()
        with quiet():
            self.assertAlmostEqual(await plug.energy_today_kwh(), 1.234)

    async def test_a_plug_without_a_meter_reports_nothing(self):
        plug, _, _ = self.plug(energy=None)
        with quiet():
            self.assertIsNone(await plug.energy_today_kwh())

    async def test_a_meter_without_a_daily_total_reports_nothing(self):
        plug, _, _ = self.plug(energy={})
        with quiet():
            self.assertIsNone(await plug.energy_today_kwh())

    async def test_the_live_draw_is_read_in_watts(self):
        plug, _, _ = self.plug()
        with quiet():
            self.assertEqual(await plug.power_w(), 2410)

    async def test_a_plug_without_a_meter_reports_no_draw(self):
        # Nothing decides on this number, so an unreadable one must not stop
        # the pass - it just leaves a gap in the record.
        plug, _, _ = self.plug(energy=None)
        with quiet():
            self.assertIsNone(await plug.power_w())


class FakeLogbook:
    def __init__(self):
        self.samples = []
        self.prunes = 0

    def sample(self, **fields):
        self.samples.append(fields)

    def prune(self):
        self.prunes += 1


class SampleRecording(unittest.TestCase):
    """Recorder: every change at once, a quiet hour only now and then."""

    def test_an_unchanged_state_is_written_once(self):
        book = FakeLogbook()
        recorder = main.Recorder(book, 3600)
        for _ in range(5):
            recorder.record(socket_on=False, wanted=False, reason="outside both windows")
        self.assertEqual(len(book.samples), 1)

    def test_a_changed_decision_is_written_at_once(self):
        book = FakeLogbook()
        recorder = main.Recorder(book, 3600)
        recorder.record(socket_on=False, wanted=False, reason="outside both windows")
        recorder.record(socket_on=True, wanted=True, reason="buying 4.0 kWh")
        self.assertEqual(len(book.samples), 2)

    def test_a_changed_reason_alone_is_written_at_once(self):
        # The numbers in the reason are the interesting part of a quiet hour.
        book = FakeLogbook()
        recorder = main.Recorder(book, 3600)
        recorder.record(socket_on=False, wanted=False, reason="battery 66% - holding")
        recorder.record(socket_on=False, wanted=False, reason="battery 64% - no spare solar")
        self.assertEqual(len(book.samples), 2)

    def test_the_quiet_state_is_re_recorded_when_the_interval_passes(self):
        book = FakeLogbook()
        recorder = main.Recorder(book, 0)
        recorder.record(socket_on=False, wanted=False, reason="outside both windows")
        recorder.record(socket_on=False, wanted=False, reason="outside both windows")
        self.assertEqual(len(book.samples), 2)

    def test_every_field_reaches_the_logbook(self):
        book = FakeLogbook()
        main.Recorder(book, 3600).record(spare_kw=2.4, free_kwh=2.0, reason="x")
        self.assertEqual(book.samples[0]["spare_kw"], 2.4)
        self.assertEqual(book.samples[0]["free_kwh"], 2.0)

    def test_the_record_is_pruned_once_a_day(self):
        book = FakeLogbook()
        clock = [1000.0]
        with mock.patch.object(time, "monotonic", lambda: clock[0]):
            recorder = main.Recorder(book, 0)
            recorder.record(reason="a")
            self.assertEqual(book.prunes, 0)
            clock[0] += 86399
            recorder.record(reason="b")
            self.assertEqual(book.prunes, 0)
            clock[0] += 2
            recorder.record(reason="c")
            self.assertEqual(book.prunes, 1)


class SettingsForThePage(unittest.TestCase):
    """settings() draws the thresholds on the page - and nothing else."""

    def test_it_carries_no_credentials(self):
        from tests import SECRET
        blob = repr(main.settings())
        self.assertNotIn(SECRET, blob)
        self.assertNotIn("@", blob)
        for key in main.settings():
            self.assertNotRegex(key, r"password|secret|token|email|credential")

    def test_it_survives_json(self):
        import json
        self.assertIn("night_start", json.loads(json.dumps(main.settings())))

    def test_the_windows_are_drawn_as_hh_mm(self):
        self.assertEqual(main.settings()["night_start"], "00:00")
        self.assertEqual(main.settings()["solar_end"], "18:00")


if __name__ == "__main__":
    unittest.main()
