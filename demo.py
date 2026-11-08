"""Serve the dashboard against invented data, for working on the page itself.

Waiting for a night to pass to see whether a chart change worked is no way to
build a page, so this fills a throwaway logbook with three days of plausible
behaviour - a bright day and a dull one, boosts on both tariffs, a lost
forecast, a failed switch, a stretch with the watcher paused from the page,
scheduled outages under blackout mode - and serves it exactly as main.py would.

    python demo.py                  # http://127.0.0.1:8080, Ctrl-C to stop
    python demo.py 9000             # another port

Nothing here touches the real history or any hardware. The numbers are fake;
only the shapes are meant to be realistic.
"""

import math
import random
import sys
import tempfile
import time
from datetime import date, datetime, time as clock, timedelta
from pathlib import Path

import dashboard
import history
import overrides

SETTINGS = {
    "plug_ip": "192.168.0.239", "strategy": "forecast", "check_interval": 60,
    "sample_interval": 300, "monitor_daytime": True,
    "night_start": "00:00", "night_end": "07:00",
    "solar_start": "10:00", "solar_end": "18:00",
    "device_daily_kwh": 6, "device_daily_hours": 3, "device_power_kw": 2.0,
    "house_daytime_kwh": 8,
    "blackout_mode": True, "blackout_sentinel": "192.168.0.50:80",
    "blackout_inverter": True, "inverter_max_age": 300, "inverter_settle": 600,
    "blackout_solar_share": 0.7,
}
STEP = 300          # the default HISTORY_SAMPLE_INTERVAL
DAYS = 3
SOLAR_FROM, SOLAR_TO = 10, 18       # the hours SETTINGS' solar window covers


def is_bright(day):
    """Alternate bright and dull days, counting back from today.

    Anchored on today rather than the calendar so the day the page opens on is
    always the interesting one - a curve that clears the house and leaves the
    load something, rather than one the night has to buy outright.
    """
    return (date.today() - day).days % 2 == 0


def is_lazy(day):
    """Yesterday is the day nobody drew any hot water.

    The load's own thermostat cuts out, so the socket is closed but the meter
    barely moves - the kWh budget is never going to fill, and the hours
    allowance is the only thing that ends the run. That is the whole reason
    the allowance exists, so the page has to have a day showing it.
    """
    return (date.today() - day).days == 1


# What the load actually draws on such a day, as a fraction of its rating.
LAZY_DUTY = 0.45


def curve(day, bright):
    """One day's outlook, shaped the way solar_forecast returns it.

    Rows carry the END of the hour they cover, exactly as Open-Meteo stamps
    them - the page labels each period from that, and a fixture that cheated
    here would make the whole panel read an hour early.
    """
    rows = []
    for index in range(SOLAR_TO - SOLAR_FROM):
        middle = SOLAR_FROM + index + 0.5
        kw = max(0.0, math.sin((middle - 6.5) / 11.0 * math.pi)) * (6.4 if bright else 2.1)
        # Morning haze, thickening cloud through the afternoon and one wet
        # hour, so the hourly panel has more to show than a clean arc - and
        # blackout mode's day window has a weak first hour to sit out.
        kw *= 0.3 if index == 0 else 1.0 if index < 4 else 0.55 if index == 5 else 0.8
        rows.append({
            "time": datetime.combine(day, clock(SOLAR_FROM)) + timedelta(hours=index + 1),
            "kw": round(kw, 2),
            "gti": round(kw / 6.4 * 900),
            "cloud_cover": min(95, (10 if bright else 55) + index * (12 if bright else 6)),
            "precipitation": (0.0 if bright else 0.6) if index == 5 else 0.0,
            "precipitation_probability": min(95, (5 if bright else 45) + index * 9),
        })
    return {
        "day": day,
        "hours": rows,
        "pv_kwh": round(sum(row["kw"] for row in rows), 2),
        "peak_kw": max(row["kw"] for row in rows),
        "cloud_cover": sum(row["cloud_cover"] for row in rows) / float(len(rows)),
        "rain_mm": round(sum(row["precipitation"] for row in rows), 2),
    }


def plan(outlook):
    """(free kWh, grid kWh, wet fraction, strong hours) - main.py's arithmetic.

    The day's total less the house, exactly as `free_solar_kwh` does it, so
    the page's hourly panel and its day-ahead card agree here for the same
    reason they agree on the Pi. Blackout mode is on in SETTINGS, so the night
    buys all or nothing, as `night_target_kwh` does it there.
    """
    daily, power = SETTINGS["device_daily_kwh"], SETTINGS["device_power_kw"]
    free = min(daily, max(0.0, outlook["pv_kwh"] - SETTINGS["house_daytime_kwh"]))
    strong = sum(1 for row in outlook["hours"] if row["kw"] >= power)
    if SETTINGS["blackout_mode"]:
        needed = daily * SETTINGS["blackout_solar_share"]
        target = 0.0 if free >= needed and strong * power >= needed else daily
    else:
        target = min(daily, max(0.0, daily - free))
    wet = sum(1 for row in outlook["hours"]
              if row["precipitation"] >= 0.1 or row["precipitation_probability"] >= 60)
    return round(free, 2), round(target, 2), wet / float(len(outlook["hours"])), strong


