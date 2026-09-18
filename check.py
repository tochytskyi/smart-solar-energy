"""Report the controlled socket, the solar outlook, and the decision the
watcher would make at this moment.

Usage:
    python check.py                 # socket + forecast + decision
    python check.py 192.168.0.239   # check specific IP(s) only
"""

import asyncio
import base64
import sys
from datetime import datetime
from pathlib import Path

import history
import main as watcher
import solar_forecast
from tapo_client import client, config, is_online


def is_paused():
    """Whether the watcher has been paused from its page, or None if unknown.

    Read out of the logbook the watcher keeps, and only when there already is
    one: this is a report, and running it on a machine that has never started
    the watcher should not leave a database behind.
    """
    path = Path(watcher.HISTORY_DB)
    if not path.exists():
        return None
    book = history.Logbook(path, watcher.HISTORY_RETENTION_DAYS)
    try:
        return not book.control(history.CONTROL_ENABLED, True)
    finally:
        book.close()


def decode_nickname(value):
    if not value:
        return "(unnamed)"
    try:
        return base64.b64decode(value).decode("utf-8")
    except Exception:
        return value


async def check(api, ip):
    """(reachable?, kWh through this socket today). The meter reading is what
    both windows decide against, so it is carried out to check_decision."""
    print("=" * 60)
    print("Plug %s" % ip)
    print("=" * 60)

    if not is_online(ip):
        print("  UNREACHABLE - no ping reply. Check the IP, or grant this app the")
        print("  macOS Local Network permission (Privacy & Security).")
        return False, None

    print("  online, authenticating...")
    device = info = None
    last_exc = None
    for ctor in (api.p110, api.p100, api.p115, api.p105):   # try likely models
        try:
            device = await ctor(ip)
            info = await device.get_device_info_json()
            break
        except Exception as exc:
            last_exc = exc
            device = None
    if info is None:
        msg = str(last_exc)
        print("  CONNECT FAILED: %s: %s" % (type(last_exc).__name__, msg))
        if "403" in msg or "InvalidResponse" in msg or "Forbidden" in msg:
            print("  --> 403 handshake. This plug's firmware (1.4.x) has THIRD-PARTY")
            print("      COMPATIBILITY turned OFF, so it rejects local API logins.")
            print("      Fix in the Tapo app: open the device > Device Settings >")
            print("      Third-Party Compatibility > enable it (per plug).")
        else:
            print("  Check TAPO_EMAIL / TAPO_PASSWORD (must be the TP-Link cloud account).")
        return False, None

    print("  name       : %s" % decode_nickname(info.get("nickname")))
    print("  model      : %s (hw %s, fw %s)" % (
        info.get("model"), info.get("hw_ver"), info.get("fw_ver")))
    print("  mac        : %s" % info.get("mac"))
    print("  relay       : %s" % ("ON" if info.get("device_on") else "OFF"))
    print("  on_time    : %s s" % info.get("on_time"))
    print("  wifi rssi  : %s dBm (level %s)" % (info.get("rssi"), info.get("signal_level")))
    print("  overheated : %s" % info.get("overheated"))

    delivered = None
    if hasattr(device, "get_current_power"):
        try:
            power = await device.get_current_power()
            print("  power now  : %s W" % power.to_dict().get("current_power"))
            usage = await device.get_energy_usage()
            u = usage.to_dict()
            print("  energy     : today %s Wh, this month %s Wh" % (
                u.get("today_energy"), u.get("month_energy")))
            today_wh = u.get("today_energy")
            delivered = None if today_wh is None else float(today_wh) / 1000.0
        except Exception:
            pass  # P100 and some models have no energy monitoring

    return True, delivered


