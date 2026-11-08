"""Heat one load (plug B) from free solar when there will be enough, and from
cheap night grid only for the part the sun cannot cover.

Two numbers decide everything: the plug's own meter, which says how much the
load has already had today, and the Open-Meteo yield forecast for this roof,
which says how much the day is going to hand over for nothing. The inverter
is read for one thing only - whether the grid is there, in blackout mode -
and never reaches the arithmetic below.

The load owes itself DEVICE_DAILY_KWH every day. There are two chances to
give it that, and the plug's meter keeps a single running total, so the two
windows share one daily budget:

  00:00-07:00  cheap grid. Buy only the shortfall the day's forecast total
               will not cover: DEVICE_DAILY_KWH - (forecast - house).
  10:00-18:00  top-up. Run from the start of the window until the meter says
               the load has had its DEVICE_DAILY_KWH, on whatever mix of roof
               and grid the day happens to be giving.

That ration is measured two ways and the first to fill ends the day:
DEVICE_DAILY_KWH off the meter, or DEVICE_DAILY_HOURS on the clock. The clock
is there because the load has its own thermostat - on a day nobody drew hot
water it cuts out, the meter barely moves, and a kWh-only brake would leave
the socket closed for the whole window waiting for energy it will never take.

Outside both windows the socket stays off.

BLACKOUT_MODE changes both windows, because during scheduled outages the
house runs on its battery and anything the roof or the grid does not carry
comes out of it. The night buys only while the grid is there: by the
inverter's own grid voltage, read off the Deye Cloud, or - when the cloud
cannot say, or says it too late - by whether BLACKOUT_SENTINEL_IP, a device on
a circuit the battery does not back up, answers on the LAN. And the ration is
no longer split: the roof gets the day only when it can carry at least
BLACKOUT_SOLAR_SHARE of DEVICE_DAILY_KWH, otherwise the night buys all of it.
The day window then runs
only in hours forecast to make DEVICE_POWER_KW or more - an hour under that is
an hour the load would be drawing the difference from the battery.

All of that can be suspended from the page. Paused, the watcher still reads
the meter, still fetches the sky and still records every pass - it simply
stops commanding the relay, so the socket keeps whatever state it had, and it
asks the plug what that state is rather than assuming. The switch lives in the
logbook, so it survives a restart.

Outside blackout mode the forecast is one number a day - the total the roof
should make - and it only ever sizes the night's buy. Nothing there tries to
work out which hours will be sunny: a day that comes in under forecast costs
some energy at the day tariff, not a load left cold. Blackout mode is the one
place the hours count, and it judges the night by the same hours the day will
run in, so a day the gate will not open is a day the night buys in full.

Every pass is written to a SQLite logbook (history.py) and served as a live
page on DASHBOARD_PORT (dashboard.py), so what the program did last night can
be read back rather than remembered. Config lives in .env. Run with:
    python main.py
"""

import asyncio
import os
import signal
import sys
import time
from datetime import datetime, timedelta

import dashboard
import deye_cloud
import history
import overrides
import solar_forecast
from solar_forecast import in_window, parse_hhmm, window_hours
from tapo_client import client, config, host_answers

PLUG_IP = config("PLUG_B_IP")               # the only plug this program controls
CHECK_INTERVAL = int(config("CHECK_INTERVAL", "60", required=False))

# Cheap-tariff window. The socket is only ever switched on inside it.
NIGHT_START = parse_hhmm(config("NIGHT_START", "00:00", required=False))
NIGHT_END = parse_hhmm(config("NIGHT_END", "07:00", required=False))
# The part of the day the roof is expected to carry the load.
SOLAR_START = parse_hhmm(config("SOLAR_START", "10:00", required=False))
SOLAR_END = parse_hhmm(config("SOLAR_END", "18:00", required=False))

BOOST_STRATEGY = config("BOOST_STRATEGY", "forecast", required=False).strip().lower()

# The load and the house, in kWh/day and kW. These drive the whole arithmetic.
DEVICE_DAILY_KWH = float(config("DEVICE_DAILY_KWH", "6", required=False))
DEVICE_POWER_KW = float(config("DEVICE_POWER_KW", "2.0", required=False))
HOUSE_DAYTIME_KWH = float(config("HOUSE_DAYTIME_KWH", "8", required=False))

# The same daily ration measured by the clock instead of the meter, and the
# reason there are two: the load has its own thermostat. On a day nobody drew
# any hot water it cuts out, the plug measures almost nothing, and a meter-only
# brake never releases - the socket sits closed for the whole window waiting for
# kWh that are never going to arrive. Whichever measure fills first ends the day.
# 0 switches this brake off and leaves the meter alone in charge.
DEVICE_DAILY_HOURS = float(config("DEVICE_DAILY_HOURS", "3", required=False))

# How long the cheap window lasts. Only used to warn at startup when it is too
# short to deliver the budget at DEVICE_POWER_KW - the night cannot buy 6 kWh
# through a 2 kW load in three hours, and silently falling short is worse than
# being told before the first night runs.
NIGHT_WINDOW_HOURS = window_hours(NIGHT_START, NIGHT_END)

# What counts as a wet hour for the rain strategy, and how many of them make
# a day "mostly rainy".
RAIN_MM = float(config("RAIN_MM", "0.1", required=False))
RAIN_PROBABILITY = float(config("RAIN_PROBABILITY", "60", required=False))
RAIN_HOURS_FRACTION = float(config("RAIN_HOURS_FRACTION", "0.5", required=False))

# Keep reading the plug and the sky outside the night window too. Nothing is
# switched then - it only means the record, and so the dashboard, covers the
# whole day instead of going blank at breakfast. Set to 0 for the old behaviour.
MONITOR_DAYTIME = config("MONITOR_DAYTIME", "1", required=False).strip().lower() \
    not in ("0", "false", "no", "off")

# Blackout mode: the night buys grid only while the grid is actually there,
# judged by whether a device that only has power when the grid does is answering
# on the LAN. Without it, an outage at 00:00 means the inverter feeds the load
# from the battery, and the "cheap" night empties the battery the house needs.
BLACKOUT_MODE = config("BLACKOUT_MODE", "0", required=False).strip().lower() \
    not in ("0", "false", "no", "off")
