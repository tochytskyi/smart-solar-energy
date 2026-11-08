"""Blackout mode's grid answer: what the inverter says, and when the sentinel decides.

The cloud is never reached - urlopen is replaced by a dictionary of canned
replies shaped like the real ones (taken from this site's own inverter during
an outage), and the sentinel by a stub. Nothing here touches the network.
"""

import asyncio
import json
import time
import unittest
import urllib.error
from unittest import mock

import deye_cloud
import main
from tests import ENV


def latest(volts=230.0, hz=50.0, age=30.0, serial="2604255378", phases=None):
    """A /device/latest reply with one inverter in it."""
    phases = phases or (volts, volts, volts)
    data = [{"key": "GridVoltageL%d" % n, "value": "%.2f" % v, "unit": "V"}
            for n, v in zip((1, 2, 3), phases)]
    data += [{"key": "GridFrequency", "value": "%.2f" % hz, "unit": "Hz"},
             {"key": "GridStatus", "value": "Static", "unit": None},
             {"key": "SOC", "value": "87", "unit": "%"},
             {"key": "BatteryPower", "value": "-1200", "unit": "W"}]
    return {"code": "1000000", "msg": "success", "success": True,
            "deviceDataList": [{"deviceSn": serial, "deviceType": "INVERTER", "deviceState": 1,
                                "collectionTime": int(time.time() - age), "dataList": data}]}


class Reply:
    def __init__(self, body):
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeApi:
    """urlopen, answering by path, and keeping every request it was sent."""

    def __init__(self, data=None):
        self.sent = []
        self.routes = {
            "/v1.0/account/token": {"success": True, "accessToken": "tok-1",
                                    "expiresIn": "5183999"},
            "/v1.0/station/list": {"success": True, "stationList": [{"id": 62560630}]},
            "/v1.0/station/device": {"success": True, "deviceListItems": [
                {"deviceType": "COLLECTOR", "deviceSn": "D26320893340"},
                {"deviceType": "BATTERY", "deviceSn": "2604255378BM01"},
                {"deviceType": "INVERTER", "deviceSn": "2604255378"}]},
            "/v1.0/device/latest": data or latest(),
        }

    def __call__(self, request, timeout=None):
        path = request.full_url.split("deyecloud.com", 1)[1].split("?", 1)[0]
        self.sent.append((path, request))
        answer = self.routes[path]
        if isinstance(answer, Exception):
            raise answer
        return Reply(answer)

    def paths(self):
        return [path for path, _ in self.sent]


def cloud():
    return deye_cloud.Cloud("https://eu1-developer.deyecloud.com", "app-id-1",
                            ENV["DEYE_APP_SECRET"], ENV["DEYE_EMAIL"], ENV["DEYE_PASSWORD"])


class Parse(unittest.TestCase):
    """parse: the inverter's own accept or refuse, from frames this site recorded."""

    def accepted(self, **kwargs):
        return deye_cloud.parse(latest(**kwargs), "2604255378")["accepted"]

    def test_a_live_mains_is_accepted(self):
        self.assertTrue(self.accepted(volts=229.9, hz=50.0))

    def test_a_weak_grid_the_inverter_still_buys_from_is_accepted(self):
        # 11-03 01:01 - 3.9 kW bought with the weakest phase at 188.6 V.
        self.assertTrue(self.accepted(phases=(196.0, 198.0, 188.6), hz=49.91))

    def test_0_hz_with_voltage_left_is_the_inverter_refusing(self):
        # 11-04 18:34:52 - 176-185 V on the phases, 0 Hz, and the inverter
        # raised Grid_Phase_Wrong at the same second.
        self.assertFalse(self.accepted(phases=(182.0, 185.0, 175.9), hz=0.0))

    def test_the_outage_is_refused(self):
        reading = deye_cloud.parse(latest(0.0, 0.0), "2604255378")
        self.assertFalse(reading["accepted"])
        self.assertEqual(reading["status"], "Static")

    def test_a_missing_phase_is_refused(self):
        # 11-04 07:23:52 - two phases back, L2 still 0 V.
        self.assertFalse(self.accepted(phases=(241.1, 0.0, 235.3), hz=49.95))

    def test_the_weakest_phase_is_the_one_reported(self):
        reading = deye_cloud.parse(latest(phases=(196.0, 198.0, 188.6)), "2604255378")
        self.assertAlmostEqual(reading["volts"], 188.6)

    def test_the_battery_is_left_where_it_lies(self):
        reading = deye_cloud.parse(latest(), "2604255378")
        self.assertEqual(set(reading), {"accepted", "volts", "hz", "status", "collected"})

    def test_a_reply_without_the_grid_is_no_answer(self):
        reply = latest()
        reply["deviceDataList"][0]["dataList"] = [{"key": "SOC", "value": "87"}]
        with self.assertRaises(deye_cloud.DeyeError):
            deye_cloud.parse(reply, "2604255378")

    def test_another_inverter_is_no_answer(self):
        with self.assertRaises(deye_cloud.DeyeError):
            deye_cloud.parse(latest(serial="someone-else"), "2604255378")


