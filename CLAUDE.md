# CLAUDE.md

Working notes for this repository. Read before changing anything here.

## What this is

One Tapo P110 socket, switched so the load on it gets its `DEVICE_DAILY_KWH`
from the roof when the roof will manage it, and from cheap night grid only for
the part the roof will not. It runs as a single process on a Raspberry Pi in
Docker, decides once a `CHECK_INTERVAL`, writes everything it saw and did to a
SQLite logbook, and serves that logbook as a live page.

**The page can stop it.** One switch - `control.enabled` in the logbook,
thrown by `POST /api/control` - decides whether the loop is allowed to command
the relay. Paused, every pass still reads, decides and records; only the
switching stops, so the socket keeps whatever state it was in. It also asks
the plug what its relay is actually doing, because what it last commanded is
no longer evidence once a person can switch it by hand - that read reaches the
record and never a decision.

**The page can also leave the inverter out.** A second switch,
`control.inverter`, thrown through the same route, decides whether blackout
mode asks the Deye Cloud at all. Off, the cloud is not called and the sentinel
alone judges the grid; switched back on, the inverter has to be seen holding
the grid for the whole `DEYE_GRID_SETTLE` again. It can only narrow what the
night believes - off with no sentinel, nothing says the grid is there and the
night does not buy. These two are the only things on the page that change
anything, and `tests/test_contract.py::ThePageChangesOnlyItsDeclaredSwitches`
fails if a third appears.

**Two inputs to the arithmetic, and only two**: the plug's own meter and the
Open-Meteo forecast. The inverter is read for one thing - whether the grid is
there, as blackout mode's first opinion - through the Deye Cloud, because the
logger here speaks no local Modbus. The LAN sentinel decides whenever the cloud
cannot answer or its reading is older than `DEYE_MAX_AGE`. There was once a
Deye client that fed the battery and power flows into the decision; that is
not to come back. `tests/test_contract.py::TheInverterOnlySaysWhetherTheGridIsThere`
fails if the inverter reaches anything but `grid_up`.

**The forecast reaches the decision as one number a day** - the total the roof
should make - and it only ever sizes the night's grid buy. The day window is
the meter alone. `tests/test_contract.py::TheDecisionUsesTheDayTotal` fails if
per-hour gating creeps back.

`DOCKER.md` is the deployment and behaviour document - the arithmetic of both
windows is explained there, and it is the file to update when the rules change.

## Files

| file | what it is |
|---|---|
| `main.py` | the loop: read, decide, switch, record. Every tunable is a module-level constant read from `.env` |
| `tapo_client.py` | `.env` loading and the Tapo API client |
| `solar_forecast.py` | Open-Meteo hourly yield for this roof, and the row covering "now" |
| `deye_cloud.py` | Deye Cloud client: grid voltage and frequency only, for blackout mode |
| `history.py` | the SQLite logbook: `samples`, `events`, `forecasts` |
| `dashboard.py` | HTTP server for the page and its JSON. Reads, apart from the one route that throws the page's two switches |
| `dashboard.html` | the page itself - vanilla JS, hand-drawn SVG charts, no build step |
| `check.py` | one-off status report from the terminal |
| `demo.py` | fills a throwaway logbook with invented data and serves the page |
| `tests/` | the decision logic, the logbook and the routes, checked with `unittest`. `tests/__init__.py` stubs `tapo` and silences `.env`, so the suite needs no dependency, no network and no hardware |

## The standing rule: a change is not finished until the page shows it

The point of the logbook is that the watcher's behaviour can be read back
instead of guessed at. That only holds if every change to what it does reaches
the record and the page in the same commit. Working code with an invisible new
behaviour is an unfinished change.

| what you changed | what else must change |
|---|---|
| a number the decision uses or produces | a column in `SCHEMA` **and** `SAMPLE_FIELDS` (`history.py`), the `recorder.record(...)` call (`main.py`), `CSV_COLUMNS` (`dashboard.py`), and somewhere visible in `dashboard.html` - a card row, a chart series, or the hover tooltip |
| a new log line | `log(message, level, category)`, never a bare `print` - level is `info`/`warn`/`error` and category is what the Log panel filters on (`system`, `plug`, `forecast`, `decision`) |
| a new config key | `.env.example` with a comment saying what it is for, `settings()` in `main.py` if the page needs to draw it (thresholds, windows), `demo.py`'s `SETTINGS`, and `DOCKER.md` |
| a new window, mode or state | a `phase` value in `main.py`, its shading in the chart, its colour in the plug band, and its column in the Nights table |
| a new way to fail | log it at `warn` or `error` so it stands out red on the page, and make sure the loop carries on |
| a new thing the page can change | a name in `CONTROLS` (`history.py`), a branch in `do_POST` (`dashboard.py`), a control in the header **and** the state it produces drawn wherever the record shows it, and the guard in `tests/test_contract.py` widened deliberately rather than by accident |

