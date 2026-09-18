"""The call itself: what main.py decides, and what it does with the answer.

The constants are the ones tests/__init__.py put in the environment: 6.0
kWh/day owed to the load and an 8.0 kWh house. Only the day's forecast TOTAL
reaches a decision, so every expected number below is one subtraction: what
the day makes, less the 8.0 the house takes, is what the load gets free, and
the night buys whatever is left of the 6.0.
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

    Only the day's total is ever read; the hours are carried because a real
    outlook has them and the page draws them.
    """
    return {"pv_kwh": sum(kws), "hours": [{"kw": kw} for kw in kws]}


class FreeSolar(unittest.TestCase):
    """free_solar_kwh: what today gives the load before the grid is touched."""

    def test_the_house_is_taken_out_of_the_day_first(self):
        # 12 kWh on the roof, 8 to the house, 4 left for the load.
        self.assertAlmostEqual(main.free_solar_kwh(outlook(4.0, 4.0, 4.0)), 4.0)

    def test_a_day_the_house_swallows_whole_gives_the_load_nothing(self):
        self.assertAlmostEqual(main.free_solar_kwh(outlook(3.0, 3.0)), 0.0)

    def test_it_never_goes_negative(self):
        # A day that does not even cover the house is still zero to the load,
        # never a debt carried into the night's arithmetic.
        self.assertAlmostEqual(main.free_solar_kwh(outlook(0.5)), 0.0)

    def test_it_never_promises_more_than_the_load_can_take(self):
        # 40 kWh on the roof still only ever fills a 6 kWh budget.
        self.assertAlmostEqual(main.free_solar_kwh(outlook(*([5.0] * 8))), 6.0)

    def test_how_the_day_is_shaped_makes_no_difference(self):
        # The same 12 kWh, dribbled out or in one arc. The hourly version
        # answered these two differently; this one cannot.
        flat = main.free_solar_kwh(outlook(*([1.5] * 8)))
        peaked = main.free_solar_kwh(outlook(0.2, 0.5, 1.2, 3.3, 4.0, 1.8, 0.7, 0.3))
        self.assertAlmostEqual(flat, 4.0)
        self.assertAlmostEqual(peaked, 4.0)


class NightTarget(unittest.TestCase):
    """night_target_kwh: only the shortfall, and never more than a day's worth."""

    def test_a_day_that_covers_the_load_buys_nothing(self):
        # 40 kWh roof, 8 to the house, the load's 6 covered outright.
        self.assertAlmostEqual(main.night_target_kwh(outlook(*([5.0] * 8))), 0.0)

    def test_only_the_part_the_sun_will_not_manage(self):
        # 12 kWh roof - 8 house = 4.0 kWh free of the 6.0 owed -> buy 2.0.
        self.assertAlmostEqual(main.night_target_kwh(outlook(4.0, 4.0, 4.0)), 2.0)

    def test_never_more_than_the_daily_budget(self):
        self.assertAlmostEqual(main.night_target_kwh(outlook(0.0, 0.0)), 6.0)

    def test_a_day_that_only_feeds_the_house_buys_the_lot(self):
        self.assertAlmostEqual(main.night_target_kwh(outlook(3.0, 3.0)), 6.0)


class NightDecision(unittest.TestCase):
    """decide_night under the forecast strategy."""

    # 12 kWh on the roof less the 8 kWh house: 4.0 kWh free, 2.0 kWh to buy.
    SHORTFALL = (4.0, 4.0, 4.0)
    PLENTY = (5.0,) * 8

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
        self.assertIn("12.0 kWh sun", why)        # three hours at 4.0 kW
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
    """decide_solar: top up until the meter says the day is done.

    No forecast and no threshold reach this window. It runs from SOLAR_START
    and the meter is the only thing that stops it.
    """

    def test_an_empty_meter_runs_the_load(self):
        on, why = main.decide_solar(0.0)
        self.assertTrue(on)
        self.assertIn("topping up", why)

    def test_a_spent_budget_ends_the_day(self):
        on, why = main.decide_solar(6.0)
        self.assertFalse(on)
        self.assertIn("already had", why)

    def test_the_budget_is_inclusive(self):
        on, _ = main.decide_solar(6.0)
        self.assertFalse(on)

    def test_part_way_through_it_keeps_going(self):
        on, why = main.decide_solar(5.9)
        self.assertTrue(on)
        self.assertIn("5.9 of 6.0 kWh", why)

    def test_what_the_night_already_bought_counts_against_the_day(self):
        # One meter, two windows: 4.0 kWh bought at night leaves 2.0 to top
        # up, and the socket stops the moment the meter says 6.0.
        self.assertTrue(main.decide_solar(4.0)[0])
        self.assertFalse(main.decide_solar(6.0)[0])

    def test_an_unreadable_meter_holds_off(self):
        # The meter is the only brake on this window now. Without it the
        # socket would heat until sunset, so unknown has to mean off.
        on, why = main.decide_solar(None)
        self.assertFalse(on)
        self.assertIn("no meter reading", why)