class TheClient(unittest.TestCase):
    """Cloud: one login, one lookup, one reading a minute, and no secret leaks."""

    def run_with(self, api, client=None):
        client = client or cloud()
        with mock.patch("urllib.request.urlopen", api):
            return client, client.grid()

    def test_it_finds_the_inverter_and_reads_it(self):
        api = FakeApi()
        client, reading = self.run_with(api)
        self.assertTrue(reading["accepted"])
        self.assertEqual(client.serial, "2604255378")
        self.assertEqual(api.paths(), ["/v1.0/account/token", "/v1.0/station/list",
                                       "/v1.0/station/device", "/v1.0/device/latest"])

    def test_the_password_is_sent_hashed(self):
        api = FakeApi()
        self.run_with(api)
        login = api.sent[0][1].data.decode()
        self.assertNotIn(ENV["DEYE_PASSWORD"], login)
        self.assertEqual(len(json.loads(login)["password"]), 64)

    def test_a_second_pass_within_the_minute_spends_no_quota(self):
        api = FakeApi()
        client, _ = self.run_with(api)
        self.run_with(api, client)
        self.assertEqual(api.paths().count("/v1.0/device/latest"), 1)

    def test_after_the_minute_only_the_reading_is_asked_again(self):
        api = FakeApi()
        client, _ = self.run_with(api)
        client._cached_at -= deye_cloud.CACHE_SECONDS
        self.run_with(api, client)
        self.assertEqual(api.paths()[4:], ["/v1.0/device/latest"])

    def test_a_refusal_sends_the_next_call_back_through_the_login(self):
        api = FakeApi()
        client, _ = self.run_with(api)
        client._cached = None
        api.routes["/v1.0/device/latest"] = {"success": False, "code": "2101019",
                                             "msg": "token expired"}
        with self.assertRaises(deye_cloud.DeyeError):
            self.run_with(api, client)
        api.routes["/v1.0/device/latest"] = latest()
        self.run_with(api, client)
        self.assertEqual(api.paths()[-2:], ["/v1.0/account/token", "/v1.0/device/latest"])

    def test_no_error_carries_a_credential(self):
        api = FakeApi()
        api.routes["/v1.0/account/token"] = urllib.error.URLError("timed out")
        with self.assertRaises(deye_cloud.DeyeError) as caught:
            self.run_with(api)
        for secret in (ENV["DEYE_APP_SECRET"], ENV["DEYE_PASSWORD"], "app-id-1"):
            self.assertNotIn(secret, str(caught.exception))


class FakeCloud:
    """Hands back whatever reading the test last put in it."""

    def __init__(self, reading=None, error=None):
        self.reading, self.error = reading, error

    def grid(self):
        if self.error:
            raise deye_cloud.DeyeError(self.error)
        return self.reading


def reading(volts=230.0, hz=50.0, age=30.0, at=None):
    collected = at if at is not None else time.time() - age
    return {"accepted": hz > 0 and volts >= deye_cloud.PHASE_PRESENT_VOLTS,
            "volts": volts, "hz": hz, "status": "Static", "collected": collected}


