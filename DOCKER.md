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
- **the pause switch** - top right, `switching` / `paused`. See below;
- **the grid switch** - next to it, `both` / `inverter only` /
  `sentinel only`, when blackout mode has an inverter to ask. See below;
- **settings** - every key in `.env.example`, with its comment, editable on
  its own tab. Saving restarts the watcher on the new values. See below.

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

### Leaving the inverter or the sentinel out from the page

When blackout mode has an inverter to ask (`DEYE_APP_ID` set), a second switch
sits next to the pause, choosing who judges the grid:

| | how the night judges the grid |
|---|---|
| `both` | the normal thing - the inverter's own verdict, read off the Deye Cloud, with the sentinel as the fallback |
| `inverter only` | the sentinel is **not knocked on at all**; when the cloud cannot say, nothing does, and the night does not buy |
| `sentinel only` | the Deye Cloud is **not called at all**; `BLACKOUT_SENTINEL_IP` alone says whether the grid is there |

With no sentinel configured it is two-way, `inverter` / `inverter off`, and
off means nothing can say the grid is there, so the night does not buy at all.

`sentinel only` is for a cloud that is down, slow or answering nonsense;
`inverter only` for a sentinel that is unplugged, moved, or turned out to be on
a backed-up circuit - both without editing `.env` and restarting. They are two
switches in the logbook, `inverter` and `sentinel`, and like the pause they
survive a restart; a watcher that starts with either off says so in the log.
The page never turns both off, and `POST /api/control` refuses it with a 409.

What each gives up: the sentinel only sees a dead grid, not a weak one the
inverter would refuse, so with `sentinel only` a sagging evening reads as
"grid up". With `inverter only`, an outage that takes the router with it
leaves the cloud stale and the night not buying - the cautious side. Neither
can ever make the night buy when it otherwise would not have.

Switched back on, the inverter is not trusted on the spot: it has to be seen
holding the grid for the whole `DEYE_GRID_SETTLE` again, since nobody watched
it while it was off. The sentinel is asked again on the next pass, and a
contradiction with the inverter that was there before it was left out is
warned about again.

On the page the switch shows `inverter only` or `sentinel only` in amber, the
Socket card says `sentinel ... online (inverter off)` or `inverter silent and
the sentinel switched off`, and until the next pass picks a click up, `...
switched off · applies from the next check`. The hover has an `inverter · off
from the page` or `sentinel · off from the page` row, and the CSV has
`inverter_enabled` and `sentinel_enabled`. The Log gets one line per flip.

```bash
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"sentinel": false}' http://<pi-address>:8080/api/control
```

### Changing the settings from the page

The **Settings** tab lists every key `.env.example` documents, grouped and
explained by the same comments, with the value the watcher is running on.
Change any of them, press **Save and restart**, and:

1. the watcher checks it would start on the new values - in a separate
   interpreter, with exactly the parsing and refusals it starts with - and a
   value it would refuse (`CHECK_INTERVAL=soon`, `BOOST_STRATEGY=sunny`,
   blackout mode with nothing to ask) is refused on the page with the reason,
   and nothing is stored;
2. the values are written to `data/settings.env`;
3. the pass in progress finishes, the logbook is closed, and the process
   re-executes itself in place - the same PID, so Docker sees nothing and the
   container is not restarted. The page waits for the new process and reloads.

The socket is not touched by any of this: it keeps whatever state it was in
for the few seconds the restart takes, and the pause and grid switches, being
in the logbook, survive it.

`.env` is **never written**. In Docker the container does not even see that
file - compose hands its values over as environment variables - so the page
keeps its own file in `./data`, which is already mounted and survives an
image update. `data/settings.env` beats both `.env` and the container's
environment; everything not in it comes from `.env` as before. A key the page
has changed is marked `this page`, with a `use .env` button that hands it
back, and every start logs which keys the page is overriding. Typing a value
back to what `.env` says stops overriding it, too. `check.py` reads the same
file, so it reports what the watcher is actually running on.

