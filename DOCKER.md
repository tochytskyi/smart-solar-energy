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

- **the state now** - socket, what it is drawing, how much of the day's budget
  the night has to buy, the forecast for the day the next decision will use
  (today until its solar window is spent, tomorrow from then on), and the
  verdict sentence with the kWh arithmetic behind it;
- **a chart** over 6 hours to 30 days, all in kW - the forecast curves, the
  socket's own measured draw against the level the load pulls, both windows
  shaded, and a band underneath showing exactly when the socket was on and on
  which tariff. It reaches past `now` - marked with a line - to show the hours
  still to come, never giving the future more than half the width;
- **nights** - per day, how long the socket ran on grid versus on solar, what
  the plug's meter recorded, and the grid share that day was sized for;
- **the forecast, hour by hour, for today and tomorrow** - what the roof should
  make each hour, and the sky behind it. The decision does not read these rows;
  the foot of each day carries the whole of the arithmetic it does do, so the
  verdict can be checked by eye. Open-Meteo is asked for two days at a time, so
  the second one costs nothing extra;
- **the log** - the same lines `docker compose logs` shows, kept for
  `HISTORY_RETENTION_DAYS`, filterable by level and text;
- **the pause switch** - top right, `switching` / `paused`. See below.

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

### Pausing it from the page

The switch in the top right corner hands the socket back to you:

| | what the watcher does |
|---|---|
| `switching` | the normal thing - decides every `CHECK_INTERVAL` and commands the relay |
| `paused` | still reads the meter, still fetches the forecast, still records every pass and its verdict - but never touches the relay |

**Pausing does not switch the socket.** It leaves it exactly as it was at that
moment: on stays on, off stays off, and it will sit there until the switch goes
back or you change it yourself in the Tapo app. That is the point of it - a
morning where you want the boiler left alone, or an evening where you want it
on regardless of the arithmetic, without stopping the container and losing the
record.

While it is paused the page says so in a band across the top, the Socket card
says the state is being left alone, and the chart outlines the paused stretches
over the plug band, so a night that looks wrong can be read back later and
explained. The socket's state is read off the plug itself for as long as the
pause lasts, rather than remembered - so switching it by hand in the Tapo app
shows up on the chart like any other change. The verdict is still worked out and written to the log and the CSV -
"paused from the page - would be ON: ..." - so you can see what it wanted to do
while it was not doing it.

The switch is kept in the logbook, not in memory: it survives a restart, a
reboot and a `docker compose pull`. A watcher that starts up paused says so in
the log. Coming back to `switching` re-sends the verdict to the plug rather
than assuming the relay is where it was left, because it may not be.

```bash
# the same switch, without a browser
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"enabled": false}' http://<pi-address>:8080/api/control
curl -s http://<pi-address>:8080/api/state | grep -o '"enabled":[a-z]*'
```

`python check.py` reports the pause too, above the verdict it would otherwise
be acting on.

### Keep it on the LAN

Every other route is **read-only**, and none of them carries a credential. The
one that is not - `POST /api/control` - writes a single boolean and can reach
nothing else: it cannot switch the socket, cannot change a threshold, and
cannot touch the record.

There is no password on any of it. Anyone who can open the page can pause the
heating, so keep it on the LAN and do not forward the port.
`DASHBOARD_HOST=127.0.0.1` restricts it to the Pi itself (reachable then over
an SSH tunnel), and `DASHBOARD_PORT=0` switches it off entirely. The optional
`ngrok` profile in `docker-compose.yml` does the opposite - it puts this page,
switch and all, on a public address - so only run it behind ngrok's own access
control, and never with a reserved domain you have shared.

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
| `SOLAR_START`-`SOLAR_END` (10:00-18:00) | roof first, grid for the rest | the budget is not full yet |
| anything else | - | never |
| any of them, paused from the page | - | never - the relay is not touched at all |

**Two numbers decide all of it**: the plug's own meter, and the Open-Meteo
forecast for this roof. Nothing is read from the inverter - no cloud account,
no battery charge, no API key anywhere in the loop.

### The arithmetic, in full

