"""The standing rule, checked by machine.

CLAUDE.md: "a change is not finished until the page shows it". A number the
decision produces has to reach the schema, the recorder, the CSV and the page,
or it is invisible - and an invisible behaviour is what the logbook exists to
prevent. These tests read the source and fail when one of those links is
missing, which is cheaper than noticing a blank card three nights later.
"""

import ast
import re
import unittest
from pathlib import Path

import dashboard
import demo
import history
import main

ROOT = Path(__file__).resolve().parent.parent
MAIN = ast.parse((ROOT / "main.py").read_text())
PAGE = (ROOT / "dashboard.html").read_text()
EXAMPLE = (ROOT / ".env.example").read_text()


def calls(tree, name):
    """Every call to `name` (or `something.name`) in the tree."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        label = target.attr if isinstance(target, ast.Attribute) else \
            target.id if isinstance(target, ast.Name) else None
        if label == name:
            found.append(node)
    return found


def keywords(call):
    return {kw.arg for kw in call.keywords if kw.arg}


def literal(call, index=None, name=None):
    """A string argument of a call, or None if it is not a plain literal."""
    node = None
    if name is not None:
        node = next((kw.value for kw in call.keywords if kw.arg == name), None)
    elif index is not None and len(call.args) > index:
        node = call.args[index]
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


class TheRecordedNumbers(unittest.TestCase):
    """A number the decision uses or produces reaches all four places."""

    def test_every_declared_field_is_a_column(self):
        columns = {name for name, _ in history._schema_columns()["samples"]}
        self.assertEqual(set(history.SAMPLE_FIELDS) - columns, set())

    def test_every_column_is_a_declared_field(self):
        # Otherwise the column exists but nothing ever writes to it.
        columns = {name for name, _ in history._schema_columns()["samples"]}
        self.assertEqual(columns - set(history.SAMPLE_FIELDS) - {"id", "ts"}, set())

    def test_the_recorder_writes_exactly_the_declared_fields(self):
        recorded = set()
        for call in calls(MAIN, "record"):
            recorded |= keywords(call)
        self.assertEqual(recorded, set(history.SAMPLE_FIELDS))

    def test_every_exported_column_exists(self):
        self.assertEqual(
            set(dashboard.CSV_COLUMNS) - set(history.SAMPLE_FIELDS) - {"ts"}, set())

    def test_every_recorded_field_is_drawn_somewhere_on_the_page(self):
        missing = [name for name in history.SAMPLE_FIELDS if name not in PAGE]
        self.assertFalse(missing, "recorded but invisible on the page: %s" % missing)


class TheLogLines(unittest.TestCase):
    """A new log line goes through log(), at a level and in a category the
    page can filter on."""

    CATEGORIES = {"system", "plug", "forecast", "decision"}

    def test_nothing_prints_behind_the_logbook_s_back(self):
        # A bare print never reaches the page's Log panel.
        for call in calls(MAIN, "print"):
            self.assertEqual(call.lineno, main.log.__code__.co_firstlineno + 2,
                             "print() outside log() at main.py:%d" % call.lineno)

    def test_every_level_is_one_the_page_colours(self):
        for call in calls(MAIN, "log"):
            level = literal(call, index=1, name="level")
            if level is not None:
                self.assertIn(level, history.LEVELS)

    def test_every_category_is_one_the_page_filters_on(self):
        for call in calls(MAIN, "log"):
            category = literal(call, index=2, name="category")
            if category is not None:
                self.assertIn(category, self.CATEGORIES,
                              "main.py:%d logs an unknown category" % call.lineno)

    def test_the_log_panel_shows_whatever_category_it_is_given(self):
        # The page prints the category rather than knowing a list of them, so
        # a new one is filterable (the search covers category too) at once.
        self.assertIn("e.category", PAGE)


class TheConfig(unittest.TestCase):
    """A new config key is documented, or nobody knows it exists."""

    def keys(self):
        found = set()
        for name in ("main.py", "tapo_client.py", "solar_forecast.py",
                     "dashboard.py", "history.py", "check.py"):
            found |= set(re.findall(r'config\(\s*"([A-Z0-9_]+)"', (ROOT / name).read_text()))
        # .env is read by docker compose as well as by this program, so a key
        # only the compose file substitutes is documented, not stale - with or
        # without a `:-default`.
        found |= set(re.findall(r"\$\{([A-Z][A-Z0-9_]+)(?::-[^}]*)?\}",
                                (ROOT / "docker-compose.yml").read_text()))
        return found

    def documented(self):
        return set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", EXAMPLE, re.M))

    def test_every_key_the_code_reads_is_in_env_example(self):
        missing = sorted(self.keys() - self.documented())
        self.assertEqual(missing, [], "undocumented config: %s" % missing)

    def test_every_documented_key_is_actually_read(self):
        stale = sorted(self.documented() - self.keys())
        self.assertEqual(stale, [], "documented but unread: %s" % stale)

    def test_every_key_carries_a_comment_saying_what_it_is_for(self):
        lines = EXAMPLE.splitlines()
        for index, line in enumerate(lines):
            match = re.match(r"^#?\s*([A-Z][A-Z0-9_]+)=", line)
            if not match:
                continue
            above = [line for line in lines[max(0, index - 6):index] if line.strip()]
            self.assertTrue(any(line.lstrip().startswith("#") and "=" not in line
                                for line in above),
                            "%s has no comment above it" % match.group(1))


class ThePhases(unittest.TestCase):
    """A window is a phase, and a phase is shaded, coloured and totalled."""

    PHASES = ("night", "solar", "idle")

    def test_the_loop_records_only_the_known_phases(self):
        recorded = set()
        for call in calls(MAIN, "record"):
            for keyword in call.keywords:
                if keyword.arg == "phase":
                    recorded |= {node.value for node in ast.walk(keyword.value)
                                 if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        self.assertEqual(recorded, set(self.PHASES))

    def test_the_page_draws_every_phase(self):
        for phase in self.PHASES:
            self.assertTrue(re.search(r"\b%s\b" % phase, PAGE),
                            "the page never mentions the %r phase" % phase)


class TheDemo(unittest.TestCase):
    """demo.py is how the page is worked on, so it has to stay representative."""

    def test_it_invents_the_same_settings_the_watcher_reports(self):
        self.assertEqual(set(demo.SETTINGS), set(main.settings()))

    def test_it_invents_every_phase(self):
        source = (ROOT / "demo.py").read_text()
        for phase in ThePhases.PHASES:
            self.assertTrue(re.search(r"\b%s\b" % phase, source),
                            "demo.py never produces the %r phase" % phase)


class TheInverterOnlySaysWhetherTheGridIsThere(unittest.TestCase):
    """The decision is the plug's meter and the forecast; the inverter is a grid detector.

    There was a Deye client here that fed the battery and the power flows into
    the arithmetic, and it was deleted. It came back - deliberately, for one
    job - as blackout mode's "is the grid there?", read off the Deye Cloud
    because the logger speaks no local Modbus. These fail if it grows past
    that: a SOC term in a sum, a power flow in the record, or the page and the
    logbook learning which inverter this is.
    """

    # Never mention it: they show what the record says, not where it came from.
    SOURCES = ("dashboard.py", "history.py", "solar_forecast.py", "demo.py", "dashboard.html")
    CLIENT = (ROOT / "deye_cloud.py").read_text()

    def test_only_the_watcher_and_its_report_reach_for_it(self):
        for name in self.SOURCES:
            self.assertNotIn("deye", (ROOT / name).read_text().lower(),
                             "%s reaches for the inverter" % name)

    def test_the_client_hands_back_the_grid_and_nothing_else(self):
        import deye_cloud
        reply = {"deviceDataList": [{"deviceSn": "x", "collectionTime": 0, "dataList": [
            {"key": key, "value": "1"} for key in
            ("GridVoltageL1", "GridVoltageL2", "GridVoltageL3", "GridFrequency",
             "SOC", "BatteryPower", "TotalSolarPower", "LoadPower")]}]}
        self.assertEqual(set(deye_cloud.parse(reply, "x")),
                         {"accepted", "volts", "hz", "status", "collected"})

    def test_the_client_only_ever_reads(self):
        # Four endpoints, every one a query. Nothing that sets, controls or
        # schedules the inverter may appear in it.
        paths = set(re.findall(r'"(/v1\.0/[a-z/]+)', self.CLIENT))
        self.assertEqual(paths, {"/v1.0/account/token", "/v1.0/station/list",
                                 "/v1.0/station/device", "/v1.0/device/latest"})

    def test_the_watcher_asks_it_one_question(self):
        asked = {node.attr for node in ast.walk(MAIN) if isinstance(node, ast.Attribute)
                 and isinstance(node.value, ast.Attribute) and node.value.attr == "_cloud"}
        self.assertEqual(asked, {"grid"})

    def test_the_night_still_takes_a_yes_or_no(self):
        # Not a reading: the arithmetic sees grid_up and the words for the log.
        decide = next(node for node in ast.walk(MAIN)
                      if isinstance(node, ast.FunctionDef) and node.name == "decide_night")
        self.assertEqual([arg.arg for arg in decide.args.args],
                         ["outlook", "wet_fraction", "delivered", "ran_hours",
                          "grid_up", "grid_said"])

    def test_no_battery_charge_is_left_in_the_recorded_numbers(self):
        self.assertNotIn("soc", history.SAMPLE_FIELDS)
        self.assertNotIn("soc", dashboard.CSV_COLUMNS)
        for name in history.SAMPLE_FIELDS:
            self.assertNotRegex(name, r"^soc$|batt|pv_power|load_power",
                                "%s is an inverter number in the record" % name)

    def test_its_config_is_the_grid_reading_and_nothing_else(self):
        documented = re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", EXAMPLE, re.M)
        self.assertEqual(sorted(key for key in documented if key.startswith("DEYE")),
                         ["DEYE_API_URL", "DEYE_APP_ID", "DEYE_APP_SECRET", "DEYE_EMAIL",
                          "DEYE_GRID_SETTLE", "DEYE_MAX_AGE", "DEYE_PASSWORD"])


class TheDecisionUsesTheDayTotal(unittest.TestCase):
    """The forecast reaches the decision as one number a day, not as a curve.

    The hourly version is the natural thing to write, and it was here once: it
    predicted which individual hours would be sunny enough to carry the load
    and refused the rest, so an overcast day that never cleared the gate left
    the load cold. This fails if per-hour gating creeps back.
    """

    def test_free_solar_reads_the_day_total_and_not_the_hours(self):
        source = ast.get_source_segment(
            (ROOT / "main.py").read_text(),
            next(node for node in ast.walk(MAIN)
                 if isinstance(node, ast.FunctionDef) and node.name == "free_solar_kwh"))
        self.assertIn('pv_kwh', source)
        self.assertNotIn('"hours"', source,
                         "free_solar_kwh is walking the curve again")

    def test_the_day_window_takes_no_forecast_at_all(self):
        # Widened once, deliberately: the day window is now the meter and the
        # clock, because a load whose thermostat cuts out never fills the kWh
        # budget and a meter-only brake never releases. What this still
        # forbids is the forecast - an outlook, a curve or a wet fraction
        # reaching this window is the failure it was written for.
        #
        # Widened a second time, deliberately, for blackout mode alone: there
        # an hour forecast under DEVICE_POWER_KW is an hour drawn from the
        # battery, so this hour's kW reaches the window - and nothing reads it
        # unless BLACKOUT_MODE is on. The verdict itself still takes the two.
        decide = next(node for node in ast.walk(MAIN)
                      if isinstance(node, ast.FunctionDef) and node.name == "decide_solar")
        self.assertEqual([arg.arg for arg in decide.args.args],
                         ["delivered", "ran_hours", "hour_kw"],
                         "decide_solar has grown an argument; the day window is"
                         " the meter and the clock, and blackout mode's hour gate")
        verdict = next(node for node in ast.walk(MAIN)
                       if isinstance(node, ast.FunctionDef) and node.name == "solar_verdict")
        self.assertEqual([arg.arg for arg in verdict.args.args], ["delivered", "ran_hours"])

    def test_the_hour_gate_is_blackout_mode_s_alone(self):
        source = ast.get_source_segment(
            (ROOT / "main.py").read_text(),
            next(node for node in ast.walk(MAIN)
                 if isinstance(node, ast.FunctionDef) and node.name == "decide_solar"))
        gates = [line for line in source.splitlines() if "hour_kw <" in line]
        self.assertEqual(len(gates), 1)
        self.assertIn("BLACKOUT_MODE", gates[0])

    def test_no_surplus_thresholds_are_left_anywhere(self):
        for name in TheInverterOnlySaysWhetherTheGridIsThere.SOURCES + (
                "main.py", "check.py", "deye_cloud.py", ".env.example"):
            self.assertNotIn("SOLAR_SURPLUS", (ROOT / name).read_text(),
                             "%s still carries a per-hour surplus gate" % name)


class ThePageChangesOnlyItsDeclaredSwitches(unittest.TestCase):
    """The dashboard reads the logbook and writes its switches and settings, and nothing else.

    It was purely a reader until the pause switch arrived, and the reason it
    could be left unauthenticated on the LAN was that there was nothing to
    write. Widened once, deliberately, for a second switch - whether blackout
    mode may ask the inverter - and again for a third, whether it may ask the
    sentinel. Widened a fourth time, deliberately, for the Settings tab: a
    second route, which writes only through overrides.Editor, only keys
    .env.example documents, and only values the watcher would start on. So
    what it may touch is checked by machine: two routes, a fixed list of
    switches, a fixed catalogue of keys, and no way to reach the relay.
    """

    DASHBOARD = ast.parse((ROOT / "dashboard.py").read_text())
    SOURCE = (ROOT / "dashboard.py").read_text()

    def test_only_one_route_answers_a_post(self):
        post = next(node for node in ast.walk(self.DASHBOARD)
                    if isinstance(node, ast.FunctionDef) and node.name == "do_POST")
        # Every route this handler compares itself against, which is not the
        # same as every path string in it: "/" is how the path is normalised.
        routes = set()
        for node in ast.walk(post):
            if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) \
                    and node.left.id == "route":
                routes |= {other.value for other in node.comparators
                           if isinstance(other, ast.Constant)}
        self.assertEqual(routes, {"/api/control", "/api/settings"})

    def test_the_settings_route_writes_only_through_the_editor(self):
        # One save call, and nothing in dashboard.py that opens a file for
        # writing or touches the environment behind the editor's back.
        self.assertEqual(len(calls(self.DASHBOARD, "save")), 1)
        self.assertIn("editor.save(", self.SOURCE)
        for forbidden in ("os.environ", "open(", "write_text", "putenv"):
            self.assertNotIn(forbidden, self.SOURCE)

    def test_the_editor_offers_only_documented_keys(self):
        import overrides
        editor = overrides.Editor(path=ROOT / "nonexistent" / "settings.env",
                                  base={}, check=lambda path: None)
        documented = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", EXAMPLE, re.M))
        self.assertEqual(editor.allowed, documented)
        for dangerous in ("PATH", "LD_PRELOAD", "PYTHONPATH", "PYTHONSTARTUP", "HOME"):
            self.assertNotIn(dangerous, editor.allowed)

    def test_the_switches_are_exactly_these(self):
        # A fourth one is a decision, not a side effect: it lands here first.
        self.assertEqual(history.CONTROLS, (history.CONTROL_ENABLED, history.CONTROL_INVERTER,
                                            history.CONTROL_SENTINEL))
        self.assertEqual(history.CONTROLS, ("enabled", "inverter", "sentinel"))

    def test_it_writes_nothing_but_a_declared_switch(self):
        # The one set_control call writes a name drawn from history.CONTROLS,
        # and the name is only ever bound by that comprehension.
        writes = calls(self.DASHBOARD, "set_control")
        self.assertEqual(len(writes), 1)
        self.assertIn("for name in history.CONTROLS if name in given", self.SOURCE)
        self.assertEqual(self.SOURCE.count("name = names[0]"), 1)

    def test_no_switch_can_reach_the_relay_or_a_threshold(self):
        # The grid switches narrow what the night believes; they are not a
        # way to make the night buy. Off, the loop only skips the cloud or
        # the sentinel's knock.
        main_source = (ROOT / "main.py").read_text()
        self.assertIn("grid.read(ask_inverter, ask_sentinel)", main_source)

    def test_the_server_cannot_reach_the_plug(self):
        # The switch says whether the watcher may decide. It is not a relay,
        # and nothing served over HTTP is allowed to become one.
        for forbidden in ("set_state", "import tapo", "ApiClient", "PLUG_B_IP"):
            self.assertNotIn(forbidden, self.SOURCE,
                             "dashboard.py can reach the socket itself")

    def test_the_page_posts_nowhere_else(self):
        self.assertEqual(set(re.findall(r'post\(\s*"([^"]+)"', PAGE)),
                         {"/api/control", "/api/settings"})
        # One writer, so a second fetch with a method is a second write.
        self.assertEqual(PAGE.count("method:"), 1)

    def test_a_paused_pass_is_recorded_like_any_other(self):
        # Otherwise the record would show a socket that ignored the verdict
        # with nothing to say why.
        self.assertIn("enabled", history.SAMPLE_FIELDS)
        self.assertIn("enabled", dashboard.CSV_COLUMNS)

    def test_the_watcher_reads_the_switch_rather_than_being_told(self):
        # main.py holds no copy of it: the logbook is the one place it lives,
        # so the page and the loop cannot disagree about what it says.
        self.assertIn("CONTROL_ENABLED", (ROOT / "main.py").read_text())
        self.assertEqual(calls(MAIN, "set_control"), [])


class TheDeployedDocument(unittest.TestCase):
    """DOCKER.md is the behaviour document; the thresholds live in both."""

    def test_it_mentions_every_window_and_threshold(self):
        docker = (ROOT / "DOCKER.md").read_text()
        for key in ("NIGHT_START", "NIGHT_END", "SOLAR_START", "SOLAR_END",
                    "DEVICE_DAILY_KWH", "DEVICE_DAILY_HOURS", "DEVICE_POWER_KW",
                    "HOUSE_DAYTIME_KWH", "BOOST_STRATEGY",
                    "BLACKOUT_MODE", "BLACKOUT_SENTINEL_IP", "BLACKOUT_SENTINEL_PORT",
                    "DEYE_APP_ID", "DEYE_MAX_AGE", "DEYE_GRID_SETTLE"):
            self.assertIn(key, docker)


if __name__ == "__main__":
    unittest.main()