Credentials - any key with `PASSWORD`, `SECRET`, `TOKEN` or `EMAIL` in its
name - are write-only: the page shows whether one is set, never the value, and
leaving the box empty keeps it. The commented-out keys (`TAPO_IMAGE`,
`NGROK_*`) are compose's, not the watcher's, and are not offered: changing
them inside the container would do nothing. A key that `.env.example` does
not document cannot be set at all.

If a change ever leaves the watcher unable to start anyway (a plug IP that is
valid but wrong only shows up as a plug that never answers), fix it on the
page like anything else; if the page itself is gone - `DASHBOARD_PORT`
changed, say - delete the file on the Pi and restart:

```bash
rm ~/tapo/data/settings.env && docker compose restart
```

### Keep it on the LAN

Every GET route is **read-only**, and none of them carries a credential. The
two that are not write a narrow set of things. `POST /api/control` writes one
of three booleans per request (the pause, or whether the inverter or the
sentinel is asked) and can reach nothing else. `POST /api/settings` writes
only keys `.env.example` documents, only values the watcher would start on,
and then restarts it. Neither can switch the socket itself or touch the record.

There is no password on any of it. Anyone who can open the page can pause the
heating, leave the inverter out, or **change the settings** - point the
watcher at another plug, another Tapo account or another Deye Cloud address,
which would hand that address your login. Keep it on the LAN and do not
forward the port.

What it does refuse is another website doing it through your browser: both
writing routes take only `Content-Type: application/json`, and refuse a
request whose `Origin` is not the page's own address. `curl` sends no
`Origin`, so the commands above still work as written.
`DASHBOARD_HOST=127.0.0.1` restricts it to the Pi itself (reachable then over
an SSH tunnel), and `DASHBOARD_PORT=0` switches it off entirely. The optional
`ngrok` profile in `docker-compose.yml` does the opposite - it puts this page,
switch and all, on a public address (`NGROK_DOMAIN`). It runs behind ngrok's
Google sign-in, and only the accounts in `NGROK_OAUTH_EMAIL` get past it; with
that unset nobody does. Set `NGROK_AUTHTOKEN`, `NGROK_DOMAIN` and
`NGROK_OAUTH_EMAIL` in `.env`, then `docker compose --profile ngrok up -d`.

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

That ration is measured two ways, and **the first of them to fill ends the
day**: `DEVICE_DAILY_KWH` off the meter, or `DEVICE_DAILY_HOURS` on the clock.

| Window | Source | Runs when |
|---|---|---|
| `NIGHT_START`-`NIGHT_END` (00:00-07:00) | cheap grid | today's sun will not cover the load - and, with `BLACKOUT_MODE=1`, the grid is actually there |
| `SOLAR_START`-`SOLAR_END` (10:00-18:00) | roof first, grid for the rest | the budget is not full yet - and, with `BLACKOUT_MODE=1`, this hour is forecast at `DEVICE_POWER_KW` or more |
| anything else | - | never |
| any of them, paused from the page | - | never - the relay is not touched at all |

**Two numbers decide all of it**: the plug's own meter, and the Open-Meteo
forecast for this roof. The inverter is read for one thing only - whether the
grid is there, in blackout mode below - and never for its battery charge or
its power flows.

### The arithmetic, in full

One number out of the forecast - what the roof should make over the whole of
`SOLAR_START`-`SOLAR_END` - and one number off the plug's meter. That is all
of it:

