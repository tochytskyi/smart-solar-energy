"""The Settings tab: what it lists, what it stores, and what it refuses.

Every Editor here gets its own temporary file, its own `base` (what .env and
the environment would say) and a fake preflight, so nothing reads the site's
.env or starts an interpreter.
"""

import json
import os
import shutil
import stat
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import dashboard
import history
import main
import overrides
import tapo_client
from tests import REAL_LOAD_ENV, SECRETS

EXAMPLE = """\
# Copy to .env and fill in.

# The account.
TAPO_EMAIL=you@example.com
TAPO_PASSWORD=your-password

# The plug.
PLUG_B_IP=192.168.0.239

# Not ours - compose substitutes it.
# TAPO_IMAGE=ghcr.io/x/tapo:latest

# Seconds between passes.
CHECK_INTERVAL=60

# ---------------------------------------------------------------------------
# Night window. The socket is only switched
# on inside it.
# ---------------------------------------------------------------------------
NIGHT_START=00:00
NIGHT_END=07:00

# ---------------------------------------------------------------------------
# Tunnel, all compose's.
# ---------------------------------------------------------------------------
# NGROK_AUTHTOKEN=token
"""

BASE = {
    "TAPO_EMAIL": "me@example.invalid",
    "TAPO_PASSWORD": SECRETS[0],
    "PLUG_B_IP": "10.0.0.5",
    "CHECK_INTERVAL": "60",
    "NIGHT_START": "00:00",
    "NIGHT_END": "07:00",
}


class Sandbox(unittest.TestCase):

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="tapo-overrides-"))
        (self.dir / "env.example").write_text(EXAMPLE)
        self.path = self.dir / "data" / "settings.env"
        self.checked = []
        self.refuse = None
        self.editor = overrides.Editor(self.path, self.dir / "env.example", BASE, self.check)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def check(self, candidate):
        self.checked.append(overrides.read_env(candidate))
        return self.refuse

    def entry(self, key):
        for section in self.editor.view()["sections"]:
            for group in section["groups"]:
                for item in group["keys"]:
                    if item["key"] == key:
                        return item
        return None


class TheCatalogue(Sandbox):

    def test_it_is_env_example_minus_the_commented_out_keys(self):
        self.assertEqual(overrides.keys(self.editor.sections),
                         ["TAPO_EMAIL", "TAPO_PASSWORD", "PLUG_B_IP", "CHECK_INTERVAL",
                          "NIGHT_START", "NIGHT_END"])

    def test_sections_take_the_header_s_first_sentence(self):
        titles = [(section["title"], section["note"]) for section in self.editor.sections]
        # The tunnel section has no key of ours, so it is not drawn at all.
        self.assertEqual(titles, [("Account and plug", ""),
                                  ("Night window", "The socket is only switched on inside it.")])

    def test_keys_under_one_comment_share_it(self):
        groups = self.editor.sections[1]["groups"]
        self.assertEqual([[item["key"] for item in group["keys"]] for group in groups],
                         [["NIGHT_START", "NIGHT_END"]])
        first = self.editor.sections[0]["groups"]
        self.assertEqual(first[0]["help"], "The account.")
        self.assertEqual(first[2]["help"], "Seconds between passes.")

    def test_the_real_example_parses_into_every_key_it_documents(self):
        editor = overrides.Editor(self.path, base={}, check=self.check)
        self.assertIn("DEVICE_DAILY_KWH", editor.allowed)
        self.assertNotIn("NGROK_AUTHTOKEN", editor.allowed)
        self.assertNotIn("TAPO_IMAGE", editor.allowed)
        for section in editor.sections:
            self.assertTrue(section["title"])


class TheView(Sandbox):

    def test_values_come_from_env_until_the_page_sets_them(self):
        self.assertEqual(self.entry("CHECK_INTERVAL")["value"], "60")
        self.assertFalse(self.entry("CHECK_INTERVAL")["overridden"])
        self.editor.save({"CHECK_INTERVAL": "30"}, [])
        item = self.entry("CHECK_INTERVAL")
        self.assertEqual((item["value"], item["env"], item["overridden"]), ("30", "60", True))

    def test_credentials_are_never_handed_out(self):
        self.editor.save({"TAPO_PASSWORD": "a-new-one-also-secret"}, [])
        served = json.dumps(self.editor.view())
        for secret in SECRETS + ("a-new-one-also-secret", "me@example.invalid"):
            self.assertNotIn(secret, served)
        item = self.entry("TAPO_PASSWORD")
        self.assertEqual((item["secret"], item["set"]), (True, True))
        self.assertNotIn("value", item)
        self.assertNotIn("env", item)

    def test_secret_by_name(self):
        for key in ("TAPO_PASSWORD", "DEYE_APP_SECRET", "TAPO_EMAIL", "NGROK_AUTHTOKEN"):
            self.assertTrue(overrides.is_secret(key), key)
        for key in ("PLUG_B_IP", "CHECK_INTERVAL", "DEYE_APP_ID", "DEYE_API_URL"):
            self.assertFalse(overrides.is_secret(key), key)