def check_forecast():
    """Print the solar outlook for the window the watcher judges, or why not.

    The hourly rows are here to be looked at, not decided on: only the day's
    total reaches the decision, and only to size the night's grid buy.
    """
    print("=" * 60)
    print("Solar outlook")
    print("=" * 60)

    forecast = solar_forecast.from_config()
    if forecast is None:
        print("  not configured - set LATITUDE and LONGITUDE in .env.")
        return None

    now = datetime.now()
    day = solar_forecast.target_day(now, watcher.SOLAR_END)
    try:
        outlook = forecast.outlook(day, watcher.SOLAR_START, watcher.SOLAR_END)
    except solar_forecast.ForecastError as exc:
        print("  FAILED: %s" % exc)
        return None

    if forecast.clock_skew:
        print("  WARNING: this host's clock is %+.1f h off the forecast's %s."
              % (-forecast.clock_skew["hours"], forecast.clock_skew["forecast_timezone"]))
        print("           Set TZ in .env to the site's zone or every window shifts.")

    print("  %s, %s-%s, %.0f kWp at %s deg tilt / %s deg bearing" % (
        day, watcher.SOLAR_START.strftime("%H:%M"), watcher.SOLAR_END.strftime("%H:%M"),
        float(config("PV_KWP", "10", required=False)),
        config("PV_TILT", "30", required=False),
        config("PV_AZIMUTH", "180", required=False)))
    for row in outlook["hours"]:
        print("    %s  %4.0f W/m2 -> %5.2f kW   cloud %3s%%   rain %4.1f mm (%s%%)" % (
            row["time"].strftime("%H:%M"), row["gti"], row["kw"],
            row["cloud_cover"], row["precipitation"], row["precipitation_probability"]))
    wet = forecast.wet_hour_fraction(outlook, watcher.RAIN_MM, watcher.RAIN_PROBABILITY)
    print("  expected   : %.1f kWh, peak %.1f kW" % (outlook["pv_kwh"], outlook["peak_kw"]))
    print("  house first: %.1f kWh of that before the load sees any"
          % watcher.HOUSE_DAYTIME_KWH)
    print("  weather    : mean cloud %.0f%%, %.1f mm rain, %.0f%% of hours wet" % (
        outlook["cloud_cover"] or 0, outlook["rain_mm"], wet * 100))
    return outlook, wet


def check_decision(forecast_result, delivered=None):
    """Print what the watcher would do with these numbers right now."""
    print("=" * 60)
    print("Decision")
    print("=" * 60)

    now = datetime.now()
    night = solar_forecast.in_window(now.time(), watcher.NIGHT_START, watcher.NIGHT_END)
    solar = solar_forecast.in_window(now.time(), watcher.SOLAR_START, watcher.SOLAR_END)
    where = "cheap grid" if night else "free solar" if solar else "neither window"
    print("  now        : %s (%s)" % (now.strftime("%H:%M"), where))
    print("  strategy   : %s" % watcher.BOOST_STRATEGY)
    if is_paused():
        print("  PAUSED     : switching is paused from the dashboard. The verdict")
        print("               below is still worked out, but the socket is left")
        print("               exactly as it is until the page resumes it.")

    outlook, wet = forecast_result if forecast_result else (None, 0.0)
    print("  delivered  : %s" % (
        "meter unreadable" if delivered is None
        else "%.2f of %.1f kWh today" % (delivered, watcher.DEVICE_DAILY_KWH)))
    if outlook is not None and watcher.BOOST_STRATEGY == "forecast":
        free = watcher.free_solar_kwh(outlook)
        target = watcher.night_target_kwh(outlook)
        print("  free today : %.1f kWh of the day's %.1f kWh is left for the load"
              " once the house has had its %.1f" % (
                  free, outlook["pv_kwh"], watcher.HOUSE_DAYTIME_KWH))
        print("  load needs : %.1f kWh -> buy %.1f kWh from the grid tonight,"
              " top up the rest from %s" % (
                  watcher.DEVICE_DAILY_KWH, target,
                  watcher.SOLAR_START.strftime("%H:%M")))

    if night:
        wanted, reason = watcher.decide_night(outlook, wet, delivered)
    elif solar:
        wanted, reason = watcher.decide_solar(delivered)
    else:
        wanted, reason = watcher.decide_night(outlook, wet, delivered)
    verdict = "SOCKET ON" if wanted else "socket off"
    if night or solar:
        print("  verdict    : %s - %s" % (verdict, reason))
    else:
        print("  verdict    : socket off - outside both windows")
        print("  tonight    : %s, on these numbers - %s" % (verdict, reason))


async def main():
    explicit_ips = sys.argv[1:]
    ips = explicit_ips or [config("PLUG_B_IP")]

    api = client()
    results = [await check(api, ip) for ip in ips]
    reachable = [ok for ok, _ in results]
    delivered = next((kwh for _, kwh in results if kwh is not None), None)

    # Named IPs mean "look at these plugs" and nothing else.
    if explicit_ips:
        print()
        print("%d/%d plug(s) reachable." % (sum(reachable), len(reachable)))
        return 0 if all(reachable) else 1

    print()
    forecast_result = await asyncio.to_thread(check_forecast)
    print()
    check_decision(forecast_result, delivered)

    print()
    print("%d/%d plug(s) reachable." % (sum(reachable), len(reachable)))
    return 0 if all(reachable) and forecast_result is not None else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