```
free        = clamp(forecast kWh for the day - HOUSE_DAYTIME_KWH, 0 .. DEVICE_DAILY_KWH)
buy tonight = DEVICE_DAILY_KWH - free
day is done = meter >= DEVICE_DAILY_KWH  or  socket on for >= DEVICE_DAILY_HOURS
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

If the meter cannot be read the socket is held off rather than run blind - the
forecast used to be a second opinion here and no longer is.

### The clock: the brake for the day nobody used any water

`DEVICE_DAILY_HOURS` is the same ration counted in hours the socket was
actually closed, and it stops either window. It exists because the load has its
own thermostat: on a day nobody drew hot water the element cuts out, the plug
measures almost nothing, and a kWh-only brake never releases - the socket would
sit closed for the entire window waiting for energy the load is never going to
take.

Set it to `DEVICE_DAILY_KWH / DEVICE_POWER_KW` to mean the same ration both
ways - 6 kWh through a 2 kW load is 3 h - so the clock only bites on a day the
meter has stalled. Lower it to cap the running time harder; `0` switches it off
and leaves the meter alone in charge.

The hours are integrated from the logbook, not counted in the process, so a
watcher restarted mid-window knows the socket has already had two of its three
hours. They are checked before the meter is missed, so a socket that has had
its allowance stays off even when the plug has gone unreadable. An unreadable
**logbook** reads as zero hours, which leaves the meter governing on its own -
a broken card must not be the thing that stops the load heating.

### Blackout mode: buy only while the grid is there

During scheduled outages the house runs on its battery. The night window would
not know that - it would see a shortfall, close the socket, and the inverter
would empty the battery into hot water at 01:00, exactly when the house needs
it to last until the power comes back.

`BLACKOUT_MODE=1` changes three things. The night comes on only while the
grid is there; the ration is no longer split between the roof and the night;
and the day window runs only in strong hours. The last two are under
[All or nothing](#all-or-nothing-the-roof-carries-the-day-or-the-night-buys-it)
below. Two things can say whether the grid is there, in this order:

1. **The inverter**, when `DEYE_APP_ID` is set, read off the Deye Cloud
   OpenAPI. What counts is the inverter's **own** decision to use the grid,
   not a voltage threshold of ours - see below. This is the better witness:
   it sees a grid too weak to use, which the sentinel cannot.
2. **The sentinel**, `BLACKOUT_SENTINEL_IP`. A device that is on **only when
   the grid is** - plugged into a socket the inverter does not back up - with
   a fixed IP in the router. It decides whenever the inverter cannot: the
   cloud failed, or its reading is older than `DEYE_MAX_AGE` seconds (300 by
   default).

```
night buys  = (the sum above says buy)  and  the grid is there
grid        = the inverter has held the grid for DEYE_GRID_SETTLE,
                if its reading is no older than DEYE_MAX_AGE
                (and the page has not left the inverter out)
              else BLACKOUT_SENTINEL_IP answers
                (unless the page has left the sentinel out)
              else unknown - which holds off like down
