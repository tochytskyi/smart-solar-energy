"""Tomorrow's sun, from Open-Meteo - how much the array will make, and whether
the middle of the day is a washout.

Open-Meteo needs no API key and no account. It exposes global_tilted_irradiance
for an arbitrary panel tilt and bearing, which is exactly the number a yield
estimate wants, so the array geometry is handed straight to the API rather than
being approximated here.

Run standalone for today's outlook:
    python solar_forecast.py
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from tapo_client import config

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

HOURLY_FIELDS = (
    "global_tilted_irradiance",
    "cloud_cover",
    "precipitation",
    "precipitation_probability",
)


class ForecastError(RuntimeError):
    """Open-Meteo was unreachable or returned something unusable."""


def compass_to_open_meteo(bearing):
    """Compass bearing (0 = north, 90 = east) -> Open-Meteo's angle.

    Open-Meteo measures panel azimuth from due south: 0 south, -90 east,
    +90 west. A 240 deg compass bearing (west-south-west) becomes +60.
    """
    return ((float(bearing) - 180.0 + 180.0) % 360.0) - 180.0


def parse_hhmm(value):
    """'07:00' -> datetime.time."""
    hour, _, minute = value.strip().partition(":")
    return datetime.min.replace(hour=int(hour), minute=int(minute or 0)).time()


def in_window(moment, start, end):
    """Is this time of day inside [start, end)? Windows may wrap past midnight."""
    if start <= end:
        return start <= moment < end
    return moment >= start or moment < end


def window_hours(start, end):
    """How long a time-of-day window lasts, in hours.

    Windows may wrap past midnight, and a start equal to its end means the
    whole day rather than nothing - there is no zero-length window here.
    """
    minutes = (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
    return (minutes % 1440 or 1440) / 60.0


class SolarForecast:
    """Hourly outlook for one roof, cached so the loop can ask as often as it likes."""

    def __init__(self, latitude, longitude, tilt, azimuth, kwp,
                 performance_ratio=0.8, inverter_kw=None, timezone="auto",
                 refresh_seconds=3600):
        self._query = {
            "latitude": latitude,
            "longitude": longitude,
            "tilt": tilt,
            "azimuth": compass_to_open_meteo(azimuth),
            "hourly": ",".join(HOURLY_FIELDS),
            "timezone": timezone,
            "forecast_days": 2,
        }
        self._kwp = float(kwp)
        self._performance_ratio = float(performance_ratio)
        self._inverter_kw = float(inverter_kw) if inverter_kw else None
        self._refresh_seconds = refresh_seconds
        self._hours = None
        self._fetched_at = None
        self.clock_skew = None      # see _check_clock below

    # -- fetching -----------------------------------------------------------

    def _fetch(self):
        url = FORECAST_URL + "?" + urllib.parse.urlencode(self._query)
        try:
            with urllib.request.urlopen(url, timeout=15) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            raise ForecastError("HTTP %s from Open-Meteo: %s" % (exc.code, detail)) from exc
        except (OSError, ValueError) as exc:
            raise ForecastError("Open-Meteo unreachable: %s" % exc) from exc

        self._check_clock(body)

        hourly = body.get("hourly") or {}
        times = hourly.get("time")
        if not times:
            raise ForecastError("Open-Meteo returned no hourly data")

        def column(name):
            values = hourly.get(name) or []
            return list(values) + [None] * (len(times) - len(values))

        gti = column("global_tilted_irradiance")
        cloud = column("cloud_cover")
        rain = column("precipitation")
        rain_probability = column("precipitation_probability")

        # Times come back already in the requested timezone, so they compare
        # directly against datetime.now() with no tz arithmetic anywhere.
        self._hours = [
            {
                "time": datetime.fromisoformat(times[i]),
                "gti": gti[i] or 0.0,
                "cloud_cover": cloud[i],
                "precipitation": rain[i] or 0.0,
                "precipitation_probability": rain_probability[i] or 0,
                "kw": self._power_kw(gti[i] or 0.0),
            }
            for i in range(len(times))
        ]
        self._fetched_at = datetime.now()
        return self._hours

    def _check_clock(self, body):
        """Warn if this machine's clock is in a different zone than the forecast.

        Every time in this program is naive local time - the night window, the
        solar window and the hourly rows all compare directly. That only holds
        while the host's timezone matches the one Open-Meteo answered in, so a
        container left on UTC would quietly shift every window by hours.
        """
        remote = body.get("utc_offset_seconds")
        if remote is None:
            return
        local = datetime.now().astimezone().utcoffset().total_seconds()
        skew = remote - local
        self.clock_skew = None if abs(skew) < 60 else {
            "hours": skew / 3600.0,
            "forecast_timezone": body.get("timezone"),
        }

    @property
    def fetched_at(self):
        """When the cached rows were last pulled from Open-Meteo, or None.

        The curve only changes when this does, which is what lets the watcher
        file a day's forecast once an hour instead of once a minute.
        """
        return self._fetched_at

    def hours(self):
        """Cached hourly rows, refetched once the cache goes stale."""
        stale = (
            self._fetched_at is None
            or (datetime.now() - self._fetched_at).total_seconds() >= self._refresh_seconds
        )
        if stale:
            return self._fetch()
        return self._hours

    # -- modelling ----------------------------------------------------------

    def _power_kw(self, gti):
        """Array output for a given plane-of-array irradiance, in kW.

        A kWp rating is defined at 1000 W/m2, so output scales straight off the
        tilted irradiance. The performance ratio is the usual lump sum for cell
        temperature, inverter losses, soiling and wiring; the inverter cap is
        applied last because it clips whatever the panels manage.
        """
        kw = self._kwp * (gti / 1000.0) * self._performance_ratio
        if self._inverter_kw is not None:
            kw = min(kw, self._inverter_kw)
        return kw

    def outlook(self, day, start, end):
        """Summarise one day's window: yield in kWh, plus how wet it looks.

        Open-Meteo stamps an hourly row with the END of the hour it describes -
        the 14:00 row is the mean irradiance over 13:00-14:00, not the reading
        at 14:00. So a row belongs to the window when the hour it covers STARTS
        inside it, which is one hour before its own timestamp. Selecting on the
        raw timestamp instead shifts the whole window an hour early, which on a
        west-facing roof trades the best hour of the afternoon for a dim
        morning one.
        """
        rows = [
            hour for hour in self.hours()
            if covers(hour["time"]).date() == day
            and in_window(covers(hour["time"]).time(), start, end)
        ]
        if not rows:
            raise ForecastError("no forecast rows for %s %s-%s" % (day, start, end))

        # Each row is one hour, so kW and kWh are the same number here.
        return {
            "day": day,
            "hours": rows,
            "pv_kwh": sum(row["kw"] for row in rows),
            "peak_kw": max(row["kw"] for row in rows),
            "cloud_cover": _mean(row["cloud_cover"] for row in rows),
            "rain_mm": sum(row["precipitation"] for row in rows),
        }

    def wet_hour_fraction(self, outlook, min_mm, min_probability):
        """Share of the window's hours that look rainy, 0.0 to 1.0."""
        rows = outlook["hours"]
        wet = sum(
            1 for row in rows
            if row["precipitation"] >= min_mm
            or row["precipitation_probability"] >= min_probability
        )
        return wet / float(len(rows))


