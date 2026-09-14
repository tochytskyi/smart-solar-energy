"""Read the plug's own energy meter, hour by hour.

The watcher records what it decided; this reads what the socket actually did,
straight off the P110. That is the only way to settle "the page says it bought
2 kWh but the meter says 5" - the logbook and the meter are different
witnesses, and when they disagree the meter is the one holding the evidence.

Usage:
    python plug_history.py                 # PLUG_B_IP, today and yesterday
    python plug_history.py 192.168.0.239   # a specific plug
"""

import asyncio
import sys
from datetime import date, datetime, timedelta

import main as watcher
from tapo_client import client, config, is_online


def buckets(result):
    """Hourly Wh out of whatever shape the library hands back.

    The Rust binding has changed the field name across versions, so pick the
    first list that looks like readings rather than trusting one spelling.
    """
    data = result.to_dict()
    for key in ("data", "energy_data", "values"):
        if isinstance(data.get(key), list):
            return data[key], data
    return [], data


def window_label(hour):
    """Which of the watcher's windows this hour falls in."""
    at = datetime.min.time().replace(hour=hour)
    if watcher.in_window(at, watcher.NIGHT_START, watcher.NIGHT_END):
        return "night"
    if watcher.in_window(at, watcher.SOLAR_START, watcher.SOLAR_END):
        return "solar"
    return ""


async def show_day(device, day):
    from tapo.requests import EnergyDataInterval

    print("=" * 62)
    print("%s - hourly, straight off the meter" % day)
    print("=" * 62)
    try:
        result = await device.get_energy_data(EnergyDataInterval.Hourly, day)
    except Exception as exc:
        print("  could not read: %s" % exc)
        return

    rows, raw = buckets(result)
    if not rows:
        print("  no hourly data returned. Raw response:")
        print("  %r" % (raw,))
        return

    totals = {"night": 0.0, "solar": 0.0, "": 0.0}
    for hour, wh in enumerate(rows[:24]):
        wh = float(wh or 0)
        where = window_label(hour)
        totals[where] += wh
        if wh <= 0:
            continue
        print("  %02d:00-%02d:00 %8.0f Wh  %5.2f kW avg  %s" % (
            hour, (hour + 1) % 24, wh, wh / 1000.0, where))

    print("  %s" % ("-" * 58))
    print("  cheap grid %s-%s : %6.2f kWh" % (
        watcher.NIGHT_START.strftime("%H:%M"), watcher.NIGHT_END.strftime("%H:%M"),
        totals["night"] / 1000.0))
    print("  free solar %s-%s : %6.2f kWh" % (
        watcher.SOLAR_START.strftime("%H:%M"), watcher.SOLAR_END.strftime("%H:%M"),
        totals["solar"] / 1000.0))
    print("  outside both windows: %6.2f kWh   <- nothing the watcher asked for"
          % (totals[""] / 1000.0))
    print("  day total           : %6.2f kWh   (budget %.1f kWh)\n" % (
        sum(totals.values()) / 1000.0, watcher.DEVICE_DAILY_KWH))


async def main():
    ip = sys.argv[1] if len(sys.argv) > 1 else config("PLUG_B_IP")
    if not is_online(ip):
        raise SystemExit(
            "%s is not answering on the local API port.\n"
            "Run this where the plug is reachable - on the Pi, not over a VPN." % ip)

    device = await client().p110(ip)

    info = (await device.get_device_info()).to_dict()
    usage = (await device.get_energy_usage()).to_dict()
    print()
    print("=" * 62)
    print("Plug %s - right now" % ip)
    print("=" * 62)
    print("  relay        : %s" % ("ON" if info.get("device_on") else "OFF"))
    print("  drawing      : %.0f W" % ((await device.get_current_power()).to_dict()
                                       .get("current_power") or 0))
    print("  today        : %.3f kWh" % ((usage.get("today_energy") or 0) / 1000.0))
    print("  this month   : %.3f kWh" % ((usage.get("month_energy") or 0) / 1000.0))
    print("  running today: %s minutes\n" % usage.get("today_runtime"))

    today = date.today()
    await show_day(device, today)
    await show_day(device, today - timedelta(days=1))


asyncio.run(main())
