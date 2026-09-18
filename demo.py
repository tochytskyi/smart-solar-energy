"""Serve the dashboard against invented data, for working on the page itself.

Waiting for a night to pass to see whether a chart change worked is no way to
build a page, so this fills a throwaway logbook with three days of plausible
behaviour - a bright day and a dull one, boosts on both tariffs, a lost
forecast, a failed switch, a stretch with the watcher paused from the page -
and serves it exactly as main.py would.

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

SETTINGS = {
    "plug_ip": "192.168.0.239", "strategy": "forecast", "check_interval": 60,
    "sample_interval": 300, "monitor_daytime": True,
    "night_start": "00:00", "night_end": "07:00",
    "solar_start": "10:00", "solar_end": "18:00",
    "device_daily_kwh": 6, "device_power_kw": 2.0,
    "house_daytime_kwh": 8,
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
        # Thickening cloud through the afternoon and one wet hour, so the
        # hourly panel has more to show than a clean arc.
        kw *= 1.0 if index < 4 else 0.55 if index == 5 else 0.8
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
    """(free kWh, grid kWh, wet fraction) - main.py's arithmetic, on this curve.

    The day's total less the house, exactly as `free_solar_kwh` does it, so
    the page's hourly panel and its Verdict card agree here for the same
    reason they agree on the Pi.
    """
    free = min(SETTINGS["device_daily_kwh"],
               max(0.0, outlook["pv_kwh"] - SETTINGS["house_daytime_kwh"]))
    target = min(SETTINGS["device_daily_kwh"],
                 max(0.0, SETTINGS["device_daily_kwh"] - free))
    wet = sum(1 for row in outlook["hours"]
              if row["precipitation"] >= 0.1 or row["precipitation_probability"] >= 60)
    return round(free, 2), round(target, 2), wet / float(len(outlook["hours"]))


# The stretch the demo has the watcher paused for, in hours before now.
# Anchored on now rather than on the clock so it is always inside the page's
# default 24-hour view: the point of it is to be looked at.
PAUSED_FROM, PAUSED_TO = 9, 3


def fill(book):
    """Three days of five-minute samples, plus the log lines that go with them."""
    delivered, day_seen = 0.0, None
    outlook = ahead = None
    relay = False               # what the socket is actually doing
    was_enabled = True
    moment = time.time() - DAYS * 86400
    paused_from = time.time() - PAUSED_FROM * 3600
    paused_to = time.time() - PAUSED_TO * 3600

    while moment < time.time():
        local = datetime.fromtimestamp(moment)
        hour = local.hour + local.minute / 60.0
        if day_seen != local.date():
            day_seen, delivered = local.date(), 0.0
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
        free, target, wet = plan(judged)
        # Only for the hourly log line below - no decision reads this.
        row = judged["hours"][int(hour) - SOLAR_FROM] if solar else None

        if night:
            wanted = delivered < target
            reason = ("%.1f kWh sun, house takes %.1f - %.1f kWh reaches the load free"
                      % (judged["pv_kwh"], SETTINGS["house_daytime_kwh"], free))
            reason += (" - buying %.1f kWh of it from the grid" % target if target
                       else " - covers %.1f kWh, waiting for sun" % SETTINGS["device_daily_kwh"])
        elif solar:
            wanted = delivered < SETTINGS["device_daily_kwh"]
            reason = ("%.1f of %.1f kWh so far - topping up from roof and grid"
                      % (delivered, SETTINGS["device_daily_kwh"]))
        else:
            wanted, reason = False, "outside both windows"

        if delivered >= SETTINGS["device_daily_kwh"]:
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
        if relay:
            delivered += SETTINGS["device_power_kw"] * STEP / 3600.0

        book.sample(
            at=moment, phase="night" if night else "solar" if solar else "idle",
            strategy="forecast",
            pv_forecast_kwh=judged["pv_kwh"], peak_kw=judged["peak_kw"],
            cloud_cover=round(judged["cloud_cover"]), rain_mm=judged["rain_mm"],
            wet_fraction=wet,
            plug_power_w=round(SETTINGS["device_power_kw"] * 1000 + random.random() * 120)
                         if relay else 0,
            delivered_kwh=round(delivered, 2),
            free_kwh=free, target_kwh=target,
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

    print("invented history in %s" % path)
    print("dashboard on http://127.0.0.1:%d  (Ctrl-C to stop)" % port)
    dashboard.serve("127.0.0.1", port, book, lambda: dict(SETTINGS))
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