class Saving(Sandbox):

    def test_only_what_differs_from_env_is_stored(self):
        self.assertEqual(self.editor.save(dict(BASE, CHECK_INTERVAL="30"), []),
                         ["CHECK_INTERVAL"])
        self.assertEqual(overrides.read_env(self.path), {"CHECK_INTERVAL": "30"})

    def test_an_unchanged_form_changes_nothing_and_checks_nothing(self):
        self.assertEqual(self.editor.save(dict(BASE), []), [])
        self.assertFalse(self.path.exists())
        self.assertEqual(self.checked, [])

    def test_typing_the_env_value_back_stops_overriding(self):
        self.editor.save({"CHECK_INTERVAL": "30"}, [])
        self.assertEqual(self.editor.save({"CHECK_INTERVAL": "60"}, []), ["CHECK_INTERVAL"])
        self.assertEqual(overrides.read_env(self.path), {})

    def test_reset_hands_a_key_back_to_env(self):
        self.editor.save({"CHECK_INTERVAL": "30", "PLUG_B_IP": "10.0.0.9"}, [])
        self.assertEqual(self.editor.save({}, ["CHECK_INTERVAL"]), ["CHECK_INTERVAL"])
        self.assertEqual(overrides.read_env(self.path), {"PLUG_B_IP": "10.0.0.9"})

    def test_blank_is_a_value_not_a_reset(self):
        # Blank means "not set" to config(), which is the built-in default -
        # not what .env says, so it has to be stored to mean anything.
        self.assertEqual(self.editor.save({"CHECK_INTERVAL": ""}, []), ["CHECK_INTERVAL"])
        self.assertEqual(overrides.read_env(self.path), {"CHECK_INTERVAL": ""})

    def test_the_watcher_checks_the_whole_file_it_would_start_on(self):
        self.editor.save({"CHECK_INTERVAL": "30"}, [])
        self.editor.save({"PLUG_B_IP": "10.0.0.9"}, [])
        self.assertEqual(self.checked[-1], {"CHECK_INTERVAL": "30", "PLUG_B_IP": "10.0.0.9"})

    def test_a_refused_check_leaves_the_stored_file_alone(self):
        self.editor.save({"CHECK_INTERVAL": "30"}, [])
        self.refuse = "ValueError: invalid literal for int() with base 10: 'soon'"
        with self.assertRaises(overrides.Refused) as caught:
            self.editor.save({"CHECK_INTERVAL": "soon"}, [])
        self.assertEqual(caught.exception.status, 422)
        self.assertIn("'soon'", str(caught.exception))
        self.assertEqual(overrides.read_env(self.path), {"CHECK_INTERVAL": "30"})
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ["settings.env"])

    def test_the_file_is_the_owner_s_alone(self):
        self.editor.save({"TAPO_PASSWORD": "another"}, [])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_unknown_keys_are_refused_whole(self):
        for values, reset in (({"LD_PRELOAD": "/tmp/x.so"}, []),
                              ({"CHECK_INTERVAL": "30", "PYTHONPATH": "/tmp"}, []),
                              ({}, ["PATH"]),
                              ({"TAPO_IMAGE": "evil"}, [])):
            with self.assertRaises(overrides.Refused):
                self.editor.save(values, reset)
        self.assertFalse(self.path.exists())

    def test_a_value_cannot_smuggle_in_a_second_line(self):
        for value in ("60\nLD_PRELOAD=/tmp/x.so", "60\r", "6\x000", "9" * 501):
            with self.assertRaises(overrides.Refused):
                self.editor.save({"CHECK_INTERVAL": value}, [])
        self.assertFalse(self.path.exists())

    def test_only_strings(self):
        for values in ({"CHECK_INTERVAL": 30}, {"CHECK_INTERVAL": None}, ["CHECK_INTERVAL"]):
            with self.assertRaises(overrides.Refused):
                self.editor.save(values, [])


