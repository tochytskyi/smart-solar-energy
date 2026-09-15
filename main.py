"""Heat one load (plug B) from free solar when there will be enough, and from
cheap night grid only for the part the sun cannot cover.

Two numbers decide everything: the plug's own meter, which says how much the
load has already had today, and the Open-Meteo yield forecast for this roof,
which says how much the day is going to hand over for nothing. Nothing is
read from the inverter.

The load owes itself DEVICE_DAILY_KWH every day. There are two chances to
give it that, and the plug's meter keeps a single running total, so the two
windows share one daily budget:

  00:00-07:00  cheap grid. Buy only the shortfall the day's forecast total
               will not cover: DEVICE_DAILY_KWH - (forecast - house).
  10:00-18:00  top-up. Run from the start of the window until the meter says
               the load has had its DEVICE_DAILY_KWH, on whatever mix of roof
               and grid the day happens to be giving.

Outside both windows the socket stays off.

The forecast is one number a day - the total the roof should make - and it
only ever sizes the night's buy. Nothing here tries to work out which hours
will be sunny: a day that comes in under forecast costs some energy at the
day tariff, not a load left cold.

Every pass is written to a SQLite logbook (history.py) and served as a live
page on DASHBOARD_PORT (dashboard.py), so what the program did last night can
be read back rather than remembered. Config lives in .env. Run with:
    python main.py
"""

import asyncio
import signal
import time
from datetime import datetime, timedelta

import dashboard
import history
import solar_forecast
from solar_forecast import in_window, parse_hhmm, window_hours
from tapo_client import client, config

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

# Where the history lives, how long it is kept, and how often an unchanged
# state is re-recorded. Changes are always recorded at once; this interval only
# governs the quiet stretches, and keeps the Pi's SD card from being written
# 1440 times a day for no new information.
HISTORY_DB = config("HISTORY_DB", "data/history.db", required=False)
HISTORY_RETENTION_DAYS = float(config("HISTORY_RETENTION_DAYS", "30", required=False))
HISTORY_SAMPLE_INTERVAL = int(config("HISTORY_SAMPLE_INTERVAL", "300", required=False))

# The read-only status page. Port 0 switches it off.
DASHBOARD_PORT = int(config("DASHBOARD_PORT", "8080", required=False))
DASHBOARD_HOST = config("DASHBOARD_HOST", "0.0.0.0", required=False)

LOGBOOK = None      # set in main(); every log line is copied into it
FILED = {}          # day -> the fetch its stored curve came from


def log(message, level="info", category="system"):
    """Print one line, and keep it - the dashboard's Log panel reads these back."""
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), message), flush=True)
    if LOGBOOK is not None:
        LOGBOOK.event(message, level, category)


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
        "device_power_kw": DEVICE_POWER_KW,
        "house_daytime_kwh": HOUSE_DAYTIME_KWH,
    }


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
                    log("  %s" % msg[:160], "warn", "plug")
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
                        log("cannot read the %s: %s" % (what, type(exc).__name__),
                            "warn", "plug")
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


def night_target_kwh(outlook):
    """How much cheap grid to buy tonight, in kWh.

    Only the shortfall: if the sun will hand over 4 of the 6 kWh for free,
    the grid is asked for 2, not 6. Zero means the day covers it outright and
    the socket should stay off all night.
    """
    return min(DEVICE_DAILY_KWH, max(0.0, DEVICE_DAILY_KWH - free_solar_kwh(outlook)))


def decide_night(outlook, wet_fraction, delivered):
    """(socket on?, why) inside the cheap-grid window."""
    delivered = delivered or 0.0
    if delivered >= DEVICE_DAILY_KWH > 0:
        return False, "load already had its %.1f kWh today" % DEVICE_DAILY_KWH

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

    if target <= 0:
        return False, context + " - covers %.1f kWh, waiting for sun" % DEVICE_DAILY_KWH
    if delivered >= target:
        return False, context + " - grid share of %.1f kWh delivered" % target
    return True, context + " - buying %.1f kWh of it from the grid" % target