class NoDailyBudget(unittest.TestCase):
    """DEVICE_DAILY_KWH of 0 means 'no budget', not 'a budget of nothing'."""

    def test_the_solar_window_ignores_the_meter(self):
        with constants(DEVICE_DAILY_KWH=0.0):
            on, _ = main.decide_solar(99.0)
        self.assertTrue(on)

    def test_the_solar_window_runs_without_a_meter_at_all(self):
        # A P100 has no meter. With no budget to enforce there is nothing to
        # read, so the window itself is the only limit.
        with constants(DEVICE_DAILY_KWH=0.0):
            on, _ = main.decide_solar(None)
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

    def __init__(self, failures=0, energy=METER, power=2410, meter_failures=0,
                 relay=False, info_failures=0):
        self.failures = failures
        self.energy = energy
        self.power = power
        self.meter_failures = meter_failures     # e.g. an expired session
        self.relay = relay          # what the socket is doing, however it got there
        self.info_failures = info_failures
        self.calls = []
        self.reads = 0

    async def on(self):
        self._switch(True)

    async def off(self):
        self._switch(False)

    def _switch(self, state):
        self.calls.append(state)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("DeviceError: HostUnreachable")
        self.relay = state

    async def get_device_info(self):
        if self.info_failures:
            self.info_failures -= 1
            raise RuntimeError("SessionTimeout")
        return types.SimpleNamespace(to_dict=lambda: {"device_on": self.relay})

    async def get_energy_usage(self):
        self.reads += 1
        if self.meter_failures:
            self.meter_failures -= 1
            raise RuntimeError("SessionTimeout")
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
        with mock.patch.object(main.asyncio, "sleep", no_sleep), quiet():
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
        with mock.patch.object(main.asyncio, "sleep", no_sleep), quiet():
            self.assertIsNone(await plug.power_w())


class ComingBackFromAPause(unittest.IsolatedAsyncioTestCase):
    """What the watcher believes the relay is doing stops being evidence the
    moment it hands the socket back to a person."""

    def plug(self, **kwargs):
        device = FakeDevice(**kwargs)
        return main.Plug(FakeApi(device), "10.0.0.5"), device

    async def test_the_verdict_is_re_sent_after_a_pause(self):
        plug, device = self.plug()
        with quiet():
            await plug.set_state(True)
            plug.forget()               # what main() does on resuming
            await plug.set_state(True)
        # Twice: while paused the socket may have been switched by hand, so
        # "I already commanded ON" no longer says the relay is ON.
        self.assertEqual(device.calls, [True, True])

    async def test_it_is_not_re_sent_when_nothing_was_paused(self):
        plug, device = self.plug()
        with quiet():
            await plug.set_state(True)
            await plug.set_state(True)
        self.assertEqual(device.calls, [True])

    async def test_the_relay_is_asked_while_nothing_is_being_commanded(self):
        plug, device = self.plug(relay=True)
        # Nobody commanded this - someone switched it in the Tapo app while
        # the watcher was paused - and the record has to say so anyway.
        with quiet():
            self.assertIs(await plug.is_on(), True)
        self.assertEqual(device.calls, [])
        self.assertIsNone(plug.state)

    async def test_a_relay_that_cannot_be_read_is_unknown_not_off(self):
        plug, _ = self.plug(info_failures=2)
        with quiet():
            self.assertIsNone(await plug.is_on())

    async def test_forgetting_leaves_the_state_unknown_rather_than_off(self):
        plug, _ = self.plug()
        with quiet():
            await plug.set_state(True)
        plug.forget()
        self.assertIsNone(plug.state)