BLACKOUT_SENTINEL_IP = config("BLACKOUT_SENTINEL_IP", "", required=False).strip()
# Any port will do: a refused connection still proves the host is up. A port it
# actually listens on is safer, because some devices drop rather than refuse.
BLACKOUT_SENTINEL_PORT = int(config("BLACKOUT_SENTINEL_PORT", "80", required=False))
# How much of the ration the roof must be able to carry before blackout mode
# leaves the day to it. Below this the night buys the whole ration; at or above
# it the night buys nothing, and the load takes what the strong hours give.
BLACKOUT_SOLAR_SHARE = float(config("BLACKOUT_SOLAR_SHARE", "0.7", required=False))

# The inverter's own view of the grid, from the Deye Cloud: blackout mode's
# first opinion whenever its credentials are set, with the sentinel above as
# the fallback. A reading older than DEYE_MAX_AGE is no answer at all - an
# outage can take the internet with it, and the last thing the logger managed
# to upload would then say "grid up" for as long as the outage lasts.
DEYE_APP_ID = config("DEYE_APP_ID", "", required=False).strip()
DEYE_API_URL = config("DEYE_API_URL", "https://eu1-developer.deyecloud.com",
                      required=False).strip()
DEYE_MAX_AGE = float(config("DEYE_MAX_AGE", "300", required=False))
# How long the inverter must have held the grid, without a single rejected
# reading, before the night buys from it. A weak grid here flaps - rejected,
# taken back, rejected again, a minute apart for a quarter of an hour - and a
# socket closed on the first good minute is a socket on the battery the next.
# It also covers the inverter's own reconnect delay after an outage.
DEYE_GRID_SETTLE = float(config("DEYE_GRID_SETTLE", "600", required=False))

# Where the history lives, how long it is kept, and how often an unchanged
# state is re-recorded. Changes are always recorded at once; this interval only
# governs the quiet stretches, and keeps the Pi's SD card from being written
# 1440 times a day for no new information.
HISTORY_DB = config("HISTORY_DB", "data/history.db", required=False)
HISTORY_RETENTION_DAYS = float(config("HISTORY_RETENTION_DAYS", "30", required=False))
HISTORY_SAMPLE_INTERVAL = int(config("HISTORY_SAMPLE_INTERVAL", "300", required=False))

# The status page. Port 0 switches it off. It reads the logbook, and writes
# exactly one thing back: the pause below.
DASHBOARD_PORT = int(config("DASHBOARD_PORT", "8080", required=False))
DASHBOARD_HOST = config("DASHBOARD_HOST", "0.0.0.0", required=False)

LOGBOOK = None      # set in main(); every log line is copied into it
FILED = {}          # day -> the fetch its stored curve came from


def log(message, level="info", category="system"):
    """Print one line, and keep it - the dashboard's Log panel reads these back."""
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), message), flush=True)
    if LOGBOOK is not None:
        LOGBOOK.event(message, level, category)


