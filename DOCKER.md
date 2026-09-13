# Running on a Raspberry Pi via GHCR

> How the control logic itself works, with flowcharts and worked
> scenarios: [docs/](docs/README.md) (Ukrainian).

## 1. Publish the image

### Option A - GitHub Actions (recommended)

Push this repo to GitHub. `.github/workflows/docker-publish.yml` builds
`linux/amd64`, `linux/arm64` and `linux/arm/v7` and pushes to
`ghcr.io/<owner>/<repo>` on every push to `main` (and on `v*` tags).

It authenticates with the built-in `GITHUB_TOKEN` - no secrets to configure.

The package starts out **private**. To pull it on the Pi without logging in,
open it at `https://github.com/users/<owner>/packages/container/<repo>/settings`
and set visibility to public. Otherwise see step 2.

### Option B - build and push from this machine

```bash
echo "$GHCR_PAT" | docker login ghcr.io -u <your-github-username> --password-stdin

docker buildx build \
  --platform linux/arm64,linux/arm/v7,linux/amd64 \
  --label org.opencontainers.image.source=https://github.com/<owner>/<repo> \
  -t ghcr.io/<owner>/tapo:latest \
  --push .
```

`GHCR_PAT` is a classic personal access token with the `write:packages` scope.

## 2. Pull and run on the Pi

```bash
mkdir -p ~/tapo/data && cd ~/tapo
# copy docker-compose.yml and .env.example over, then:
cp .env.example .env
nano .env          # fill in credentials, plug IPs, TAPO_IMAGE

# only needed if the GHCR package is private:
echo "$GHCR_PAT" | docker login ghcr.io -u <your-github-username> --password-stdin

docker compose pull
docker compose up -d
docker compose logs -f
```

`restart: unless-stopped` plus a Docker daemon enabled at boot means it comes
back after a power cut. No systemd unit needed.

To update after a new image is published:

```bash
docker compose pull && docker compose up -d
```

## One-off status check

```bash
docker compose run --rm tapo-watcher check.py                 # plug + forecast
docker compose run --rm tapo-watcher check.py 192.168.0.239   # one plug only
docker compose run --rm tapo-watcher solar_forecast.py        # today's outlook
```

## The dashboard

Every pass the watcher makes is written to `data/history.db` and served as a
live page on the Pi:

```
http://<pi-address>:8080
```

`network_mode: host` means there is no port to map - the page is simply on the
Pi's own address. It shows:

- **the state now** - socket, what it is drawing, the spare roof this hour, the
  day's forecast, and the verdict sentence with the kWh arithmetic behind it;
- **a chart** over 6 hours to 30 days, all in kW - the forecast curves, the
  spare roof left after the house, the socket's own measured draw, both windows
  shaded, and a band underneath showing exactly when the socket was on and on
  which tariff. It reaches past `now` - marked with a line - to show the hours
  still to come, never giving the future more than half the width;
- **nights** - per day, how long the socket ran on grid versus on solar, what
  the plug's meter recorded, and the best spare hour of the day;
- **the forecast, hour by hour, for today and tomorrow** - what the roof should
  make each hour, what is left once the house has taken its share, which hours
  clear `SOLAR_SURPLUS_ON_KW`, and how much of the load's budget each of those
  can carry. The total at the foot of each day is the same "free from the day"
  the verdict is built on, so the arithmetic can be checked by eye. Open-Meteo
  is asked for two days at a time, so the second one costs nothing extra;
- **the log** - the same lines `docker compose logs` shows, kept for
  `HISTORY_RETENTION_DAYS`, filterable by level and text.

It refreshes itself once a minute - the same cadence as `CHECK_INTERVAL`, so
the page is never more than one decision behind - and reads well on a phone. A
tab you switch back to updates immediately instead of waiting out the minute.

```bash
# the raw numbers, for a spreadsheet
curl -o history.csv "http://<pi-address>:8080/api/export.csv?hours=168"
```

`python demo.py` on any machine with this repo checked out fills a throwaway
database with three days of invented behaviour and serves the same page on
`http://127.0.0.1:8080`, which is the quick way to see what the dashboard looks
like before a night has actually passed.

The page is **read-only and unauthenticated** - it can show the numbers, never
change them, and it carries no credentials. Keep it on the LAN; do not forward
the port. `DASHBOARD_HOST=127.0.0.1` restricts it to the Pi itself (reachable
then over an SSH tunnel), and `DASHBOARD_PORT=0` switches it off entirely.

### Where the history lives

