# CLAUDE.md

Working notes for this repository. Read before changing anything here.

## What this is

One Tapo P110 socket, switched so the load on it gets its `DEVICE_DAILY_KWH`
from the roof when the roof will manage it, and from cheap night grid only for
the part the roof will not. It runs as a single process on a Raspberry Pi in
Docker, decides once a `CHECK_INTERVAL`, writes everything it saw and did to a
SQLite logbook, and serves that logbook as a live page.

**Two inputs, and only two**: the plug's own meter and the Open-Meteo forecast.
The inverter is not read - there was a Deye Cloud client here and it is gone.
`tests/test_contract.py::TheInverterIsNotRead` fails if it creeps back.

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
| `history.py` | the SQLite logbook: `samples`, `events`, `forecasts` |
| `dashboard.py` | read-only HTTP server for the page and its JSON |
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
worked out by hand. `tests/test_contract.py` is the standing rule below,
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
- **The forecast is one number a day, and only the night reads it.**
  `free_solar_kwh` is `clamp(pv_kwh - HOUSE_DAYTIME_KWH, 0 .. DEVICE_DAILY_KWH)`
  and nothing else. There was an hourly version that predicted which hours
  would clear a `SOLAR_SURPLUS_ON_KW` gate and ran only in those; it left the
  load cold on any overcast day, because no single hour ever cleared the bar
  and the night had already decided not to buy. The day total can still be
  wrong - a day under forecast tops up at the day tariff - but it fails
  towards a bigger bill rather than a cold load. Keep it that way.
- **The meter is the only brake on the day window.** `decide_solar` runs from
  `SOLAR_START` until the meter says the budget is full, so an unreadable meter
  has to mean off. It used to need the forecast's permission to run at all;
  that second opinion is gone.

## Things that have bitten

- `HTTPServer.server_bind()` resolves the host's FQDN, which stalls for seconds
  on a LAN with no reverse DNS - `dashboard.Server` skips it. Do not go back to
  plain `ThreadingHTTPServer`.
- The dashboard is **unauthenticated and read-only**, on the LAN by design.
  Never add a route that writes, and never expose it to the internet.
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
  decision is close, so that is the sample that matters. (The watcher no longer
  reads the inverter at all, so this calibration is now a manual, occasional
  job against whatever the inverter's own app reports.)
- **The day being judged follows the solar window, not the night one.**
  `solar_forecast.target_day` takes `SOLAR_END`: today until the roof's day is
  spent, tomorrow from then on. Keying it to `NIGHT_START` reads as equivalent
  and is not - a 00:00-07:00 window was judged "today" all evening, so the page
  and the log reported the shortfall for a day that was already over, right up
  until midnight rolled the date. No switch was ever wrong, because nothing is
  switched outside the windows; everything you read for those six hours was.
- Open-Meteo stamps an hourly row with the **end** of the hour it covers: the
  14:00 row is the mean irradiance over 13:00-14:00, not a reading at 14:00.
  `solar_forecast._covers` is what stops a window sliding an hour early. Ask for
  `..._instant` variants if you ever want the reading at the stamp instead.