class TheMeterRecovers(unittest.IsolatedAsyncioTestCase):
    """A stale session must not take the meter out for the rest of the day.

    The daytime window switches on the meter and nothing else, so a read that
    gave up after one failure - keeping the dead handle - would leave the
    socket off until the watcher was restarted. This is that bug, guarded.
    """

    def plug(self, **kwargs):
        device = FakeDevice(**kwargs)
        api = FakeApi(device)
        return main.Plug(api, "10.0.0.5"), api, device

    async def test_an_expired_session_is_retried_on_a_fresh_one(self):
        plug, api, device = self.plug(meter_failures=1)
        with mock.patch.object(main.asyncio, "sleep", no_sleep), quiet():
            self.assertAlmostEqual(await plug.energy_today_kwh(), 1.234)
        self.assertEqual(device.reads, 2)        # failed, then succeeded
        self.assertEqual(api.connects, 2)        # on a new handshake

    async def test_the_dead_handle_is_dropped_so_the_next_pass_reconnects(self):
        plug, api, device = self.plug(meter_failures=99)
        with mock.patch.object(main.asyncio, "sleep", no_sleep), quiet():
            self.assertIsNone(await plug.energy_today_kwh())
            device.meter_failures = 0
            self.assertAlmostEqual(await plug.energy_today_kwh(), 1.234)
        self.assertGreater(api.connects, 1)

    async def test_the_outage_is_reported_once_not_every_pass(self):
        plug, _, device = self.plug(meter_failures=99)
        with mock.patch.object(main.asyncio, "sleep", no_sleep), quiet() as log:
            for _ in range(3):
                await plug.energy_today_kwh()
        self.assertEqual(log.getvalue().count("cannot read the meter"), 1)

    async def test_coming_back_is_reported_too(self):
        plug, _, device = self.plug(meter_failures=99)
        with mock.patch.object(main.asyncio, "sleep", no_sleep), quiet() as log:
            await plug.energy_today_kwh()
            device.meter_failures = 0
            await plug.energy_today_kwh()
        self.assertIn("meter readable again", log.getvalue())

    async def test_a_long_outage_does_not_flood_the_log(self):
        # _read reconnects every pass while the plug is unwell. If each
        # handshake announced itself, the one warning that matters would be
        # buried under a line a minute.
        plug, _, _ = self.plug(meter_failures=99)
        with mock.patch.object(main.asyncio, "sleep", no_sleep), quiet() as log:
            for _ in range(5):
                await plug.energy_today_kwh()
        self.assertEqual(log.getvalue().count("connected to plug"), 1)

    async def test_a_broken_meter_does_not_silence_the_power_read(self):
        # Separate reads, separate reporting - one being out must not hide
        # the other coming back.
        plug, _, device = self.plug(meter_failures=99)
        with mock.patch.object(main.asyncio, "sleep", no_sleep), quiet():
            self.assertIsNone(await plug.energy_today_kwh())
            self.assertEqual(await plug.power_w(), 2410)


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

    def test_a_pause_alone_is_written_at_once(self):
        # Nothing else about the pass changes when the switch is thrown - the
        # verdict and the relay both stay put - so if this were not in the key
        # the record would show no pause until the reason moved on.
        book = FakeLogbook()
        recorder = main.Recorder(book, 3600)
        recorder.record(socket_on=True, wanted=True, enabled=True, reason="buying 4.0 kWh")
        recorder.record(socket_on=True, wanted=True, enabled=False, reason="buying 4.0 kWh")
        self.assertEqual(len(book.samples), 2)
        self.assertIs(book.samples[1]["enabled"], False)

    def test_a_changed_reason_alone_is_written_at_once(self):
        # The numbers in the reason are the interesting part of a quiet hour.
        book = FakeLogbook()
        recorder = main.Recorder(book, 3600)
        recorder.record(socket_on=False, wanted=False, reason="10.0 kWh sun - waiting")
        recorder.record(socket_on=False, wanted=False, reason="11.0 kWh sun - waiting")
        self.assertEqual(len(book.samples), 2)

    def test_the_quiet_state_is_re_recorded_when_the_interval_passes(self):
        book = FakeLogbook()
        recorder = main.Recorder(book, 0)
        recorder.record(socket_on=False, wanted=False, reason="outside both windows")
        recorder.record(socket_on=False, wanted=False, reason="outside both windows")
        self.assertEqual(len(book.samples), 2)

    def test_every_field_reaches_the_logbook(self):
        book = FakeLogbook()
        main.Recorder(book, 3600).record(target_kwh=2.4, free_kwh=2.0, reason="x")
        self.assertEqual(book.samples[0]["target_kwh"], 2.4)
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