`./data` on the host is bind-mounted to `/app/data`, so pulling a new image or
running `docker compose down` keeps the record. The container runs as uid 1000,
which is the Pi's first user - create the directory yourself
(`mkdir -p ~/tapo/data`) rather than letting Docker create it as root, or the
watcher cannot write to it. It says so in the log if that happens, and carries
on switching the socket regardless.

Nothing is lost to a restart, a reboot or a `docker compose pull` - only to
age. `HISTORY_RETENTION_DAYS` (30 by default) deletes rows older than that,
once a day; set it to `0` to keep everything forever.

Measured at the default 5-minute sample interval:

| kept | file |
|---|---|
| 30 days | ~4 MB |
| 1 year | ~47 MB |
| forever (`0`) | ~47 MB a year, growing |

Even "forever" is small against an 8 GB card, so the 30-day default is about
keeping the page quick to query, not about space. The container's own stdout
log (`docker compose logs`) is separate and rotates at 3 x 10 MB - the same
lines are in the database, which is what the dashboard reads.

## What it actually does

One plug is controlled: `PLUG_B_IP`. The load on it owes itself
`DEVICE_DAILY_KWH` every day, and there are two chances to give it that. The
plug's own meter keeps one running total, so **both windows share one daily
budget** - whatever the night buys, the afternoon does not repeat.

| Window | Source | Runs when |
|---|---|---|
| `NIGHT_START`-`NIGHT_END` (00:00-07:00) | cheap grid | today's sun will not cover the load |
| `SOLAR_START`-`SOLAR_END` (10:00-18:00) | free solar | the roof is ahead of the house |
| anything else | - | never |

**Two numbers decide all of it**: the plug's own meter, and the Open-Meteo
forecast for this roof. Nothing is read from the inverter - no cloud account,
no battery charge, no API key anywhere in the loop.

### The spare roof

Both windows are built on the same per-hour number:

```
spare(hour) = forecast kW for that hour - HOUSE_BASELINE_KW

HOUSE_BASELINE_KW = HOUSE_DAYTIME_KWH / length of SOLAR_START-SOLAR_END
```

8 kWh of house across a 10:00-18:00 window is 1.0 kW the roof owes the house
before the load sees any of it. An hour making 3 kW therefore has 2 kW spare;
an hour making 0.4 kW has none at all, and goes negative.

### The day: take the surplus

Inside the solar window the socket follows that spare figure, with hysteresis:

| spare this hour | Socket |
|---|---|
| at or above `SOLAR_SURPLUS_ON_KW` (2.0) | **on** - the roof carries the load outright, this is free |
| between | held wherever it was |
| at or below `SOLAR_SURPLUS_OFF_KW` (1.0) | **off** - the house needs the roof |

Set `SOLAR_SURPLUS_ON_KW` to roughly what the load draws (`DEVICE_POWER_KW`):
that is the point at which switching it on costs the rest of the house nothing.
The band below it is what stops an hour grazing the threshold from making the
relay chatter.

The forecast is an hourly curve, not an instantaneous reading, which is exactly
why it works here - it is already smooth, so a passing cloud or a kettle never
reaches the relay at all.

### The night: buy only the shortfall

First, how much the load gets for nothing today - hour by hour, counting only
the hours the daytime rule above will actually switch on for, and only as much
of each as the load can swallow:

```
free = sum over the solar window of
         min(DEVICE_POWER_KW, spare(hour))     for hours where spare >= SOLAR_SURPLUS_ON_KW
```

Then the grid is asked for the remainder, and nothing more:

```
buy tonight = clamp(DEVICE_DAILY_KWH - free, 0 .. DEVICE_DAILY_KWH)
```

`buy tonight == 0` means the day covers it outright and the socket stays off all
night. If the sun will hand over 4 of the 6 kWh, the grid is asked for 2, not 6 -
the socket switches off mid-window once the plug's meter says that share is in.

Counting the hours rather than the daily total is the point. A washed-out day
dribbling 12 kWh out at 1.5 kW never clears the threshold and hands the load
**nothing**, while the same 12 kWh in a sharp arc covers it twice over. A single
daily figure cannot tell those apart, and the night would buy the wrong amount
on both. It also keeps the two windows honest with each other: the night is
predicting exactly what the afternoon is going to do, off the same curve.

### Deciding the forecast: `BOOST_STRATEGY`