Adding a column is safe on a running Pi: `history.Logbook` compares `SCHEMA`
against the open database at startup and `ALTER TABLE`s in anything missing,
leaving older rows NULL for the new field. Renaming or dropping one is not
automatic - avoid it, or write the migration. Dropping `soc` and the inverter's
power flows was done the cheap way: they are out of `SCHEMA` so a fresh file
never has them, and an existing file simply keeps the dead columns with their
old rows intact and NULL from then on.

## Checking a change

```bash
python -m unittest discover     # the whole suite, ~1 s, no network
python -m unittest tests.test_decisions -v
```

The constants the tests decide against are fixed in `tests/__init__.py`, not
read from `.env`: 6 kWh/day into a 2 kW load and an 8 kWh house - so every
expected number is one subtraction from the day's forecast kWh and can be
worked out by hand. The 3 h allowance is that same ration on the clock, so a
test standing at one brake's limit is standing at the other's too unless it
says so. `tests/test_contract.py` is the standing rule below,
checked by machine - it reads the source and fails when a recorded number
never reaches the schema, the CSV or the page.

```bash
python demo.py                  # invented history + the page on :8080
```

is the fast loop for anything visual: three days of plausible behaviour, both
tariffs, a cloud outage and a failed switch, without waiting for a night.

For the real thing:

```bash
python check.py                 # socket, meter, forecast, the verdict now
python solar_forecast.py        # today's hourly outlook
python main.py                  # the watcher, page on DASHBOARD_PORT
```

The page itself has no build step and nothing that tests it - open it, or
screenshot it headlessly, and look:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless \
  --screenshot=shot.png --window-size=1200,900 --virtual-time-budget=6000 \
  http://localhost:8080/