```

**What "the inverter has the grid" means.** The cloud has no relay or
connection field - all 113 measure points were checked. But the inverter's
own history on this site shows how it says no:

| what the inverter reports | what it means | seen here |
|---|---|---|
| `GridFrequency` 50 Hz, every phase present | it is using the grid | 11-03 01:01: buying 3.9 kW with the weakest phase at 188.6 V |
| `GridFrequency` **0 Hz**, voltage still there | it **refused** the grid as too weak | 11-04 18:34:52: phases at 176-185 V, and a `W3 Grid_Phase_Wrong` alert at the same second |
| a phase under 100 V | a phase is missing; a three-phase inverter cannot use that | 11-04 07:23:52: L2 at 0 V after an outage, L1 and L3 back |
| 0 V and 0 Hz | outage | every scheduled cut |

So no grid is any of: 0 Hz, a phase under 100 V, or a reading older than
`DEYE_MAX_AGE`. The inverter's cutoff on this site sits around 181 V on the
weakest phase. That figure is not configured anywhere here; it is whatever
the inverter itself decides, which is the point.

**Even accepted, the grid has to be held.** A weak grid flaps: on 11-04
between 18:27 and 18:44 the inverter refused it, took it back, and refused it
again, minute after minute. A socket closed on the first good minute is a
socket on the battery the next. So the night buys only once the inverter has
held the grid for `DEYE_GRID_SETTLE` seconds (600 by default) without one
refused reading in between, timed on the logger's own timestamps. A single
refusal starts the wait again. It also covers the inverter's own reconnect
delay after an outage. After a restart the wait starts from the first reading
seen, which errs towards waiting.

**`GridStatus` is not the connection.** It names which way energy flows:
`Purchasing energy` (importing), `Grid connected` (exporting), and `Static`
(nothing flowing). It reads `Static` through an outage **and** through a sunny
afternoon with 233 V on every phase, so it is recorded for the page and never
decided on.

**Check every minute.** The grid can only be judged as often as the watcher
asks. With `CHECK_INTERVAL=600` a refusal can go unseen for ten minutes, the
socket drawing on the battery all the while, and a refusal between two passes
is missed entirely. The logger uploads about every 60 s, and the cloud is
asked at most once a minute whatever the interval, so `CHECK_INTERVAL=60`
costs no extra quota beyond those 45,000 calls a month.

At least one of the two has to be configured, or the watcher refuses to
start. Either works alone.

**Why the inverter goes through the cloud.** This site's logger (a
`DYDA_WiBLE` stick) has no local Modbus - ports 8899 and 502 are closed, and
its web page is an encrypted setup form - so the cloud is the only way in.
That has a cost: an outage can take the router or the ISP with it, the logger
stops uploading, and its last reading says "grid held" for as long as the
outage lasts. `DEYE_MAX_AGE` is what stops that being believed. Keep it short.

**Setting up the inverter.**

- Register an app at developer.deyecloud.com. That gives `DEYE_APP_ID` and
  `DEYE_APP_SECRET`.
- Set `DEYE_EMAIL` and `DEYE_PASSWORD` to the Deye Cloud login the station
  belongs to. The app keys say which program is asking; the login says whose
  inverter it may read. A second account the station is shared with view-only
  keeps your own password off the Pi.
- `DEYE_API_URL` is the account's region: `eu1-developer` for Europe (the
  default), `us1-developer` for the Americas.

The station and the inverter's serial are looked up once, on the first pass.
The password is sent only as the SHA-256 hash the API asks for, and none of
the four keys is ever logged or put on the page.

**Quota.** The watcher asks for a reading at most once a minute, whatever
`CHECK_INTERVAL` says - the logger does not upload faster than that. That is
under 45,000 calls a month, against the developer plan's 100,000. The token
lasts about 60 days and is renewed a day early.

- **How the sentinel is asked.** A TCP connection to `BLACKOUT_SENTINEL_PORT`
  (default 80), 2 s timeout, tried twice a second apart before it is called
  silent - one lost Wi-Fi packet must not open the relay for a whole
  `CHECK_INTERVAL`. A *refused* connection counts as up: the reset came from
  the device itself. Some devices drop rather than refuse, so a port it really
  listens on is the safe choice.
- **The sentinel is asked even while the inverter decides** - unless the page
  has left it out - so the two can be
  compared. When they contradict each other about whether there is any mains
  at all, the Log gets one `warn` line saying so, and the inverter is
  believed. A sentinel that has been unplugged reads exactly like an outage;
  that line is how you find out. A weak grid the inverter refuses still powers
  the sentinel, so that is not counted as a contradiction.
- **No answer means down.** Anything but a clear "up" holds the night off.
  Wrong that way, the load tops up at the day tariff; wrong the other way, the
  battery is gone.
- **The day window has its own gate**, below. The grid's answer is not part of
  it: the top-up runs on the roof first, and the budget and clock brakes still
  bound it.
- **The budget does not move.** A night cut short by an outage just leaves
  more for the day to top up, through the same meter.

`python check.py` prints every phase, the weakest one and the frequency right now, how
long the inverter has held the grid, how old that reading is, and which of the
two the night would believe.

The grid is asked on every pass while blackout mode is on, whatever
`MONITOR_DAYTIME` says, so the page always knows and the record shows the
outages themselves. On the page:

- **Header badge.** `blackout · grid up · inverter`. It turns red with
  `DOWN` the moment the grid is gone or refused, amber with `settling` while
  the inverter holds a grid that has not yet lasted `DEYE_GRID_SETTLE`, and
  says `sentinel` instead of `inverter` whenever the sentinel was the one
  deciding. Hover it for what each one said.
- **Grid card.** On the Overview, next to the Socket card: `UP`, `DOWN`,
  `SETTLING` or `UNKNOWN` and whose word it is; L1, L2 and L3 as the inverter
  last reported them, the weakest marked (red only when a phase is missing,
  or when it is the weakest of a grid the inverter refused - never against a
  floor of ours); the frequency (`0 Hz · rejected`), how long the grid has
  been held against `DEYE_GRID_SETTLE`, the reading's age (`too old` past
  `DEYE_MAX_AGE`), the `GridStatus` flow word, and the sentinel's own answer
  even on a pass the inverter decided. Its last line is the logic as the
  page's switches stand now - `inverter, sentinel when it cannot say`,
  `inverter only`, `sentinel only` - in amber when one is left out. The CSV
  has `grid_l1_v`, `grid_l2_v`, `grid_l3_v` and `sentinel_up`.
- **Socket card.** The same answer in words: `inverter 205 V`, `inverter
  rejected it · 176 V`, `inverter: a phase is missing`, `inverter held it
  4 min of 10 min`, or the sentinel's address and `(inverter silent)` when the
  cloud could not say.
- **Chart.** A red rule above the plug band for every stretch the night could
  not buy - outage, refusal or settling.
- **Hover.** A `grid` row naming the source, and an `inverter` row with the
  weakest phase, the frequency, how long the grid had been held (or
  `rejected`), the `GridStatus` flow word and how old the reading was -
  marked `(too old)` when it was passed over.
- **Nights table.** The hours the grid was down.

Every one of those numbers is in the CSV too: `grid_source`, `grid_volts`
(the weakest phase), `grid_hz`, `grid_age_s`, `grid_status`, and
`grid_stable_s` (how long the inverter had held the grid, empty when it was
refused).

#### All or nothing: the roof carries the day, or the night buys it

Outside blackout mode the night buys only the shortfall and the day tops up
the rest "from roof and grid". During outages that second half is roof and
**battery**: any hour the roof makes less than the load draws, the inverter
covers the difference from the battery. So blackout mode never splits the
ration:

```
strong hours = hours of SOLAR_START-SOLAR_END forecast at DEVICE_POWER_KW or more
roof covers  = free >= DEVICE_DAILY_KWH
               and strong hours x DEVICE_POWER_KW >= DEVICE_DAILY_KWH