def decide_solar(delivered):
    """(socket on?, why) inside the solar window.

    No forecast and no threshold: from SOLAR_START the socket simply runs
    until the meter says the load has had its day. Whatever the roof is making
    goes into it first and the grid covers the rest, which is the same bargain
    the night already sized - it just no longer matters which hours the sun
    turns up in.

    The meter is now the only thing that stops this window, so an unreadable
    one has to mean off. The old daytime branch needed the forecast to say yes
    before it ran at all; this one would otherwise heat until sunset.
    """
    if DEVICE_DAILY_KWH <= 0:
        return True, "no daily budget set - running the whole window"
    if delivered is None:
        return False, "no meter reading - holding off rather than heating blind"
    if delivered >= DEVICE_DAILY_KWH:
        return False, "load already had its %.1f kWh today" % DEVICE_DAILY_KWH
    return True, "%.1f of %.1f kWh so far - topping up from roof and grid" % (
        delivered, DEVICE_DAILY_KWH)


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
        key = (fields.get("socket_on"), fields.get("wanted"), fields.get("reason"))
        now = time.monotonic()
        if key == self._key and now - self._written_at < self._interval:
            return
        self._key = key
        self._written_at = now
        self._logbook.sample(**fields)

        if now - self._pruned_at >= 86400:
            self._pruned_at = now
            self._logbook.prune()


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


async def main():
    global LOGBOOK

    if BOOST_STRATEGY not in ("forecast", "rain"):
        raise SystemExit("BOOST_STRATEGY must be 'forecast' or 'rain', not %r" % BOOST_STRATEGY)

    forecast = solar_forecast.from_config()
    if forecast is None:
        raise SystemExit("No site configured - set LATITUDE and LONGITUDE in .env.")

    LOGBOOK = history.Logbook(HISTORY_DB, HISTORY_RETENTION_DAYS)
    LOGBOOK.prune()
    recorder = Recorder(LOGBOOK, HISTORY_SAMPLE_INTERVAL)

    server = None
    if DASHBOARD_PORT:
        try:
            server = dashboard.serve(DASHBOARD_HOST, DASHBOARD_PORT, LOGBOOK, settings, on_log=log)
        except OSError as exc:
            log("dashboard could not take port %d: %s" % (DASHBOARD_PORT, exc), "error")

    plug = Plug(client(), PLUG_IP)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    log("controlling %s only, %.1f kWh/day budget" % (PLUG_IP, DEVICE_DAILY_KWH))
    log("cheap grid %s-%s (strategy '%s'), top-up from %s until the budget is met" % (
        NIGHT_START.strftime("%H:%M"), NIGHT_END.strftime("%H:%M"), BOOST_STRATEGY,
        SOLAR_START.strftime("%H:%M")))
    if BOOST_STRATEGY == "forecast":
        log("house takes %.1f kWh of the day's roof before the load sees any"
            % HOUSE_DAYTIME_KWH)

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

    last_reason = None
    while not stop.is_set():
        now = datetime.now()

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

        if night:
            wanted, reason = decide_night(outlook, wet, delivered)
        elif solar:
            # The day window needs no forecast at all - only the meter.
            wanted, reason = decide_solar(delivered)
        else:
            wanted, reason = False, "outside both windows"

        if reason != last_reason:
            log(reason, category="decision")
            last_reason = reason
        await plug.set_state(wanted)

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
            free_kwh=free_kwh,
            target_kwh=target_kwh,
            wanted=wanted,
            socket_on=plug.state,
            reason=reason,
        )

        try:
            await asyncio.wait_for(stop.wait(), timeout=CHECK_INTERVAL)
        except asyncio.TimeoutError:
            pass

    log("stopped")
    if server is not None:
        server.shutdown()
    LOGBOOK.close()


if __name__ == "__main__":
    asyncio.run(main())
