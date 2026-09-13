"""The sandbox every test module runs in.

Importing main.py reads its constants once, at import time, straight from
.env - so the suite has to decide what the watcher is configured with before
that import happens. This package runs first (it is the package the test
modules live in) and does three things:

  - stubs the `tapo` package when it is not installed, so the suite runs on a
    bare interpreter and can never reach a real socket;
  - silences the .env loader, so the site's real credentials are never read
    into the tests and a machine with a different .env gets the same results;
  - puts one fixed set of round constants in the environment, so every number
    the tests expect can be worked out by hand from the values below.

Run the lot from the project root:

    python -m unittest discover -v
"""

import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import tapo                                     # noqa: F401
except ImportError:
    _tapo = types.ModuleType("tapo")

    class ApiClient:
        """Stand-in for the real client: the tests never talk to hardware."""

        def __init__(self, *args, **kwargs):
            raise AssertionError("the tests must not open a Tapo session")

    _tapo.ApiClient = ApiClient
    sys.modules["tapo"] = _tapo

import tapo_client

# config() calls this on every lookup, so replacing it here keeps the real
# .env - and the credentials in it - out of the whole suite.
tapo_client.load_env = lambda *args, **kwargs: None

# The watcher under test. Round numbers on purpose: an 8 kWh house across an
# eight-hour solar window is exactly 1.0 kW the roof owes the house before the
# load sees anything, so every surplus in the tests is "forecast kW minus one".
ENV = {
    "PLUG_B_IP": "10.0.0.5",
    "CHECK_INTERVAL": "60",

    "NIGHT_START": "00:00",
    "NIGHT_END": "07:00",
    "SOLAR_START": "10:00",
    "SOLAR_END": "18:00",

    "BOOST_STRATEGY": "forecast",
    "SOLAR_SURPLUS_ON_KW": "2.0",
    "SOLAR_SURPLUS_OFF_KW": "1.0",

    "DEVICE_DAILY_KWH": "6",
    "DEVICE_POWER_KW": "2.0",
    "HOUSE_DAYTIME_KWH": "8",

    "RAIN_MM": "0.1",
    "RAIN_PROBABILITY": "60",
    "RAIN_HOURS_FRACTION": "0.5",

    "MONITOR_DAYTIME": "1",

    "HISTORY_DB": "/nonexistent/tests-never-open-this.db",
    "HISTORY_RETENTION_DAYS": "30",
    "HISTORY_SAMPLE_INTERVAL": "300",
    "DASHBOARD_PORT": "0",
    "DASHBOARD_HOST": "127.0.0.1",

    # Sentinels, so a test can prove these never reach the page.
    "TAPO_EMAIL": "nobody@example.invalid",
    "TAPO_PASSWORD": "s3cret-must-never-be-served",

    "LATITUDE": "50.45",
    "LONGITUDE": "30.52",
}
os.environ.update(ENV)

SECRET = ENV["TAPO_PASSWORD"]