# The stretch the demo has the watcher paused for, in hours before now.
# Anchored on now rather than on the clock so it is always inside the page's
# default 24-hour view: the point of it is to be looked at.
PAUSED_FROM, PAUSED_TO = 9, 3
# And a stretch with the inverter switched off from the page, so the sentinel
# alone decides the grid and the page has to say so.
INVERTER_OFF_FROM, INVERTER_OFF_TO = 14, 12
# And one with the sentinel left out instead, so the inverter alone decides -
# long enough to take in a stale cloud, when nothing can say the grid is there.
SENTINEL_OFF_FROM, SENTINEL_OFF_TO = 30, 20


def grid_is_up(local):
    """A scheduled outage every night, and one afternoon that ran over.

    The night one sits inside the cheap window so the page shows blackout mode
    doing its job: the sum says buy, the sentinel is silent, the socket stays
    off. The afternoon one shows the day window riding straight through it.
    """
    hour = local.hour + local.minute / 60.0
    if 1.5 <= hour < 4.0:
        return False
    return not ((date.today() - local.date()).days == 1 and 14.0 <= hour < 16.0)


def cloud_is_stale(local):
    """The first half hour of every night outage, the router went down with the grid.

    The logger cannot upload, so the inverter's last reading ages past the
    limit and the sentinel decides - the page has to show that handover.
    """
    hour = local.hour + local.minute / 60.0
    return 1.5 <= hour < 2.0


def weak_grid(local):
    """Half an hour after the night outage when the mains sags and the inverter flaps.

    The shape this site's own history shows: the weakest phase dips under
    about 181 V, the inverter reports 0 Hz and refuses the grid, takes it back
    a few minutes later, refuses it again. The night has to sit it out until
    the inverter has held the grid for the whole settle time.
    """
    hour = local.hour + local.minute / 60.0
    return 4.0 <= hour < 4.5