class Watch(unittest.TestCase):
    """GridWatch: the inverter's verdict, held for the settle time; the sentinel when it cannot say."""

    def read(self, cloud, sentinel=True, watch=None, ip="10.0.0.9", ask=True, knock=True):
        watch = watch or main.GridWatch(cloud)
        watch._cloud = cloud            # a reused watch, asked of a changed cloud
        lines = []
        knocker = mock.AsyncMock(return_value=sentinel)
        with mock.patch.multiple(main, BLACKOUT_SENTINEL_IP=ip, DEYE_MAX_AGE=300.0,
                                 DEYE_GRID_SETTLE=600.0,
                                 sentinel_answers=knocker,
                                 log=lambda message, *a, **k: lines.append((message, a, k))):
            fields, said = asyncio.run(watch.read(ask, knock))
        self.knocked = knocker.await_count
        return fields, said, lines, watch

    def held(self, minutes, volts=230.0):
        """A watch that has seen the inverter hold the grid for `minutes`, and its last answer."""
        now = time.time() - 30
        watch = None
        for back in (minutes, 0):
            fields, said, lines, watch = self.read(
                FakeCloud(reading(volts, at=now - back * 60)), watch=watch)
        return fields, said, lines, watch

    def test_a_grid_held_for_the_settle_time_is_up(self):
        fields, said, _, _ = self.held(10)
        self.assertIs(fields["grid_up"], True)
        self.assertEqual(fields["grid_source"], "inverter")
        self.assertEqual(fields["grid_stable_s"], 600)
        self.assertIn("the inverter has the grid", said)

    def test_a_grid_just_taken_is_not_up_yet(self):
        fields, said, _, _ = self.held(4)
        self.assertIs(fields["grid_up"], False)
        self.assertIn("held the grid only 4 of 10 min", said)

    def test_a_first_reading_after_a_start_waits_too(self):
        fields, _, _, _ = self.read(FakeCloud(reading()))
        self.assertIs(fields["grid_up"], False)
        self.assertEqual(fields["grid_stable_s"], 0)

    def test_one_refusal_starts_the_wait_again(self):
        # The flapping evening: held for half an hour, refused for one minute,
        # taken back - and the night has to wait the whole settle time again.
        _, _, _, watch = self.held(30)
        now = time.time()
        self.read(FakeCloud(reading(176.0, 0.0, at=now - 120)), watch=watch)
        fields, said, _, _ = self.read(FakeCloud(reading(190.0, at=now - 60)), watch=watch)
        self.assertIs(fields["grid_up"], False)
        self.assertEqual(fields["grid_stable_s"], 0)

    def test_the_inverter_refusing_a_weak_grid_is_down(self):
        fields, said, lines, _ = self.read(FakeCloud(reading(175.9, 0.0)), sentinel=True)
        self.assertIs(fields["grid_up"], False)
        self.assertIsNone(fields["grid_stable_s"])
        self.assertIn("rejected the grid (0 Hz, weakest phase 176 V)", said)
        # The sentinel is still powered at 176 V: that is agreement, not a fault.
        self.assertFalse([m for m, _, _ in lines if "believing the inverter" in m])

    def test_a_missing_phase_is_down(self):
        fields, said, _, _ = self.read(FakeCloud(reading(0.0, 49.95)))
        self.assertIs(fields["grid_up"], False)
        self.assertIn("missing a phase", said)

    def test_no_mains_at_all_is_down(self):
        fields, said, _, _ = self.read(FakeCloud(reading(0.0, 0.0)), sentinel=False)
        self.assertIs(fields["grid_up"], False)
        self.assertIn("the inverter reads no grid", said)
        self.assertIn("so the grid is down", said)

    def test_a_stale_reading_hands_over_to_the_sentinel(self):
        # The logger's last upload says the grid was held, but it is ten
        # minutes old: the internet went with the grid. Believing it would
        # drain the battery.
        fields, said, lines, _ = self.read(FakeCloud(reading(age=600)), sentinel=False)
        self.assertIs(fields["grid_up"], False)
        self.assertEqual(fields["grid_source"], "sentinel")
        self.assertEqual(fields["grid_age_s"], 600)       # still recorded, to show why
        self.assertIn("is not answering", said)
        self.assertTrue(any("last reported 10 min ago" in m for m, _, _ in lines))

    def test_a_failing_cloud_hands_over_to_the_sentinel(self):
        fields, _, lines, _ = self.read(FakeCloud(error="unreachable: timed out"), sentinel=True)
        self.assertIs(fields["grid_up"], True)
        self.assertEqual(fields["grid_source"], "sentinel")
        self.assertIsNone(fields["grid_volts"])
        self.assertTrue(any("cannot be read" in m for m, _, _ in lines))

    def test_nobody_answering_is_not_up(self):
        fields, said, _, _ = self.read(FakeCloud(error="unreachable"), sentinel=None, ip="")
        self.assertIsNone(fields["grid_up"])
        with mock.patch.multiple(main, BLACKOUT_MODE=True):
            on, why = main.decide_night({"pv_kwh": 2.0, "hours": []}, 0.0, 0.0, 0.0,
                                        fields["grid_up"], said)
        self.assertFalse(on)
        self.assertIn("nothing can say", why)

    def test_the_sentinel_alone_still_works(self):
        fields, _, _, _ = self.read(None, sentinel=True)
        self.assertEqual((fields["grid_up"], fields["grid_source"]), (True, "sentinel"))

    def test_switched_off_the_cloud_is_not_called(self):
        cloud = mock.Mock(wraps=FakeCloud(reading()))
        fields, _, _, _ = self.read(cloud, sentinel=True, ask=False)
        cloud.grid.assert_not_called()
        self.assertEqual((fields["grid_up"], fields["grid_source"]), (True, "sentinel"))
        self.assertIsNone(fields["grid_volts"])

    def test_switched_off_with_no_sentinel_the_night_does_not_buy(self):
        fields, said, _, _ = self.read(FakeCloud(reading()), sentinel=None, ip="", ask=False)
        self.assertIsNone(fields["grid_up"])
        self.assertIn("switched off from the page", said)

    def test_the_sentinel_switched_off_is_not_knocked_on(self):
        fields, _, lines, _ = self.read(FakeCloud(reading()), sentinel=False, knock=False)
        self.assertEqual(self.knocked, 0)
        self.assertEqual(fields["grid_source"], "inverter")
        # Not asked is not silent: no contradiction to warn about.
        self.assertFalse([m for m, _, _ in lines if "believing the inverter" in m])

    def test_the_sentinel_switched_off_does_not_stand_in_for_the_cloud(self):
        fields, said, lines, _ = self.read(FakeCloud(error="unreachable"), sentinel=True,
                                           knock=False)
        self.assertIsNone(fields["grid_up"])
        self.assertIn("the sentinel is switched off from the page", said)
        self.assertTrue(any("the night will not buy until it is back" in m
                            for m, _, _ in lines))
        with mock.patch.multiple(main, BLACKOUT_MODE=True):
            on, why = main.decide_night({"pv_kwh": 2.0, "hours": []}, 0.0, 0.0, 0.0,
                                        fields["grid_up"], said)
        self.assertFalse(on)

    def test_switched_back_on_the_inverter_has_to_hold_the_grid_again(self):
        # Held for half an hour, then left out from the page: nobody watched
        # it in between, so that half hour no longer counts.
        _, _, _, watch = self.held(30)
        self.read(FakeCloud(reading()), watch=watch, ask=False)
        fields, said, _, _ = self.read(FakeCloud(reading()), watch=watch)
        self.assertIs(fields["grid_up"], False)
        self.assertEqual(fields["grid_stable_s"], 0)

    def test_mains_the_sentinel_cannot_see_is_warned_once(self):
        # The inverter reads mains, the sentinel is silent: one of them is
        # wrong, most likely an unplugged sentinel.
        _, _, first, watch = self.read(FakeCloud(reading()), sentinel=False)
        _, _, second, _ = self.read(FakeCloud(reading()), sentinel=False, watch=watch)
        warned = [m for m, _, _ in first + second if "believing the inverter" in m]
        self.assertEqual(len(warned), 1)

    def test_each_change_of_answer_is_logged_once(self):
        _, _, first, watch = self.held(10)
        _, _, again, _ = self.read(FakeCloud(reading()), watch=watch)
        _, _, down, _ = self.read(FakeCloud(reading(175.9, 0.0)), watch=watch)
        self.assertTrue(any(m.startswith("grid is back") or m.startswith("grid up")
                            for m, _, _ in first))
        self.assertFalse([m for m, _, _ in again if m.startswith("grid")])
        self.assertTrue(any(m.startswith("grid went DOWN") and "rejected" in m
                            for m, _, _ in down))


if __name__ == "__main__":
    unittest.main()