class TheLoader(unittest.TestCase):
    """tapo_client.load_env: .env fills gaps, the page's file beats everything."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="tapo-loader-"))
        self.saved_file = overrides.FILE
        self.saved_env = dict(os.environ)

    def tearDown(self):
        overrides.FILE = self.saved_file
        os.environ.clear()
        os.environ.update(self.saved_env)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_order(self):
        env = self.dir / ".env"
        env.write_text("ZZ_FROM_ENV=env\nZZ_BOTH=env\nZZ_SHELL=env\n")
        overrides.FILE = self.dir / "settings.env"
        overrides.FILE.write_text("ZZ_BOTH=page\nZZ_SHELL=page\n")
        os.environ["ZZ_SHELL"] = "shell"
        REAL_LOAD_ENV(env)
        self.assertEqual((os.environ["ZZ_FROM_ENV"], os.environ["ZZ_BOTH"],
                          os.environ["ZZ_SHELL"]), ("env", "page", "page"))

    def test_no_files_is_not_an_error(self):
        overrides.FILE = self.dir / "missing.env"
        REAL_LOAD_ENV(self.dir / "missing-too.env")


class TheRestart(unittest.TestCase):

    def test_the_watcher_s_own_refusals_pass_on_the_test_settings(self):
        cloud, forecast = main.preflight()
        self.assertIsNone(cloud)
        self.assertIsNotNone(forecast)

    def test_the_check_runs_the_watcher_s_own_preflight(self):
        self.assertIn("main.preflight()", overrides.PREFLIGHT)
        self.assertIn("overrides.FILE", overrides.PREFLIGHT)

    def test_a_restart_is_handed_the_environment_it_started_with(self):
        source = (Path(main.__file__)).read_text()
        self.assertIn("os.execve(sys.executable, argv, overrides.STARTED_WITH)", source)


class TheRoutes(Sandbox):

    def setUp(self):
        Sandbox.setUp(self)
        self.book = history.Logbook(self.dir / "history.db", retention_days=30)
        self.told = []
        self.server = dashboard.serve("127.0.0.1", 0, self.book, main.settings,
                                      editor=self.editor, on_settings=self.told.append)
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.book.close()
        Sandbox.tearDown(self)

    def call(self, path, body=None, headers=None):
        request = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=None if body is None else json.dumps(body).encode("utf-8"),
            method="GET" if body is None else "POST",
            headers=dict({"Content-Type": "application/json"}, **(headers or {})))
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_the_page_gets_the_catalogue_without_a_credential_in_it(self):
        status, body = self.call("/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["sections"]), 2)
        for secret in SECRETS + ("me@example.invalid",):
            self.assertNotIn(secret, json.dumps(body))

    def test_a_save_is_answered_then_the_watcher_told(self):
        status, body = self.call("/api/settings", {"values": {"CHECK_INTERVAL": "30"}})
        self.assertEqual((status, body), (200, {"changed": ["CHECK_INTERVAL"],
                                                "restarting": True}))
        self.assertEqual(self.told, [["CHECK_INTERVAL"]])

    def test_nothing_changed_restarts_nothing(self):
        status, body = self.call("/api/settings", {"values": {"CHECK_INTERVAL": "60"}})
        self.assertEqual((status, body["restarting"]), (200, False))
        self.assertEqual(self.told, [])

    def test_refusals_carry_their_reason(self):
        status, body = self.call("/api/settings", {"values": {"LD_PRELOAD": "x"}})
        self.assertEqual(status, 400)
        self.assertIn("LD_PRELOAD", body["error"])
        self.refuse = "SystemExit: BOOST_STRATEGY must be 'forecast' or 'rain'"
        status, body = self.call("/api/settings", {"values": {"CHECK_INTERVAL": "30"}})
        self.assertEqual(status, 422)
        self.assertIn("BOOST_STRATEGY", body["error"])
        self.assertEqual(self.told, [])

    def test_another_site_cannot_write(self):
        # What a page elsewhere could make a browser on the LAN send.
        for headers in ({"Content-Type": "text/plain"},
                        {"Content-Type": "application/x-www-form-urlencoded"},
                        {"Origin": "http://evil.example"},
                        {"Origin": "http://127.0.0.1:1"}):
            status, body = self.call("/api/settings",
                                     {"values": {"CHECK_INTERVAL": "30"}}, headers)
            self.assertEqual(status, 403, headers)
            status, body = self.call("/api/control", {"enabled": False}, headers)
            self.assertEqual(status, 403, headers)
        self.assertFalse(self.path.exists())
        self.assertTrue(self.book.control(history.CONTROL_ENABLED, True))
        self.assertEqual(self.told, [])

    def test_this_page_and_curl_can(self):
        origin = {"Origin": "http://127.0.0.1:%d" % self.port}
        self.assertEqual(self.call("/api/control", {"enabled": True}, origin)[0], 200)
        self.assertEqual(self.call("/api/control", {"enabled": True})[0], 200)

    def test_no_editor_no_settings(self):
        server = dashboard.serve("127.0.0.1", 0, self.book, main.settings)
        try:
            port = server.server_address[1]
            for data in (None, b'{"values": {"CHECK_INTERVAL": "30"}}'):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(urllib.request.Request(
                        "http://127.0.0.1:%d/api/settings" % port, data=data,
                        headers={"Content-Type": "application/json"}), timeout=10)
                self.assertEqual(caught.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
