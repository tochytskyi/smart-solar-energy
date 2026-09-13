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


class TheInverterIsNotRead(unittest.TestCase):
    """The decision is the plug's meter and the forecast, and nothing else.

    Deleting deye_client.py is easy to undo by accident - a reinstated import,
    a stray SOC term back in the arithmetic. This fails if either comes back.
    """

    SOURCES = ("main.py", "check.py", "dashboard.py", "history.py",
               "solar_forecast.py", "demo.py", "dashboard.html")

    def test_nothing_reaches_for_an_inverter(self):
        for name in self.SOURCES:
            self.assertNotIn("deye", (ROOT / name).read_text().lower(),
                             "%s still reaches for the inverter" % name)

    def test_no_battery_charge_is_left_in_the_recorded_numbers(self):
        self.assertNotIn("soc", history.SAMPLE_FIELDS)
        self.assertNotIn("soc", dashboard.CSV_COLUMNS)

    def test_the_inverter_credentials_are_gone_from_the_config(self):
        documented = re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", EXAMPLE, re.M)
        self.assertEqual([key for key in documented if key.startswith("DEYE")], [])


class TheDeployedDocument(unittest.TestCase):
    """DOCKER.md is the behaviour document; the thresholds live in both."""

    def test_it_mentions_every_window_and_threshold(self):
        docker = (ROOT / "DOCKER.md").read_text()
        for key in ("NIGHT_START", "NIGHT_END", "SOLAR_START", "SOLAR_END",
                    "SOLAR_SURPLUS_ON_KW", "SOLAR_SURPLUS_OFF_KW",
                    "DEVICE_DAILY_KWH", "DEVICE_POWER_KW", "HOUSE_DAYTIME_KWH",
                    "BOOST_STRATEGY"):
            self.assertIn(key, docker)


if __name__ == "__main__":
    unittest.main()