def covers(stamp):
    """The start of the hour an Open-Meteo row describes.

    Its own timestamp is the end of that hour - see SolarForecast.outlook.
    """
    return stamp - timedelta(hours=1)


def hour_row(outlook, moment):
    """The outlook's row for the hour `moment` falls in, or None.

    The decision inside the solar window needs what the roof is expected to
    make right now, and it has to be the same curve the night decision added
    up and the page draws - so it is read back out of the outlook rather than
    fetched again.
    """
    for row in outlook["hours"]:
        if covers(row["time"]) <= moment < row["time"]:
            return row
    return None


def _mean(values):
    numbers = [value for value in values if value is not None]
    return sum(numbers) / float(len(numbers)) if numbers else None


def target_day(now, night_start, night_end):
    """Which day's sunshine the current night window is deciding about.

    For a 00:00-07:00 window that is simply today. For one that wraps past
    midnight (23:00-07:00), the hours before midnight are already betting on
    tomorrow's sun.
    """
    if night_start <= night_end:
        return now.date()
    return now.date() + timedelta(days=1) if now.time() >= night_start else now.date()


def from_config():
    """Build a forecast from .env, or None when no location is configured."""
    latitude = config("LATITUDE", required=False)
    longitude = config("LONGITUDE", required=False)
    if not latitude or not longitude:
        return None
    return SolarForecast(
        latitude=float(latitude),
        longitude=float(longitude),
        tilt=float(config("PV_TILT", "30", required=False)),
        azimuth=float(config("PV_AZIMUTH", "180", required=False)),
        kwp=float(config("PV_KWP", "10", required=False)),
        performance_ratio=float(config("PV_PERFORMANCE_RATIO", "0.8", required=False)),
        inverter_kw=float(config("INVERTER_KW", "0", required=False)) or None,
        timezone=config("TZ", "auto", required=False),
        refresh_seconds=int(config("FORECAST_REFRESH", "3600", required=False)),
    )


if __name__ == "__main__":
    import sys

    forecast = from_config()
    if forecast is None:
        sys.exit("LATITUDE / LONGITUDE are not set - see .env.example.")

    start = parse_hhmm(config("SOLAR_START", "10:00", required=False))
    end = parse_hhmm(config("SOLAR_END", "18:00", required=False))
    try:
        today = forecast.outlook(datetime.now().date(), start, end)
    except ForecastError as exc:
        sys.exit("Open-Meteo: %s" % exc)

    print("%s window %s-%s" % (today["day"], start.strftime("%H:%M"), end.strftime("%H:%M")))
    for row in today["hours"]:
        print("  %s  %4.0f W/m2  %5.2f kW  cloud %3s%%  rain %4.1f mm (%s%%)" % (
            row["time"].strftime("%H:%M"), row["gti"], row["kw"],
            row["cloud_cover"], row["precipitation"], row["precipitation_probability"]))
    print("  -> %.1f kWh, peak %.1f kW, mean cloud %.0f%%, %.1f mm rain" % (
        today["pv_kwh"], today["peak_kw"], today["cloud_cover"] or 0, today["rain_mm"]))
