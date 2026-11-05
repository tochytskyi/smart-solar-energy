"""Whether the mains is there, as the inverter sees it, from the Deye Cloud OpenAPI.

This is blackout mode's first opinion, and the only thing read off the
inverter. The battery, the PV strings and the power flows are all in the same
reply and all deliberately ignored: the decision is the plug's meter and the
forecast, and the inverter is only ever asked "is the grid connected?".

The logger on this site (a DYDA_WiBLE stick) speaks no local Modbus, so the
cloud is the only way in. That has two consequences the caller deals with:

  - the reading is whatever the logger last uploaded, so it carries its own
    age, and a stale one is not an answer;
  - an outage can take the internet down with it, so silence here is not an
    outage either - the LAN sentinel decides then.

Credentials are the app's (DEYE_APP_ID / DEYE_APP_SECRET) and the account's
(DEYE_EMAIL / DEYE_PASSWORD): the app keys say which program is asking, the
login says whose station it may read. The password leaves this process only
as the SHA-256 the API asks for, and none of the four is ever put in an error.
"""

import hashlib
import json
import time
import urllib.error
import urllib.request

# The keys this module reads, and the only ones. Every other key in the reply
# - SOC, PV, load, battery - is left where it lies.
GRID_VOLTAGES = ("GridVoltageL1", "GridVoltageL2", "GridVoltageL3")
GRID_FREQUENCY = "GridFrequency"
GRID_STATUS = "GridStatus"

# Whether the inverter has the grid is its own call, not a threshold guessed
# here. Its history on this site shows how it says no: with the weakest phase
# under about 181 V it reports GridFrequency 0 while the voltage is still
# there - at the same second it raised "W3 Grid_Phase_Wrong" - and goes back to
# 50 Hz when it takes the grid again. It bought 3.9 kW at 188 V without
# complaint, so a voltage floor of our own would only refuse grid it accepts.
#
# GridStatus does not say this. It names the direction of flow - "Purchasing
# energy", "Grid connected" (exporting), "Static" (nothing flowing) - and reads
# "Static" both through an outage and through a sunny afternoon with 233 V on
# every phase. It is recorded for the page and never decided on.
#
# A phase under this is absent rather than low: a three-phase hybrid cannot use
# a grid with a phase missing, and one came back at 0 V here after an outage
# while the other two had already returned.
PHASE_PRESENT_VOLTS = 100.0

# The logger's reading was under a minute old when first probed, so asking
# more often than this would mostly spend quota on the same reading: one pass a minute is 43,200 calls a month, under
# the 100,000 the developer plan allows, whatever CHECK_INTERVAL is set to.
CACHE_SECONDS = 60


class DeyeError(Exception):
    """The cloud could not say. Never carries a credential or a token."""


class Cloud:
    """One account's inverter, with its token and serial looked up once."""

    def __init__(self, base, app_id, app_secret, email, password, timeout=15):
        self._base = base.rstrip("/")
        self._app_id = app_id
        self._login = {
            "appSecret": app_secret,
            "email": email,
            "password": hashlib.sha256(password.encode("utf-8")).hexdigest(),
        }
        self._timeout = timeout
        self._token = None
        self._token_until = 0.0
        self._serial = None
        self._cached = None
        self._cached_at = 0.0

    @property
    def serial(self):
        """The inverter being read, once it has been found."""
        return self._serial

    def _post(self, path, body, auth=True):
        request = urllib.request.Request(
            self._base + path, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json"})
        if auth:
            request.add_header("Authorization", "bearer " + self._token)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                reply = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                self._token = None
            raise DeyeError("HTTP %d" % exc.code)
        except urllib.error.URLError as exc:
            # The reason only: str(exc) can carry the URL, and the token call's
            # URL carries the app id.
            raise DeyeError("unreachable: %s" % getattr(exc, "reason", type(exc).__name__))
        except (OSError, ValueError) as exc:
            raise DeyeError("%s" % type(exc).__name__)
        if not reply.get("success"):
            # An expired token answers with a code, not a 401, and nothing in
            # the docs says which - so any refusal sends the next call back
            # through the login, which is one cheap call.
            self._token = None
            raise DeyeError("refused: %s %s" % (reply.get("code"), reply.get("msg")))
        return reply

    def _authorise(self):
        if self._token and time.time() < self._token_until:
            return
        self._token = None
        reply = self._post("/v1.0/account/token?appId=%s" % self._app_id,
                           self._login, auth=False)
        token = reply.get("accessToken")
        if not token:
            raise DeyeError("no token in the reply")
        self._token = token
        # Valid for about 60 days; renewed a day early rather than discovered
        # expired in the middle of a night.
        self._token_until = time.time() + max(0.0, float(reply.get("expiresIn") or 0) - 86400)

    def _find_inverter(self):
        if self._serial:
            return self._serial
        stations = self._post("/v1.0/station/list", {"page": 1, "size": 20})
        ids = [s.get("id") for s in stations.get("stationList") or [] if s.get("id")]
        if not ids:
            raise DeyeError("no station on this account")
        devices = self._post("/v1.0/station/device",
                             {"page": 1, "size": 20, "stationIds": ids})
        for device in devices.get("deviceListItems") or []:
            if device.get("deviceType") == "INVERTER" and device.get("deviceSn"):
                self._serial = device["deviceSn"]
                return self._serial
        raise DeyeError("no inverter on this account")

    def grid(self):
        """The inverter's latest view of the mains, or DeyeError.

        A dict of accepted (bool - whether the inverter had the grid at that
        reading), volts (the weakest phase), hz, status (Deye's flow word,
        recorded only) and collected (epoch seconds of the logger's reading).
        Whether the grid is steady enough to buy from is GridWatch's call.
        """
        now = time.monotonic()
        if self._cached is not None and now - self._cached_at < CACHE_SECONDS:
            return self._cached
        self._authorise()
        serial = self._find_inverter()
        reply = self._post("/v1.0/device/latest", {"deviceList": [serial]})
        reading = parse(reply, serial)
        self._cached, self._cached_at = reading, now
        return reading


def parse(reply, serial):
    """The grid fields out of one /device/latest reply."""
    for device in reply.get("deviceDataList") or []:
        if device.get("deviceSn") != serial:
            continue
        values = {item.get("key"): item.get("value") for item in device.get("dataList") or []}
        try:
            # The weakest phase: a three-phase inverter drops the grid when any
            # one of them goes, so the best phase says nothing.
            volts = min(float(values[key]) for key in GRID_VOLTAGES)
            hz = float(values[GRID_FREQUENCY])
            collected = float(device["collectionTime"])
        except (KeyError, TypeError, ValueError):
            raise DeyeError("the reply has no grid voltage, frequency or time")
        return {
            "accepted": hz > 0 and volts >= PHASE_PRESENT_VOLTS,
            "volts": volts,
            "hz": hz,
            "status": values.get(GRID_STATUS),
            "collected": collected,
        }
    raise DeyeError("the reply does not mention inverter %s" % serial)