One number out of the forecast - what the roof should make over the whole of
`SOLAR_START`-`SOLAR_END` - and one number off the plug's meter. That is all
of it:

```
free        = clamp(forecast kWh for the day - HOUSE_DAYTIME_KWH, 0 .. DEVICE_DAILY_KWH)
buy tonight = DEVICE_DAILY_KWH - free
```

`HOUSE_DAYTIME_KWH` is what the rest of the house takes out of the roof across
that window - including whatever the battery charges from solar, because that
is roof the load cannot have either. It is the one figure here that is a
guess; everything else is measured or forecast.

### The night: buy only the shortfall

Inside `NIGHT_START`-`NIGHT_END` the socket runs until the plug's meter says
`buy tonight` kWh have gone through it, then stops - mid-window, not at the
end. `buy tonight == 0` means the day covers the load outright and the socket
never comes on at all.

The forecast is read fresh every pass, so a revision during the night moves
the target under a socket that is already running.

### The day: top up whatever is left

From `SOLAR_START` the socket simply runs until the meter reads
`DEVICE_DAILY_KWH`, and then stops. No forecast, no threshold, no hysteresis:
whatever the roof is making goes into the load first and the grid covers the
rest.

Because both windows read the same meter, the night's purchase counts against
this one. A night that bought 2 of the 6 kWh leaves the day to find 4.

**The meter is the only brake on this window.** If it cannot be read the
socket is held off rather than run blind - the forecast used to be a second
opinion here and no longer is.

### Why the day total and not the hourly curve

The hourly version was here and it was worse. It predicted which individual
hours would be sunny enough to carry the load outright, ran only in those, and
sized the night's buy from the same count. On a genuinely overcast day no hour
ever cleared the bar, so the load was promised nothing, bought nothing, and
ended the day cold.

The day total cannot make that mistake. It can still be wrong - a day that
comes in under forecast leaves the top-up window importing at the day tariff -
but the failure is a slightly larger bill, not a cold load. That is the right
way round.

### Which day it is judging

Today, while today's solar window still has hours in it - tomorrow, from the
moment it ends. At 21:00 the dashboard and `check.py` are therefore showing
tomorrow's curve and the buy that tonight's window intends to make, not the day
that has just finished. Inside the night window nothing changes: a 00:00-07:00
window is judging the day it ends on either way.

### A night short enough to miss

`NIGHT_START`-`NIGHT_END` can only deliver `hours x DEVICE_POWER_KW`. A three
hour window and a 2 kW load is 6 kWh, so a 6 kWh budget has no slack at all
and anything above it cannot be bought at the cheap tariff however bad the
forecast. The watcher logs a `warn` at startup when the sums do not fit; the
day window then quietly makes up the difference at the day tariff.

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

prints the socket and its meter, the hour-by-hour forecast, and the verdict
with every term of the arithmetic shown - including how many kWh it intends to
buy tonight. Run it at any hour.

### Three things to get right

- **`HOUSE_DAYTIME_KWH` is the one guessed number.** It ships at 8 kWh. Take a
  few days of daytime consumption from your inverter's app, subtract this
  device, and add whatever the battery charges from solar - that is roof the
  load cannot have either. Too low and the watcher is over-optimistic about
  solar and under-buys at the cheap tariff, leaving the day window to make up
  the difference at the expensive one.
- **`DEVICE_POWER_KW` should be measured, not guessed.** `check.py` prints the
  socket's live draw - run it while the device is heating. It no longer enters
  the arithmetic, but it is what decides whether the cheap window is long
  enough to deliver the budget at all.
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
- **Upgrading from an older build**: nothing to do. Retired columns - `soc`
  and the inverter power flows, and now `spare_kw` - stay in `data/history.db`
  because migration only ever adds, and simply go NULL from the first pass
  onward. Retired `.env` keys (`DEYE_*`, `SOLAR_SURPLUS_ON_KW`,
  `SOLAR_SURPLUS_OFF_KW`) are ignored and can be deleted.
- Set `TZ` in `.env` if you want the log timestamps in your local time.

## Building locally instead of pulling

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```