def fill(book):
    """Three days of five-minute samples, plus the log lines that go with them."""
    delivered, ran_hours, day_seen = 0.0, 0.0, None
    outlook = ahead = None
    relay = False               # what the socket is actually doing
    was_enabled = True
    was_up = True
    was_stale = False
    moment = time.time() - DAYS * 86400
    held_since = moment - 3600     # the inverter had the grid before the record starts
    paused_from = time.time() - PAUSED_FROM * 3600
    paused_to = time.time() - PAUSED_TO * 3600
    inverter_off_from = time.time() - INVERTER_OFF_FROM * 3600
    inverter_off_to = time.time() - INVERTER_OFF_TO * 3600
    was_asked = True
    sentinel_off_from = time.time() - SENTINEL_OFF_FROM * 3600
    sentinel_off_to = time.time() - SENTINEL_OFF_TO * 3600
    was_knocked = True

    while moment < time.time():
        local = datetime.fromtimestamp(moment)
        hour = local.hour + local.minute / 60.0
        if day_seen != local.date():
            day_seen, delivered, ran_hours = local.date(), 0.0, 0.0
            outlook = curve(local.date(), is_bright(local.date()))
            ahead = curve(local.date() + timedelta(days=1),
                          is_bright(local.date() + timedelta(days=1)))
            book.event("00:00 - a new day, the meter starts again", "info", "decision",
                       at=moment)

        night = hour < 7
        solar = SOLAR_FROM <= hour < SOLAR_TO
        # The day the watcher is judging: today while today's roof still has
        # hours left, tomorrow from the moment it runs out - so the evening's
        # numbers preview the night that is coming, not the day just spent.
        judged = outlook if hour < SOLAR_TO else ahead
        free, target, wet, strong = plan(judged)
        # This hour's forecast: blackout mode's day window runs only on it.
        row = judged["hours"][int(hour) - SOLAR_FROM] if solar else None

        mains = grid_is_up(local)
        asked = not (inverter_off_from <= moment < inverter_off_to)
        if asked != was_asked:
            book.event("inverter switched back ON from the page - it decides the grid again,"
                       " after holding it for 10 min" if asked
                       else "inverter switched OFF from the page - only the sentinel decides"
                       " the grid", "info" if asked else "warn", "system", at=moment)
            was_asked = asked
        knocked = not (sentinel_off_from <= moment < sentinel_off_to)
        if knocked != was_knocked:
            book.event("sentinel switched back ON from the page - the inverter decides the grid,"
                       " the sentinel when it cannot say" if knocked
                       else "sentinel switched OFF from the page - only the inverter decides"
                       " the grid - when it cannot say, the night will not buy",
                       "info" if knocked else "warn", "system", at=moment)
            was_knocked = knocked
        # Off from the page, the cloud is not asked at all - which, for the
        # page, looks like a stale cloud with nothing recorded from it.
        cloud_stale = cloud_is_stale(local)
        stale = cloud_stale or not asked
        sagging = mains and weak_grid(local)
        refused = sagging and (local.minute // 5) % 2 == 0
        volts = (0.0 if not mains else round(176 + random.random() * 4, 1) if refused
                 else round(186 + random.random() * 4, 1) if sagging
                 else round(205 + random.random() * 20, 1))
        # The other two phases sit a few volts above the weakest one, which
        # moves between them the way it does on a real street.
        phases = [volts if not mains else round(volts + random.random() * 9, 1) for _ in range(3)]
        phases[int(moment // 3600) % 3] = volts
        hz = 0.0 if not mains or refused else round(49.95 + random.random() * 0.1, 2)
        if hz > 0 and asked:
            held_since = held_since if held_since is not None else moment
        else:
            held_since = None
        stable = None if held_since is None else moment - held_since
        age = 360 + (moment % 1800) if stale else 20 + random.random() * 60
        source = "sentinel" if stale else "inverter"
        if stale and not knocked:
            source, grid_up = None, None
            said = "the sentinel is switched off from the page and the inverter cannot say"
        elif stale:
            grid_up = mains
            said = "sentinel 192.168.0.50:80 %s" % ("answers" if mains else "is not answering")
        elif not mains:
            grid_up, said = False, "the inverter reads no grid (0 V)"
        elif refused:
            grid_up, said = False, "the inverter rejected the grid (0 Hz, weakest phase %.0f V)" % volts
        elif stable < SETTINGS["inverter_settle"]:
            grid_up, said = False, "the inverter has held the grid only %d of %d min" % (
                stable // 60, SETTINGS["inverter_settle"] // 60)
        else:
            grid_up, said = True, "the inverter has the grid, weakest phase %.0f V, %.1f Hz" % (
                volts, hz)
        if cloud_stale != was_stale:
            book.event("inverter grid reading last reported 6 min ago - the sentinel decides"
                       " until it is back" if cloud_stale
                       else "inverter grid reading usable again - it decides the grid",
                       "warn" if cloud_stale else "info", "system", at=moment)
            was_stale = cloud_stale
        if grid_up != was_up:
            if grid_up is None:
                book.event("grid UNKNOWN - %s, the night will not buy" % said,
                           "warn", "system", at=moment)
            elif grid_up:
                book.event("grid is back - %s" % said, "info", "system", at=moment)
            else:
                book.event("grid went DOWN - %s, the night will not buy" % said,
                           "warn", "system", at=moment)
            was_up = grid_up

        if night:
            wanted = delivered < target
            reason = ("%.1f kWh sun, house takes %.1f - %.1f kWh reaches the load free"
                      % (judged["pv_kwh"], SETTINGS["house_daytime_kwh"], free))
            reason += ", %d h at %.1f kW or more" % (strong, SETTINGS["device_power_kw"])
            share = "%.0f%% of %.1f kWh" % (SETTINGS["blackout_solar_share"] * 100,
                                            SETTINGS["device_daily_kwh"])
            reason += (" - short of %s - buying %.1f kWh from the grid, none of it left to"
                       " the roof" % (share, target) if target
                       else " - carries at least %s, waiting for sun" % share)
            if wanted and grid_up is None:
                wanted = False
                reason = reason.replace(" - buying", " - would buy") + \
                    " - but %s: holding off" % said
            elif wanted and not grid_up:
                wanted = False
                reason = reason.replace(" - buying", " - would buy") + \
                    " - but %s, so the grid is down: holding off" % said
        elif solar:
            wanted = delivered < SETTINGS["device_daily_kwh"]
            reason = ("%.1f of %.1f kWh, %.1f of %.1f h so far - topping up from roof and grid"
                      % (delivered, SETTINGS["device_daily_kwh"],
                         ran_hours, SETTINGS["device_daily_hours"]))
            if wanted and row["kw"] < SETTINGS["device_power_kw"]:
                wanted = False
                reason = reason.replace(" - topping up", " - would top up") + \
                    " - but this hour is forecast at %.1f kW, under the load's %.1f: holding" \
                    " off rather than running on the battery" % (
                        row["kw"], SETTINGS["device_power_kw"])
        else:
            wanted, reason = False, "outside both windows"

        # Either measure of the one ration ends the day, whichever fills first.
        if ran_hours >= SETTINGS["device_daily_hours"]:
            wanted = False
            reason = "socket has been on %.1f of its %.1f h today" % (
                ran_hours, SETTINGS["device_daily_hours"])
        elif delivered >= SETTINGS["device_daily_kwh"]:
            wanted = False
            reason = "load already had its %.1f kWh today" % SETTINGS["device_daily_kwh"]

        # Paused, the verdict is still worked out and recorded; only the relay
        # is left alone, so it keeps whatever it was doing.
        enabled = not (paused_from <= moment < paused_to)
        if enabled:
            relay = wanted
        else:
            reason = "paused from the page - would be %s: %s" % (
                "ON" if wanted else "off", reason)
        if enabled != was_enabled:
            book.event("resumed from the page - switching the socket again" if enabled
                       else "PAUSED from the page - the socket is left exactly as it is",
                       "info" if enabled else "warn", "decision", at=moment)
            was_enabled = enabled
        duty = LAZY_DUTY if is_lazy(local.date()) else 1.0
        if relay:
            delivered += SETTINGS["device_power_kw"] * duty * STEP / 3600.0
            ran_hours += STEP / 3600.0

        book.sample(
            at=moment, phase="night" if night else "solar" if solar else "idle",
            strategy="forecast",
            pv_forecast_kwh=judged["pv_kwh"], peak_kw=judged["peak_kw"],
            cloud_cover=round(judged["cloud_cover"]), rain_mm=judged["rain_mm"],
            wet_fraction=wet,
            plug_power_w=round(SETTINGS["device_power_kw"] * duty * 1000 + random.random() * 120)
                         if relay else 0,
            delivered_kwh=round(delivered, 2),
            ran_hours=round(ran_hours, 3),
            free_kwh=free, target_kwh=target, grid_up=grid_up,
            hour_kw=row["kw"] if row else None, strong_hours=strong,
            grid_source=source,
            grid_volts=volts if asked else None, grid_hz=hz if asked else None,
            grid_l1_v=phases[0] if asked else None, grid_l2_v=phases[1] if asked else None,
            grid_l3_v=phases[2] if asked else None,
            # Knocked on whoever decides; it only sees whether there is mains at all.
            sentinel_up=mains if knocked else None,
            grid_age_s=round(age) if asked else None,
            grid_stable_s=None if stable is None else round(stable),
            inverter_enabled=asked,
            sentinel_enabled=knocked,
            # The inverter's flow word: "Static" whenever nothing crosses the meter,
            # outage or not, which is why nothing decides on it.
            grid_status=None if not asked else "Purchasing energy" if relay and grid_up
            else "Static",
            enabled=enabled, wanted=wanted, socket_on=relay, reason=reason,
        )

        if local.minute == 0 and solar and enabled:
            book.event("roof %.1f kW this hour, %.1f of %.1f kWh into the load"
                       % (row["kw"], delivered, SETTINGS["device_daily_kwh"]),
                       "info", "forecast", at=moment)
        if local.hour == 3 and local.minute == 0:
            book.event("forecast unavailable: Open-Meteo unreachable: timed out",
                       "warn", "forecast", at=moment)
            book.event("turn ON failed (attempt 1): DeviceError", "error", "plug", at=moment + 60)
        moment += STEP

    book.event("sentinel 192.168.0.50:80 is online - grid up", "info", "system")
    book.event("controlling %s only, %.1f kWh/day budget"
               % (SETTINGS["plug_ip"], SETTINGS["device_daily_kwh"]), "info", "system")

    # Today's curve and tomorrow's, as look_ahead() would have filed them -
    # today's is the one the samples above were decided from, so the page
    # cannot contradict itself.
    today = date.today()
    for day in (today, today + timedelta(days=1)):
        book.forecast(curve(day, is_bright(day)))


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    path = Path(tempfile.mkdtemp(prefix="tapo-demo-")) / "history.db"
    book = history.Logbook(path, retention_days=30)
    fill(book)

    # The Settings tab over .env.example's own values, saving into the same
    # throwaway directory. Nothing is checked and nothing restarts: a save only
    # moves the start time on, which is what the page waits for.
    examples = {entry["key"]: entry["example"]
                for section in overrides.catalogue(overrides.EXAMPLE.read_text())
                for group in section["groups"] for entry in group["keys"]}
    editor = overrides.Editor(path.parent / "settings.env", base=examples,
                              check=lambda candidate: None)

    def saved(changed):
        print("settings saved: %s - the watcher would restart now" % ", ".join(changed))
        editor.started = time.time()

    print("invented history in %s" % path)
    print("dashboard on http://127.0.0.1:%d  (Ctrl-C to stop)" % port)
    dashboard.serve("127.0.0.1", port, book, lambda: dict(SETTINGS),
                    editor=editor, on_settings=saved)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