def announce_control(name, on):
    """Log a flip of one of the page's switches, on the thread that served the click.

    The loop reads the switches itself on its next pass, so this changes
    nothing - it only means the Log panel shows the flip at the second it
    happened rather than up to a CHECK_INTERVAL later.
    """
    if name in (history.CONTROL_INVERTER, history.CONTROL_SENTINEL):
        log("%s switched %s from the page - %s%s" % (
            name, "back ON" if on else "OFF", who_decides(
                LOGBOOK.control(history.CONTROL_INVERTER, True),
                LOGBOOK.control(history.CONTROL_SENTINEL, True)),
            ", once it has held the grid for %d min" % (DEYE_GRID_SETTLE // 60)
            if on and name == history.CONTROL_INVERTER and DEYE_APP_ID else ""),
            "info" if on else "warn", "system")
        return
    log("resumed from the page - switching the socket again" if on
        else "PAUSED from the page - the socket is left exactly as it is",
        "info" if on else "warn", "decision")


def who_decides(ask_inverter, ask_sentinel):
    """Who judges the grid with the page's two switches set so, in words for the log."""
    inverter = ask_inverter and bool(DEYE_APP_ID)
    sentinel = ask_sentinel and bool(BLACKOUT_SENTINEL_IP)
    if inverter and sentinel:
        return "the inverter decides the grid, the sentinel when it cannot say"
    if inverter:
        return "only the inverter decides the grid - when it cannot say, the night will not buy"
    if sentinel:
        return "only the sentinel decides the grid"
    return "nothing decides the grid, so the night will not buy"


def settings():
    """The operating parameters, for the dashboard. Never any credentials."""
    return {
        "plug_ip": PLUG_IP,
        "strategy": BOOST_STRATEGY,
        "check_interval": CHECK_INTERVAL,
        "sample_interval": HISTORY_SAMPLE_INTERVAL,
        "monitor_daytime": MONITOR_DAYTIME,
        "night_start": NIGHT_START.strftime("%H:%M"),
        "night_end": NIGHT_END.strftime("%H:%M"),
        "solar_start": SOLAR_START.strftime("%H:%M"),
        "solar_end": SOLAR_END.strftime("%H:%M"),
        "device_daily_kwh": DEVICE_DAILY_KWH,
        "device_daily_hours": DEVICE_DAILY_HOURS,
        "device_power_kw": DEVICE_POWER_KW,
        "house_daytime_kwh": HOUSE_DAYTIME_KWH,
        "blackout_mode": BLACKOUT_MODE,
        "blackout_sentinel": "%s:%d" % (BLACKOUT_SENTINEL_IP, BLACKOUT_SENTINEL_PORT)
                             if BLACKOUT_MODE and BLACKOUT_SENTINEL_IP else None,
        "blackout_inverter": BLACKOUT_MODE and bool(DEYE_APP_ID),
        "blackout_solar_share": BLACKOUT_SOLAR_SHARE,
        "inverter_max_age": DEYE_MAX_AGE,
        "inverter_settle": DEYE_GRID_SETTLE,
    }


def inverter():
    """The Deye Cloud client, or None when blackout mode has no use for one.

    Read here rather than at import, like the Tapo login: the secret, the
    email and the password are never module constants, so nothing that walks
    this module - settings() above, a test, a traceback - can stumble on them.
    """
    if not (BLACKOUT_MODE and DEYE_APP_ID):
        return None
    return deye_cloud.Cloud(DEYE_API_URL, DEYE_APP_ID, config("DEYE_APP_SECRET"),
                            config("DEYE_EMAIL"), config("DEYE_PASSWORD"))


class Plug:
    """A P110 handle that re-authenticates itself when the session goes stale."""

    def __init__(self, api, ip):
        self._api = api
        self._ip = ip
        self._device = None
        self._state = None      # last state we successfully commanded
        self._read_failed = set()   # reads already reported broken, so we say it once
        self._announced = False     # whether the first connection has been logged

    @property
    def state(self):
        """Last state we successfully commanded, or None if unknown."""
        return self._state

    async def _connect(self):
        self._device = await self._api.p110(self._ip)
        # Said once. _read drops the handle and reconnects on every pass while
        # the plug is unwell, and a line a minute here would bury the warning
        # that actually matters. Coming back is reported by _read instead.
        if not self._announced:
            self._announced = True
            log("connected to plug at %s" % self._ip, category="plug")
        return self._device

    def forget(self):
        """Drop what we believe the relay is doing, so the next command is sent.

        Only used when coming back from a pause. While the watcher was paused
        the socket may have been switched by hand in the Tapo app, and
        set_state skips the round trip when it thinks it is already there -
        so without this, resuming could leave the relay wherever a person put
        it and never notice.
        """
        self._state = None

    async def set_state(self, on):
        """Switch the relay, skipping the round trip if it is already there."""
        if self._state is on:
            return True

        name = "ON" if on else "OFF"
        for attempt in (1, 2):
            try:
                device = self._device or await self._connect()
                await (device.on() if on else device.off())
                self._state = on
                log("socket is %s" % name, category="plug")
                return True
            except Exception as exc:
                msg = str(exc)
                log("turn %s failed (attempt %d): %s" % (name, attempt, type(exc).__name__),
                    "error", "plug")
                if "403" in msg or "InvalidResponse" in msg or "Forbidden" in msg:
                    log("  plug is refusing local login (403). Enable Third-Party", "warn", "plug")
                    log("  Compatibility for it in the Tapo app, then retry.", "warn", "plug")
                elif "HostUnreachable" in msg or "No route to host" in msg:
                    log("  no IP route to the plug. Check that %s is right and" % self._ip,
                        "warn", "plug")
                    log("  that this host is on the same LAN as the plug.", "warn", "plug")
                else:
                    log("  %s" % msg[:400], "warn", "plug")
                # Force a fresh handshake, and re-send next time: after a failure
                # we no longer know what the relay is actually doing.
                self._device = None
                self._state = None
                if attempt == 2:
                    return False
                await asyncio.sleep(2)
        return False

    async def _read(self, call, what):
        """One read off the plug, retried once on a fresh session, or None.

        The retry is the whole point. These plugs expire their session, and a
        read that gave up after one failure would never recover: the stale
        handle is kept, so every following pass fails the same way. set_state
        has always cleared it and tried again; the meter has to as well, and
        it matters more now - the daytime window switches on the meter and
        nothing else, so a socket that never gets a reading never comes on.
        """
        for attempt in (1, 2):
            try:
                device = self._device or await self._connect()
                value = await getattr(device, call)()
            except Exception as exc:
                self._device = None         # force a fresh handshake next time
                if attempt == 2:
                    # Once per outage, not once a minute: this runs every pass.
                    if call not in self._read_failed:
                        self._read_failed.add(call)
                        # The tapo library raises bare Exception for everything,
                        # so the class name alone says nothing - the message
                        # (Tapo(SessionTimeout), an HTTP timeout...) is the cause.
                        log("cannot read the %s: %s: %s" % (
                            what, type(exc).__name__, str(exc)[:400]), "warn", "plug")
                    return None
                await asyncio.sleep(1)
                continue
            if call in self._read_failed:
                self._read_failed.discard(call)
                log("%s readable again" % what, category="plug")
            return value.to_dict()
        return None

    async def energy_today_kwh(self):
        """Energy through this socket since midnight, or None if unavailable.

        The plug's daily counter resets at its own midnight, so it measures
        exactly what has gone into the device today. This is the single daily
        budget both windows spend against.
        """
        usage = await self._read("get_energy_usage", "meter")
        if usage is None:
            return None
        today_wh = usage.get("today_energy")
        return None if today_wh is None else float(today_wh) / 1000.0

    async def is_on(self):
        """What the relay is actually doing, or None if it cannot be read.

        Only asked while the watcher is paused. The rest of the time the state
        it last commanded is the answer and costs no round trip - but paused it
        commands nothing, and the whole point of a pause is that someone else
        is deciding. Recording what we last said would be recording a guess.
        """
        info = await self._read("get_device_info", "relay state")
        if info is None:
            return None
        state = info.get("device_on")
        return None if state is None else bool(state)

    async def power_w(self):
        """What the socket is drawing this second, or None if unavailable.

        Nothing decides on this - it is the proof that the relay did what the
        verdict asked, and it is the only measured power the page has now that
        the inverter is not read.
        """
        power = await self._read("get_current_power", "power meter")
        return None if power is None else power.get("current_power")


def free_solar_kwh(outlook):
    """kWh of today's roof the load can expect after the house has had its share.

    One number for the whole day, not an hour-by-hour count. The hourly
    version tried to predict which individual hours would be sunny enough to
    run the load outright, and refused the rest - so a day that dribbled its
    energy out under cloud handed the load nothing and left it cold. Here the
    forecast only ever sizes the night's grid buy; the day window tops up
    whatever is still missing regardless of how the sun is spread. Being wrong
    now costs some energy at the day tariff instead of a cold load.
    """
    return min(DEVICE_DAILY_KWH, max(0.0, outlook["pv_kwh"] - HOUSE_DAYTIME_KWH))


def strong_hours(outlook):
    """Hours of the solar window forecast to make DEVICE_POWER_KW or more.

    Blackout mode only. Those are the hours the day window may run in, because
    in any other hour the load draws the difference from the battery.
    """
    return sum(1 for row in outlook["hours"] if row["kw"] >= DEVICE_POWER_KW)


def solar_needed_kwh():
    """Blackout mode: the kWh the roof must be able to carry to be given the day."""
    return DEVICE_DAILY_KWH * BLACKOUT_SOLAR_SHARE


def solar_covers(outlook):
    """Blackout mode: whether the roof can carry enough of the ration to have the day.

    Enough is BLACKOUT_SOLAR_SHARE of it, and both ways of counting have to agree. The day total less the house is the
    usual one. The strong hours are the other: the day window only runs in
    them, so a big total spread thin under cloud is no use to it. Judging the
    night on the total alone would skip the buy for a day the gate never
    opens, which is the load left cold all over again.
    """
    needed = solar_needed_kwh()
    return free_solar_kwh(outlook) >= needed \
        and strong_hours(outlook) * DEVICE_POWER_KW >= needed


def night_target_kwh(outlook):
    """How much cheap grid to buy tonight, in kWh.

    Only the shortfall: if the sun will hand over 4 of the 6 kWh for free,
    the grid is asked for 2, not 6. Zero means the day covers it outright and
    the socket should stay off all night.

    In blackout mode it is all or nothing. A shortfall left to the day would
    be met by the battery in any hour the roof falls short, so the night buys
    the whole ration unless the roof can carry BLACKOUT_SOLAR_SHARE of it - and
    then nothing, leaving the rest to whatever the strong hours bring.
    """
    if BLACKOUT_MODE:
        return 0.0 if solar_covers(outlook) else DEVICE_DAILY_KWH
    return min(DEVICE_DAILY_KWH, max(0.0, DEVICE_DAILY_KWH - free_solar_kwh(outlook)))


def budget_spent(delivered, ran_hours):
    """(True, why) once the day's ration is used up, by either measure.

    Two readings of one ration, and the first to fill ends the day for both
    windows. The meter is the honest one while the load is actually drawing;
    the clock is what stops the socket sitting closed through a whole window
    on a day the load's own thermostat keeps cutting out, because nobody drew
    any hot water and the kWh are never going to arrive.

    The hours are checked first: they are the measure that still answers when
    the meter cannot be read at all.
    """
    if DEVICE_DAILY_HOURS > 0 and ran_hours >= DEVICE_DAILY_HOURS:
        return True, "socket has been on %.1f of its %.1f h today" % (
            ran_hours, DEVICE_DAILY_HOURS)
    if DEVICE_DAILY_KWH > 0 and delivered is not None and delivered >= DEVICE_DAILY_KWH:
        return True, "load already had its %.1f kWh today" % DEVICE_DAILY_KWH
    return False, None


def spent_so_far(delivered, ran_hours):
    """How much of the ration is gone, by whichever measures are switched on."""
    parts = []
    if DEVICE_DAILY_KWH > 0 and delivered is not None:
        parts.append("%.1f of %.1f kWh" % (delivered, DEVICE_DAILY_KWH))
    if DEVICE_DAILY_HOURS > 0:
        parts.append("%.1f of %.1f h" % (ran_hours, DEVICE_DAILY_HOURS))
    return ", ".join(parts)


def decide_night(outlook, wet_fraction, delivered, ran_hours=0.0, grid_up=None,
                 grid_said=None):
    """(socket on?, why) inside the cheap-grid window.

    In blackout mode a night that wants to buy also needs the grid to be there.
    Anything but a positive answer holds off: the cost of being wrong one way
    is a load topped up at the day tariff, the other way is a battery emptied
    into hot water before the house needs it. `grid_said` is GridWatch's
    account of who said the grid was down, for the reason line.
    """
    on, why = night_verdict(outlook, wet_fraction, delivered, ran_hours)
    if on and BLACKOUT_MODE and grid_up is not True:
        said = grid_said or "%s is not answering, so the grid is down" % BLACKOUT_SENTINEL_IP
        return False, why.replace(" - buying", " - would buy", 1) + \
            " - but %s: holding off" % said
    return on, why


def night_verdict(outlook, wet_fraction, delivered, ran_hours=0.0):
    """(socket on?, why) inside the cheap-grid window, as if the grid is there."""
    delivered = delivered or 0.0
    spent, why = budget_spent(delivered, ran_hours)
    if spent:
        return False, why

    if outlook is None:
        return False, "no forecast - holding off"

    if BOOST_STRATEGY == "rain":
        wet = wet_fraction >= RAIN_HOURS_FRACTION
        detail = "%.0f%% of %s-%s looks wet" % (
            wet_fraction * 100,
            SOLAR_START.strftime("%H:%M"), SOLAR_END.strftime("%H:%M"))
        return wet, detail + (" - buying grid" if wet else " - dry enough, waiting for sun")

    free = free_solar_kwh(outlook)
    target = night_target_kwh(outlook)
    context = "%.1f kWh sun, house takes %.1f - %.1f kWh reaches the load free" % (
        outlook["pv_kwh"], HOUSE_DAYTIME_KWH, free)
    if BLACKOUT_MODE:
        context += ", %d h at %.1f kW or more" % (strong_hours(outlook), DEVICE_POWER_KW)

    share = "%.0f%% of %.1f kWh" % (BLACKOUT_SOLAR_SHARE * 100, DEVICE_DAILY_KWH)
    if target <= 0 and BLACKOUT_MODE:
        return False, context + " - carries at least %s, waiting for sun" % share
    if target <= 0:
        return False, context + " - covers %.1f kWh, waiting for sun" % DEVICE_DAILY_KWH
    if delivered >= target:
        return False, context + " - grid share of %.1f kWh delivered" % target
    if BLACKOUT_MODE:
        return True, context + " - short of %s - buying %.1f kWh from the grid," \
            " none of it left to the roof" % (share, target)
    return True, context + " - buying %.1f kWh of it from the grid" % target


def decide_solar(delivered, ran_hours=0.0, hour_kw=None):
    """(socket on?, why) inside the solar window.

    In blackout mode a window that wants to run also needs this hour's
    forecast to reach DEVICE_POWER_KW. `hour_kw` is that forecast, or None
    when there is no row for now - which holds off too: the battery is what
    pays for a guess.
    """
    on, why = solar_verdict(delivered, ran_hours)
    if on and BLACKOUT_MODE and (hour_kw is None or hour_kw < DEVICE_POWER_KW):
        weak = "no forecast for this hour" if hour_kw is None else \
            "this hour is forecast at %.1f kW, under the load's %.1f" % (hour_kw, DEVICE_POWER_KW)
        return False, why.replace(" - topping up", " - would top up", 1) + \
            " - but %s: holding off rather than running on the battery" % weak
    return on, why


def solar_verdict(delivered, ran_hours=0.0):
    """(socket on?, why) inside the solar window, as if every hour were strong.

    No forecast and no threshold: from SOLAR_START the socket simply runs
    until the load has had its day, by the meter or by the clock. Whatever the
    roof is making goes into it first and the grid covers the rest, which is
    the same bargain the night already sized - it just no longer matters which
    hours the sun turns up in.

    A meter that cannot be read still means off while a kWh budget is set. The
    clock would now bound a blind run to DEVICE_DAILY_HOURS, but heating on no
    measurement at all is a bigger change than this window needs, and the old
    daytime branch would otherwise heat until sunset.
    """
    if DEVICE_DAILY_KWH <= 0 and DEVICE_DAILY_HOURS <= 0:
        return True, "no daily budget set - running the whole window"
    spent, why = budget_spent(delivered, ran_hours)
    if spent:
        return False, why
    if DEVICE_DAILY_KWH > 0 and delivered is None:
        return False, "no meter reading - holding off rather than heating blind"
    return True, "%s so far - topping up from roof and grid" % spent_so_far(
        delivered, ran_hours)


class Recorder:
    """Copies each pass into the logbook.

    Anything that changed goes in at once - a five-minute run must not be
    invisible. An unchanged state is re-recorded only once every
    HISTORY_SAMPLE_INTERVAL seconds, which is what keeps a year of "socket
    off, it is Tuesday afternoon" from wearing out the SD card.
    """

    def __init__(self, logbook, interval):
        self._logbook = logbook
        self._interval = interval
        self._written_at = 0.0
        self._key = None
        self._pruned_at = time.monotonic()

    def record(self, **fields):
        key = (fields.get("socket_on"), fields.get("wanted"), fields.get("enabled"),
               fields.get("inverter_enabled"), fields.get("sentinel_enabled"),
               fields.get("grid_up"),
               fields.get("grid_source"), fields.get("reason"))
        now = time.monotonic()
        if key == self._key and now - self._written_at < self._interval:
            return
        self._key = key
        self._written_at = now
        self._logbook.sample(**fields)

        if now - self._pruned_at >= 86400:
            self._pruned_at = now
            self._logbook.prune()


async def sentinel_answers():
    """Whether the blackout sentinel answers, knocked twice before it is called down.

    Twice because one lost packet on Wi-Fi would otherwise open the relay for a
    whole CHECK_INTERVAL and close it again on the next pass - a contactor on a
    water heater does not need that, and a real outage does not go away in a
    second. Run in a thread: a dead host costs the full timeout, every pass.
    """
    for attempt in (1, 2):
        if await asyncio.to_thread(host_answers, BLACKOUT_SENTINEL_IP,
                                   BLACKOUT_SENTINEL_PORT, 2):
            return True
        if attempt == 1:
            await asyncio.sleep(1)
    return False


class GridWatch:
    """Blackout mode's answer to "is the grid there?", and who gave it.

    The inverter first, and its own verdict rather than a threshold of ours:
    it reports 0 Hz the moment it refuses the grid, however much voltage is
    left, and a phase reading 0 V is a phase it cannot use. Even accepted, the
    grid is only "up" once the inverter has held it for DEYE_GRID_SETTLE
    without a rejection in between, timed on the logger's own clock - a weak
    grid flaps, and the inverter waits out its own reconnect delay too.

    The inverter is only reachable through the cloud, so whenever the cloud
    cannot answer, or its reading is older than DEYE_MAX_AGE, the sentinel
    decides instead. It cannot see a weak grid - the device behind it stays
    powered at 175 V - only a dead one. Neither answering is not "up".

    The sentinel is knocked on every pass even while the inverter is being
    believed, so a contradiction between them reaches the log rather than
    waiting for the night the inverter goes quiet to be discovered.

    Either can be left out from the page. Both narrow what the night may
    believe and never widen it: with only the inverter asked, a cloud that
    cannot say means the night does not buy.
    """

    def __init__(self, cloud):
        self._cloud = cloud
        self._trouble = None        # why the inverter is not being believed now
        self._disagree = False
        self._last = None           # (up, source) as last reported
        self._held_since = None     # logger time the inverter last took the grid
        self._ask_sentinel = True

    async def read(self, ask_inverter=True, ask_sentinel=True):
        """(the grid_* fields for the record, the reason-line phrase).

        `ask_inverter` is the page's switch. Off, the cloud is not called at
        all, and the inverter's hold on the grid is forgotten: switched back
        on, it has to be seen holding the grid for the whole settle time
        again, rather than trusting a run it was not watched for.

        `ask_sentinel` is the other one. Off, the sentinel is not knocked on,
        so it can neither stand in for the cloud nor contradict it.
        """
        self._ask_sentinel = ask_sentinel
        fields = {"grid_up": None, "grid_source": None, "grid_volts": None,
                  "grid_l1_v": None, "grid_l2_v": None, "grid_l3_v": None,
                  "grid_hz": None, "grid_age_s": None, "grid_status": None,
                  "grid_stable_s": None, "sentinel_up": None}
        reading = None
        if self._cloud is not None and not ask_inverter:
            self._held_since = None
        elif self._cloud is not None:
            try:
                reading = await asyncio.to_thread(self._cloud.grid)
            except deye_cloud.DeyeError as exc:
                self._doubt("cannot be read (%s)" % exc)
            else:
                age = max(0.0, time.time() - reading["collected"])
                stable = self._hold(reading)
                # Every phase, for the page: the weakest decides, but which
                # one sags is what tells a local fault from the whole street.
                l1, l2, l3 = reading.get("phases") or (None, None, None)
                fields.update(grid_volts=reading["volts"], grid_hz=reading["hz"],
                              grid_l1_v=l1, grid_l2_v=l2, grid_l3_v=l3,
                              grid_age_s=round(age), grid_status=reading["status"],
                              grid_stable_s=None if stable is None else round(stable))
                if age > DEYE_MAX_AGE:
                    self._doubt("last reported %d min ago" % (age // 60))
                    reading = None
                else:
                    self._doubt(None)

        sentinel = await sentinel_answers() if BLACKOUT_SENTINEL_IP and ask_sentinel else None
        # Recorded whoever decides, so the page can show the second opinion
        # beside the first rather than only in a warn line when they differ.
        fields["sentinel_up"] = sentinel
        where = "%s:%d" % (BLACKOUT_SENTINEL_IP, BLACKOUT_SENTINEL_PORT)
        if reading is not None:
            up, said = self._verdict(reading, fields["grid_stable_s"])
            fields.update(grid_up=up, grid_source="inverter")
            self._compare(reading, sentinel, where)
        elif sentinel is not None:
            fields.update(grid_up=sentinel, grid_source="sentinel")
            said = "sentinel %s %s" % (where, "answers" if sentinel else "is not answering")
        elif self._cloud is not None and not ask_inverter:
            said = "the inverter is switched off from the page and there is no sentinel"
        elif BLACKOUT_SENTINEL_IP and not ask_sentinel:
            said = "the sentinel is switched off from the page and the inverter %s" % (
                "cannot say" if self._cloud is not None else "is not configured")
        else:
            said = "nothing can say whether the grid is there"

        down = "%s, so the grid is down" % said if fields["grid_up"] is False else said
        self._report(fields["grid_up"], fields["grid_source"], said)
        return fields, down

    def _hold(self, reading):
        """Seconds the inverter has held the grid without a rejection, or None.

        Kept across passes and timed on the logger's collection stamps, so a
        cached or repeated reading does not stretch it. After a restart it
        starts from the first reading seen, which errs towards waiting.
        """
        if not reading["accepted"]:
            self._held_since = None
            return None
        if self._held_since is None or reading["collected"] < self._held_since:
            self._held_since = reading["collected"]
        return reading["collected"] - self._held_since

    @staticmethod
    def _verdict(reading, stable):
        """(up?, the words for it) from one inverter reading."""
        volts, hz = reading["volts"], reading["hz"]
        if hz <= 0 and volts <= 0:
            return False, "the inverter reads no grid (0 V)"
        if hz <= 0:
            return False, "the inverter rejected the grid (0 Hz, weakest phase %.0f V)" % volts
        if volts < deye_cloud.PHASE_PRESENT_VOLTS:
            return False, "the inverter is missing a phase (%.0f V)" % volts
        if stable < DEYE_GRID_SETTLE:
            return False, "the inverter has held the grid only %d of %d min" % (
                stable // 60, DEYE_GRID_SETTLE // 60)
        return True, "the inverter has the grid, weakest phase %.0f V, %.1f Hz" % (volts, hz)

    def _doubt(self, why):
        """Say once when the inverter stops being believed, and once when it is again."""
        if why == self._trouble:
            return
        if why is None:
            log("inverter grid reading usable again - it decides the grid", category="system")
        elif self._trouble is None:
            log("inverter grid reading %s - %s" % (why, "the sentinel decides until it is back"
                if BLACKOUT_SENTINEL_IP and self._ask_sentinel
                else "the night will not buy until it is back"),
                "warn", "system")
        self._trouble = why

    def _compare(self, reading, sentinel_up, where):
        """Warn when the two contradict each other; the inverter is believed either way.

        Only a contradiction about whether there is any mains at all. A weak
        grid the inverter refuses still powers the sentinel, so "rejected"
        against "answers" is the two agreeing, not a fault.
        """
        if sentinel_up is None:
            # Not asked: no contradiction to report, and none to call settled
            # either - switched back on, a sentinel still at odds is warned again.
            self._disagree = False
            return
        live = reading["volts"] >= deye_cloud.PHASE_PRESENT_VOLTS or reading["hz"] > 0
        disagree = sentinel_up != live
        if disagree and not self._disagree:
            log("inverter reads %s but sentinel %s %s - believing the inverter" % (
                "mains" if live else "NO mains", where,
                "answers" if sentinel_up else "is silent"), "warn", "system")
        elif self._disagree and not disagree:
            log("inverter and sentinel agree on the grid again", category="system")
        self._disagree = disagree

    def _report(self, up, source, said):
        """One line per change of answer, so outages read off the Log panel."""
        if (up, source) == self._last:
            return
        first = self._last is None
        self._last = (up, source)
        if up is None:
            log("grid UNKNOWN - %s, the night will not buy" % said, "warn", "system")
        elif up:
            log("grid %s - %s" % ("up" if first else "is back", said), category="system")
        else:
            log("grid %s - %s, the night will not buy" % ("DOWN" if first else "went DOWN", said),
                "warn", "system")


async def look_ahead(forecast, now):
    """(outlook, wet fraction) for the day this night window is deciding about."""
    day = solar_forecast.target_day(now, SOLAR_END)
    try:
        outlook = await asyncio.to_thread(forecast.outlook, day, SOLAR_START, SOLAR_END)
    except solar_forecast.ForecastError as exc:
        log("forecast unavailable: %s" % str(exc)[:160], "warn", "forecast")
        return None, 0.0
    if forecast.clock_skew:
        log("WARNING: this host's clock is %+.1f h off the forecast's %s - every"
            % (-forecast.clock_skew["hours"], forecast.clock_skew["forecast_timezone"]),
            "warn", "forecast")
        log("         window will be shifted. Set TZ in .env to the site's zone.",
            "warn", "forecast")
    await file_curves(forecast, outlook, now, day)
    return outlook, forecast.wet_hour_fraction(outlook, RAIN_MM, RAIN_PROBABILITY)


async def file_curves(forecast, outlook, now, day):
    """Keep today's and tomorrow's curves in the logbook.

    The chart draws them against what the roof actually did, and the page lists
    both: the day being decided, and the one after it. Neither costs a fetch -
    Open-Meteo is asked for two days at a time and the rows are already cached
    - and a curve is only written when the fetch behind it changed, so this
    stays an hourly write rather than one every CHECK_INTERVAL.
    """
    if LOGBOOK is None:
        return
    for target in sorted({now.date(), now.date() + timedelta(days=1), day}):
        if target == day:
            curve = outlook
        else:
            try:
                # In a thread like every other outlook call: the rows are
                # cached, but a refetch here would block the whole loop.
                curve = await asyncio.to_thread(
                    forecast.outlook, target, SOLAR_START, SOLAR_END)
            except solar_forecast.ForecastError:
                continue        # past the two days Open-Meteo was asked for
        if FILED.get(target) != forecast.fetched_at:
            FILED[target] = forecast.fetched_at
            LOGBOOK.forecast(curve)


def preflight():
    """Every refusal the watcher starts with, and nothing it starts.

    main() runs it first, and the page's Settings tab runs it in a fresh
    interpreter on the values it is about to save (overrides.py), so a value
    the watcher would refuse is refused at the page instead of in a container
    that then restarts forever. Importing this module has already parsed every
    number; what is left is what only makes sense together.
    """
    if BOOST_STRATEGY not in ("forecast", "rain"):
        raise SystemExit("BOOST_STRATEGY must be 'forecast' or 'rain', not %r" % BOOST_STRATEGY)
    if BLACKOUT_MODE and not (BLACKOUT_SENTINEL_IP or DEYE_APP_ID):
        # Refused rather than guessed: with nothing to ask, every night would
        # read as an outage and the load would never get its cheap grid.
        raise SystemExit("BLACKOUT_MODE is on but neither BLACKOUT_SENTINEL_IP nor"
                         " DEYE_APP_ID is set.")
    if BLACKOUT_MODE and DEVICE_POWER_KW <= 0:
        # Blackout mode gates the day on it: at 0 every hour is strong enough
        # and no hour ever counts towards covering the day.
        raise SystemExit("BLACKOUT_MODE is on but DEVICE_POWER_KW is %r - it must be"
                         " the load's draw in kW." % DEVICE_POWER_KW)
    if BLACKOUT_MODE and not 0 < BLACKOUT_SOLAR_SHARE <= 1:
        # 0 would hand every day to the roof, however dark; above 1 none of them.
        raise SystemExit("BLACKOUT_SOLAR_SHARE must be above 0 and at most 1, not %r"
                         % BLACKOUT_SOLAR_SHARE)
    # Read, not used: client() needs both, and finding out after the restart
    # is too late to say so on the page.
    config("TAPO_EMAIL")
    config("TAPO_PASSWORD")
    # Refused at startup too, rather than discovered as an inverter that never
    # answers: the client reads all four the moment it is built.
    cloud = inverter()

    forecast = solar_forecast.from_config()
    if forecast is None:
        raise SystemExit("No site configured - set LATITUDE and LONGITUDE in .env.")
    return cloud, forecast


def restart():
    """Become a fresh copy of this process, which reads its settings again.

    The same PID, so Docker sees nothing happen and the compose file's
    restart policy is not involved. It is handed the environment it was first
    started with, not today's os.environ - see overrides.STARTED_WITH.
    """
    argv = getattr(sys, "orig_argv", None) or [sys.executable] + sys.argv
    sys.stdout.flush()
    os.execve(sys.executable, argv, overrides.STARTED_WITH)


async def main():
    global LOGBOOK

    cloud, forecast = preflight()

    LOGBOOK = history.Logbook(HISTORY_DB, HISTORY_RETENTION_DAYS)
    LOGBOOK.prune()
    recorder = Recorder(LOGBOOK, HISTORY_SAMPLE_INTERVAL)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    # Set from the dashboard's thread when the Settings tab has saved: the
    # pass in flight finishes, the logbook is closed, and the process starts
    # over on the new values rather than switching on a mix of old and new.
    restarting = []

    def settings_saved(changed):
        log("settings changed from the page (%s) - restarting to take them"
            % ", ".join(changed), "warn", "system")
        restarting.append(changed)
        loop.call_soon_threadsafe(stop.set)

    editor = overrides.Editor()
    server = None
    if DASHBOARD_PORT:
        try:
            server = dashboard.serve(DASHBOARD_HOST, DASHBOARD_PORT, LOGBOOK, settings,
                                     on_log=log, on_control=announce_control,
                                     editor=editor, on_settings=settings_saved)
        except OSError as exc:
            log("dashboard could not take port %d: %s" % (DASHBOARD_PORT, exc), "error")

    plug = Plug(client(), PLUG_IP)

    log("controlling %s only, %.1f kWh/day budget, %.1f h/day allowance"
        % (PLUG_IP, DEVICE_DAILY_KWH, DEVICE_DAILY_HOURS))
    # Said once a start, so a value that is not what .env reads can be traced
    # to the page rather than puzzled over. Names only - some are passwords.
    page_set = sorted(editor.stored())
    if page_set:
        log("the Settings tab overrides .env for %s (%s)" % (
            ", ".join(page_set), editor.path))
    for name, there in ((history.CONTROL_INVERTER, cloud is not None),
                        (history.CONTROL_SENTINEL, BLACKOUT_MODE and bool(BLACKOUT_SENTINEL_IP))):
        if there and not LOGBOOK.control(name, True):
            log("%s still switched OFF from an earlier session - %s" % (name, who_decides(
                LOGBOOK.control(history.CONTROL_INVERTER, True),
                LOGBOOK.control(history.CONTROL_SENTINEL, True))), "warn", "system")
    if not LOGBOOK.control(history.CONTROL_ENABLED, True):
        log("still PAUSED from an earlier session - the socket will not be switched"
            " until the page resumes it", "warn", "decision")
    log("cheap grid %s-%s (strategy '%s'), top-up from %s until the budget is met" % (
        NIGHT_START.strftime("%H:%M"), NIGHT_END.strftime("%H:%M"), BOOST_STRATEGY,
        SOLAR_START.strftime("%H:%M")))
    if BOOST_STRATEGY == "forecast":
        log("house takes %.1f kWh of the day's roof before the load sees any"
            % HOUSE_DAYTIME_KWH)
    if BLACKOUT_MODE:
        sentinel = "%s:%d answers" % (BLACKOUT_SENTINEL_IP, BLACKOUT_SENTINEL_PORT)
        log("BLACKOUT MODE: the night buys grid only while %s" % (
            "the inverter has held the grid for %d min (Deye Cloud, at most %d min old)%s" % (
                DEYE_GRID_SETTLE // 60, DEYE_MAX_AGE // 60, ", or when it cannot say, while " + sentinel
                if BLACKOUT_SENTINEL_IP else "")
            if cloud else sentinel), "warn", "decision")
        log("BLACKOUT MODE: the night buys the whole %.1f kWh unless the roof can carry"
            " %.0f%% of it, and %s-%s runs only in hours forecast at %.1f kW or more" % (
                DEVICE_DAILY_KWH, BLACKOUT_SOLAR_SHARE * 100, SOLAR_START.strftime("%H:%M"),
                SOLAR_END.strftime("%H:%M"), DEVICE_POWER_KW), "warn", "decision")
    elif DEYE_APP_ID:
        log("DEYE_APP_ID is set but BLACKOUT_MODE is off - the inverter is not asked")

    # The night can only ever deliver what the load can swallow in the hours it
    # has. Worth saying once, loudly, rather than leaving it to be discovered
    # as a load that is cold every overcast morning.
    reach = NIGHT_WINDOW_HOURS * DEVICE_POWER_KW
    if 0 < reach <= DEVICE_DAILY_KWH:
        log("cheap window is %.1f h at %.1f kW = %.1f kWh, %s the %.1f kWh budget"
            " - the rest comes from the %s top-up at day tariff" % (
                NIGHT_WINDOW_HOURS, DEVICE_POWER_KW, reach,
                "exactly" if reach == DEVICE_DAILY_KWH else "short of",
                DEVICE_DAILY_KWH, SOLAR_START.strftime("%H:%M")), "warn")

    grid = GridWatch(cloud)
    last_reason = None
    was_enabled = True
    while not stop.is_set():
        now = datetime.now()

        # The page's switch, re-read every pass so a click takes effect within
        # one CHECK_INTERVAL. A logbook that cannot be read answers True: a
        # broken card must not be able to stop the socket being switched.
        enabled = LOGBOOK.control(history.CONTROL_ENABLED, True)

        night = in_window(now.time(), NIGHT_START, NIGHT_END)
        solar = in_window(now.time(), SOLAR_START, SOLAR_END)

        delivered = drawing = outlook = None
        wet = 0.0
        # Outside both windows nothing is decided, but the plug and the sky are
        # still read when MONITOR_DAYTIME is on, so the record - and the
        # dashboard reading it - covers the whole day rather than the windows.
        if night or solar or MONITOR_DAYTIME:
            delivered = await plug.energy_today_kwh() if DEVICE_DAILY_KWH > 0 else None
            drawing = await plug.power_w()
            outlook, wet = await look_ahead(forecast, now)

        # Whether the grid is there, by the inverter or the sentinel. Asked on
        # every pass while blackout mode is on - not only when the plug is read
        # - so the page always knows, and the record shows the outages
        # themselves and not just the nights they cost. Only the night decides
        # on it.
        # The page's other switches, read the same way and defaulting the same
        # way: an unreadable logbook leaves the inverter and the sentinel asked.
        ask_inverter = LOGBOOK.control(history.CONTROL_INVERTER, True)
        ask_sentinel = LOGBOOK.control(history.CONTROL_SENTINEL, True)
        seen, grid_said = {}, None
        if BLACKOUT_MODE:
            seen, grid_said = await grid.read(ask_inverter, ask_sentinel)

        # How long the relay has already been closed today, read back off the
        # logbook rather than counted in this process, so a restart mid-window
        # does not hand the load a fresh allowance.
        ran_hours = LOGBOOK.on_hours_today()

        # This hour's forecast, which only blackout mode's day window decides on.
        hour = solar_forecast.hour_at(outlook, now) if outlook else None
        hour_kw = hour["kw"] if hour else None

        if night:
            wanted, reason = decide_night(outlook, wet, delivered, ran_hours,
                                          seen.get("grid_up"), grid_said)
        elif solar:
            # Outside blackout mode the day window needs no forecast at all -
            # only the meter and the clock.
            wanted, reason = decide_solar(delivered, ran_hours, hour_kw)
        else:
            wanted, reason = False, "outside both windows"

        if not enabled:
            # Paused: the verdict is still worked out and recorded, so the
            # record shows what the watcher would have done, and the page can
            # say so. The relay is simply never commanded.
            reason = "paused from the page - would be %s: %s" % (
                "ON" if wanted else "off", reason)

        if reason != last_reason:
            log(reason, category="decision")
            last_reason = reason

        if enabled:
            if not was_enabled:
                # Coming back from a pause, re-send whatever the verdict is:
                # the relay may have been switched by hand while we were not
                # looking, so what we last commanded is no longer evidence.
                plug.forget()
            await plug.set_state(wanted)
            socket_on = plug.state
        else:
            # Nothing was commanded, so the only way to know what the socket
            # is doing is to ask it.
            socket_on = await plug.is_on()
        was_enabled = enabled

        free_kwh = target_kwh = None
        if outlook is not None and BOOST_STRATEGY == "forecast":
            free_kwh = free_solar_kwh(outlook)
            target_kwh = night_target_kwh(outlook)
        recorder.record(
            phase="night" if night else "solar" if solar else "idle",
            strategy=BOOST_STRATEGY,
            pv_forecast_kwh=outlook["pv_kwh"] if outlook else None,
            peak_kw=outlook["peak_kw"] if outlook else None,
            cloud_cover=outlook["cloud_cover"] if outlook else None,
            rain_mm=outlook["rain_mm"] if outlook else None,
            wet_fraction=wet if outlook else None,
            plug_power_w=drawing,
            delivered_kwh=delivered,
            ran_hours=ran_hours,
            free_kwh=free_kwh,
            target_kwh=target_kwh,
            hour_kw=hour_kw,
            # Blank outside blackout mode, where no hour gates anything.
            strong_hours=strong_hours(outlook) if outlook and BLACKOUT_MODE else None,
            grid_up=seen.get("grid_up"),
            grid_source=seen.get("grid_source"),
            grid_volts=seen.get("grid_volts"),
            grid_l1_v=seen.get("grid_l1_v"),
            grid_l2_v=seen.get("grid_l2_v"),
            grid_l3_v=seen.get("grid_l3_v"),
            grid_hz=seen.get("grid_hz"),
            grid_age_s=seen.get("grid_age_s"),
            grid_status=seen.get("grid_status"),
            grid_stable_s=seen.get("grid_stable_s"),
            sentinel_up=seen.get("sentinel_up"),
            # Only meaningful where there is an inverter to ask; blank otherwise,
            # so the page does not report a switch that changes nothing.
            inverter_enabled=ask_inverter if cloud is not None else None,
            sentinel_enabled=ask_sentinel if BLACKOUT_MODE and BLACKOUT_SENTINEL_IP else None,
            enabled=enabled,
            wanted=wanted,
            socket_on=socket_on,
            reason=reason,
        )

        try:
            await asyncio.wait_for(stop.wait(), timeout=CHECK_INTERVAL)
        except asyncio.TimeoutError:
            pass

    log("restarting" if restarting else "stopped")
    if server is not None:
        server.shutdown()
        server.server_close()
    LOGBOOK.close()
    if restarting:
        restart()


if __name__ == "__main__":
    asyncio.run(main())