```

Check it at phone width too. Headless Chrome clamps its viewport to 500 px and
then crops the image, so `--window-size=400,...` shows a false clip - ask for
500 and read that. A wide table needs its own `.scroller`.

## Conventions

- **Standard library only.** `tapo` is the single dependency, and the Pi builds
  no wheels. HTTP is `urllib`, the store is `sqlite3`, the server is
  `http.server`, the page loads nothing from a CDN.
- **Comments say why, not what.** The code is read by someone deciding whether
  to trust it with their electricity bill.
- `%`-formatting in Python, not f-strings, to match what is already there.
- **Every time is naive local time** - the windows, the forecast rows, the
  timestamps. That only works while `TZ` matches the site; the watcher warns
  when its clock disagrees with the zone Open-Meteo answered in.
- **Credentials live in `.env` and go no further.** They are never logged, never
  in `settings()`, never in the image (`.dockerignore`), never on the page.
- **Recording must never break control.** Anything in `history.py` swallows its
  errors and reports them once: a full SD card must not stop the socket being
  switched. Keep it that way for anything you add there.
- **The plug's own meter is the single daily budget.** Both windows read it, so
  neither repeats what the other already delivered. Do not add a second counter.
- **That budget has two brakes, and the first to bite ends the day.**
  `DEVICE_DAILY_KWH` off the meter, `DEVICE_DAILY_HOURS` off the clock. The
  clock is there because the load has its own thermostat: on a day nobody drew
  hot water it cuts out, the meter barely moves, and a kWh-only brake holds the
  socket closed for the whole window waiting for energy that is never taken.
  The hours come from `Logbook.on_hours_today`, integrated from the samples, so
  a restart mid-window does not hand the load a fresh allowance - never replace
  it with a counter in the process. They are checked before the meter is
  missed, so a spent allowance beats an unreadable plug.
- **The forecast is one number a day, and only the night reads it.**
  `free_solar_kwh` is `clamp(pv_kwh - HOUSE_DAYTIME_KWH, 0 .. DEVICE_DAILY_KWH)`
  and nothing else. There was an hourly version that predicted which hours
  would clear a `SOLAR_SURPLUS_ON_KW` gate and ran only in those; it left the
  load cold on any overcast day, because no single hour ever cleared the bar
  and the night had already decided not to buy. The day total can still be
  wrong - a day under forecast tops up at the day tariff - but it fails
  towards a bigger bill rather than a cold load. Keep it that way.
- **The forecast is still no part of the day window.** `decide_solar` runs from
  `SOLAR_START` until the meter or the clock says the ration is spent, and an
  unreadable meter still means off while a kWh budget is set. It used to need
  the forecast's permission to run at all; that second opinion is gone and is
  not to come back - `tests/test_contract.py::TheDecisionUsesTheDayTotal`
  pins the arguments `decide_solar` may take.

## Things that have bitten

- `HTTPServer.server_bind()` resolves the host's FQDN, which stalls for seconds
  on a LAN with no reverse DNS - `dashboard.Server` skips it. Do not go back to
  plain `ThreadingHTTPServer`.
- The dashboard is **unauthenticated**, on the LAN by design, and it writes
  two booleans, one per request, through `POST /api/control`: the pause, and
  whether the inverter is asked about the grid. Anyone who can reach the page
  can stop the watcher heating, which is the whole reason
  the port must not be forwarded - `docker-compose.yml` has an optional ngrok
  profile that does exactly that, and turning it on puts the switch on the
  public internet with no password in front of it. Never add a second writing
  route, and never let one of them reach the relay directly: the page says
  whether the watcher may decide, it is not a remote control for the socket.
- Docker creates a missing bind-mount source as root, and the container runs as
  uid 1000 - `data/` has to exist before `docker compose up`.
- **The forecast is potential, not harvest. They are not the same number on
  this site.** The system is zero-export, so once the house and the battery are
  satisfied the array is throttled. Measured over 13 days: modelled 416 kWh,
  the inverter logged 202, and the clear days pin flat at 17-19 kWh whatever
  the sky did. Potential is the right number here - the curtailed energy
  appears the moment the socket asks for it - but never fit
  `PV_PERFORMANCE_RATIO` to daily generation; it lands near 0.39 and the
  watcher then buys grid every night of the year. Only days the system could
  not fill its own demand measure the model. Those are also the only days the
  decision is close, so that is the sample that matters. (The watcher reads
  only the inverter's grid status, never its generation, so this calibration
  is a manual, occasional job against whatever the inverter's own app reports.)
- **The day being judged follows the solar window, not the night one.**
  `solar_forecast.target_day` takes `SOLAR_END`: today until the roof's day is
  spent, tomorrow from then on. Keying it to `NIGHT_START` reads as equivalent
  and is not - a 00:00-07:00 window was judged "today" all evening, so the page
  and the log reported the shortfall for a day that was already over, right up
  until midnight rolled the date. No switch was ever wrong, because nothing is
  switched outside the windows; everything you read for those six hours was.
- **A cloud reading of the grid can be a lie by omission.** An outage can take
  the router with it; the logger stops uploading and its last reading says
  "grid up" all night. `GridWatch` treats anything older than `DEYE_MAX_AGE` as
  no answer and asks the sentinel. Never drop the age check, and never let a
  failing cloud read as "up".
- **The inverter refuses a weak grid by reporting 0 Hz, and `GridStatus` is not
  the connection.** There is no relay field in the cloud. Measured here: the
  inverter bought 3.9 kW at 188 V, then at 176-185 V reported `GridFrequency`
  0 with the voltage still there, flapping in and out for 17 minutes.
  `GridStatus` is the flow direction - `Static` means nothing crossing the
  meter, outage or sunny afternoon alike. So the inverter's 0 Hz is the
  verdict, a phase under 100 V is a missing one, and `DEYE_GRID_SETTLE` makes
  the night wait out the flapping. Do not replace this with a voltage floor of
  our own, and do not decide on `GridStatus`.
- Open-Meteo stamps an hourly row with the **end** of the hour it covers: the
  14:00 row is the mean irradiance over 13:00-14:00, not a reading at 14:00.
  `solar_forecast._covers` is what stops a window sliding an hour early. Ask for
  `..._instant` variants if you ever want the reading at the stamp instead.