night target = 0 if roof covers, else DEVICE_DAILY_KWH       (never a part)
day window   = runs only while this hour's forecast >= DEVICE_POWER_KW
               (and the budget and clock brakes still bound it)
```

The strong hours are counted in "roof covers" on purpose. The day window
only runs in them, so a day with a big total spread thin under cloud is a
day it would never open. Judging the night on the total alone would skip the
buy for it and leave the load cold, which is what the hourly gate did before
it was removed outside blackout mode.

With the defaults (6 kWh, 2 kW) that means a day needs 14 kWh on the roof
**and** at least three hours forecast at 2 kW or more before the night stays
off. Anything less and the night buys all 6 kWh. If the grid is down all
night, the day window runs in the strong hours only and a weak hour holds
off: `... - would top up - but this hour is forecast at 1.4 kW, under the
load's 2.0: holding off rather than running on the battery`. An hour with no
forecast row holds off too.

`DEVICE_POWER_KW` has to be above 0 with blackout mode on; the watcher and
the Settings tab refuse it otherwise.

On the page: the day-ahead card shows `hours ≥ 2.0 kW` and a blackout note,
and the sun share is 0 whenever the night buys. The Today card shows `this
hour` (amber when it is under the gate), and the hover has `hour forecast` and
`strong hours`. Both are in the CSV as `hour_kw` and `strong_hours`;
`strong_hours` is empty with blackout mode off.

The Log says what the grid was on the first pass after every start, then one
line per change - `grid went DOWN - the inverter rejected the grid (0 Hz,
weakest phase 176 V), the night will not buy`, `grid is back - the inverter
has the grid, weakest phase 205 V, 50.0 Hz`. It also says once when the
inverter stops being believed and once when it is believed again. The verdict
reads "would buy ... - but the inverter has held the grid only 4 of 10 min, so
the grid is down: holding off".

It is an `.env` switch, not a page control: turning it on or off is a
container restart.

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
its own app. That schedule is set once, by hand; this program never writes
to the inverter, and only ever reads whether its grid is there.

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
  onward. Retired `.env` keys (`SOLAR_SURPLUS_ON_KW`, `SOLAR_SURPLUS_OFF_KW`)
  are ignored and can be deleted. The `DEYE_*` keys are back, for blackout
  mode's grid reading only - see above.
- Set `TZ` in `.env` if you want the log timestamps in your local time.

## Building locally instead of pulling

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```