`forecast` (default) is the arithmetic above. `rain` is the simpler rule - buy
grid when at least `RAIN_HOURS_FRACTION` of the solar window is forecast wet (an
hour counts as wet at `RAIN_MM` of rain or `RAIN_PROBABILITY` percent chance).
`rain` ignores how much energy the day will actually make, which is why it is
not the default: a bright overcast day still makes 12 kWh and is bone dry.

The yield comes from [Open-Meteo](https://open-meteo.com) - no API key, no
account. It serves `global_tilted_irradiance` for an arbitrary panel tilt and
bearing, so the array geometry goes straight to the API instead of being
approximated locally. Output is `PV_KWP x GTI/1000 x PV_PERFORMANCE_RATIO`,
capped at `INVERTER_KW`, summed hour by hour.

`PV_AZIMUTH` is a plain compass bearing (0 N, 90 E, 180 S, 270 W) and is
converted internally to the south-referenced angle Open-Meteo wants.

Open-Meteo stamps each hourly row with the **end** of the hour it describes, so
the 14:00 row is the mean over 13:00-14:00. The window selects rows by the hour
they cover, not by their own stamp - otherwise a 10:00-18:00 window would
quietly integrate 09:00-17:00 and hand the best afternoon hour back.

### This is potential, not what the inverter will report

On a zero-export system the two diverge, and badly. Once the house and the
battery are satisfied the array is throttled, so the inverter's daily figure
flattens out - 17 to 19 kWh here, whatever the sky did - while the roof could
have made forty. Over thirteen days the model said 416 kWh and the inverter
logged 202.

That is not an error in the forecast, and it is the right number for this
decision: the curtailed energy is genuinely available the moment something asks
for it, which is exactly what switching the load on does. But it means **you
cannot calibrate `PV_PERFORMANCE_RATIO` against daily generation.** Doing that
drives it to about 0.39 and the watcher concludes the sun never delivers, so it
buys grid every night.

To check the model honestly, use days the system could not fill its own demand -
`purchaseValue > 0`, or the battery never reaching full. On those days nothing
is throttled and the comparison is real. They are also the only days where the
decision is close, so they are the sample worth having.

### Seeing the decision

```bash
docker compose run --rm tapo-watcher check.py
```

prints the socket, the hour-by-hour forecast with the spare column and which
hours clear the threshold, and the verdict with every term of the arithmetic
shown - including how many kWh it intends to buy tonight. Run it at any hour.

### Three things to get right

- **`HOUSE_DAYTIME_KWH` is the one guessed number.** It ships at 8 kWh. Take a
  few days of daytime consumption from your inverter's app, subtract this
  device, and put the real figure in. Too low and the watcher is over-optimistic
  about solar and skips boosts it should have taken.
- **`DEVICE_POWER_KW` should be measured, not guessed.** `check.py` prints the
  socket's live draw - run it while the device is heating. It is what turns
  "hours of spare sun" into kWh, so a wrong one skews the night's grid buy in
  proportion.
- **`TZ` must match the site.** Every window here is naive local time. A Pi left
  on UTC would shift the night window and the forecast hours apart; the watcher
  warns when its clock disagrees with the zone Open-Meteo answered in, but
  setting `TZ` is the fix.

### This switches a socket, not the inverter

Turning the socket on does not by itself import from the grid - a hybrid
inverter will happily serve that load from the battery, which is the opposite of
the intent. The night boost only saves money if the inverter is also told to run
on grid (or to grid-charge) during those hours, via its time-of-use schedule in
its own app. That schedule is set once, by hand; this program never talks to
the inverter.

## Notes

- **Host networking is deliberate.** The plugs are reached by raw LAN IP, and
  `network_mode: host` keeps the container on the Pi's interface with no NAT
  hop. It is Linux-only, so `docker compose up` on macOS will not reach the
  plugs - use `docker run --rm --env-file .env <image> check.py` there instead,
  which does work through the bridge for outbound LAN traffic in most setups.
- **The `.env` file is never baked into the image** - it is in `.dockerignore`,
  and the app reads plain environment variables at runtime.
- `tapo` ships prebuilt manylinux wheels for `aarch64` and `armv7l`, so no Rust
  toolchain and no compilation on the Pi.
- **Upgrading from a build that read the inverter**: nothing to do. The retired
  `soc` and power-flow columns stay in `data/history.db` - migration only ever
  adds - and simply go NULL from the first pass onward, while the new
  `spare_kw` and `plug_power_w` columns appear on restart. The `DEYE_*` keys in
  `.env` are ignored and can be deleted.
- Set `TZ` in `.env` if you want the log timestamps in your local time.

## Building locally instead of pulling

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```
