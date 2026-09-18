"""A small web view of the logbook, and the one switch that stops the watcher.

Served by the watcher itself on DASHBOARD_PORT, from a thread, using nothing
but the standard library - the Pi already runs this process, so a second one
(and a second set of dependencies) would be a poor trade for a status page.

Routes:
    GET  /                 the page
    GET  /api/state        latest sample, today's and tomorrow's curves, settings
    GET  /api/samples      ?hours=24&points=1500   chart data
    GET  /api/events       ?hours=24&level=&q=&limit=&before=   the log tail
    GET  /api/days         ?days=14                nightly totals
    GET  /api/export.csv   ?hours=168              the same samples as a file
    POST /api/control      {"enabled": true|false} pause or resume the switching

Every route but the last is read-only, and the last one writes exactly one
boolean: whether the watcher may command the relay at all. It cannot switch
the socket itself, cannot change a threshold, and cannot reach anything else
in the logbook - history.CONTROLS is the whole list of what may be written.

It is all unauthenticated: it exposes the numbers the watcher decides on,
never the credentials it decides with. Anyone who can reach the page can
pause the watcher, which is the reason this belongs on the LAN and nowhere
else. Do not forward the port.
"""

import json
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import history

PAGE = Path(__file__).with_name("dashboard.html")

CSV_COLUMNS = (
    "ts", "phase", "pv_forecast_kwh", "peak_kw", "cloud_cover",
    "rain_mm", "plug_power_w", "delivered_kwh", "free_kwh", "target_kwh",
    "enabled", "wanted", "socket_on", "reason",
)

# The most a control request may carry. It is one boolean in a small object;
# anything larger is not a page this program served, and is dropped without
# being read into memory.
MAX_BODY = 4096


class Server(ThreadingHTTPServer):
    """ThreadingHTTPServer without the reverse-DNS lookup at bind time.

    HTTPServer.server_bind() calls socket.getfqdn(), which on a LAN with no
    reverse DNS blocks for several seconds before the watcher's first pass.
    The name it resolves is only ever used in error pages.
    """

    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port


def serve(host, port, logbook, settings, on_log=None, on_control=None):
    """Start the dashboard in a background thread and return the server.

    `on_control(enabled)` is called on the serving thread when the page throws
    the pause switch, and only when it actually changed. It is a notification,
    not the switch itself - the logbook holds that, and the watcher reads it
    there - so a caller that only wants a log line can pass one.
    """
    server = Server((host, port), _handler_class(logbook, settings, on_control))
    threading.Thread(target=server.serve_forever, name="dashboard", daemon=True).start()
    if on_log:
        on_log("dashboard on http://%s:%d" % (host if host != "0.0.0.0" else "<this-host>", port))
    return server


def _handler_class(logbook, settings, on_control=None):

    class Handler(BaseHTTPRequestHandler):
        server_version = "tapo-watcher"
        protocol_version = "HTTP/1.1"

        # -- routing --------------------------------------------------------

        def do_GET(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)
            route = url.path.rstrip("/") or "/"
            try:
                if route == "/":
                    return self._page()
                if route == "/api/state":
                    return self._json(self._state())
                if route == "/api/samples":
                    return self._json(self._samples(query))
                if route == "/api/events":
                    return self._json(self._events(query))
                if route == "/api/days":
                    return self._json({"days": logbook.days(_int(query, "days", 14, 1, 90))})
                if route == "/api/export.csv":
                    return self._csv(query)
            except Exception as exc:      # a broken page must not kill the watcher
                return self._json({"error": "%s: %s" % (type(exc).__name__, exc)}, status=500)
            self._json({"error": "not found"}, status=404)

        def do_POST(self):
            """The one thing that may be written: the pause switch."""
            route = urlparse(self.path).path.rstrip("/") or "/"
            try:
                if route == "/api/control":
                    return self._control()
            except Exception as exc:
                return self._json({"error": "%s: %s" % (type(exc).__name__, exc)}, status=500)
            self._drain()
            self._json({"error": "not found"}, status=404)

        # -- payloads -------------------------------------------------------

        def _state(self):
            # Today's curve and tomorrow's. "forecast" is the first of them -
            # today - kept as its own field because the cards and the chart
            # ask about the day in progress far more often than the list.
            curves = logbook.forecasts(2)
            return {
                "now": time.time(),
                "sample": logbook.latest_sample(),
                "forecast": curves[0] if curves else None,
                "forecasts": curves,
                "settings": settings(),
                "stats": logbook.stats(),
                "recent": logbook.events(time.time() - 86400.0, limit=6),
                # Never set, or unreadable, means the watcher is switching:
                # the same answer main.py acts on, so the page cannot show a
                # pause the loop is not honouring.
                "enabled": logbook.control(history.CONTROL_ENABLED, True),
            }

        def _samples(self, query):
            hours = _int(query, "hours", 24, 1, 24 * 90)
            points = _int(query, "points", 1500, 100, 20000)
            since = time.time() - hours * 3600.0
            curves = logbook.forecasts(2)
            return {
                "hours": hours,
                "since": since,
                "samples": logbook.samples(since, max_points=points),
                "forecast": curves[0] if curves else None,
                "forecasts": curves,
            }

        def _control(self):
            """Pause or resume the switching, and answer with what it now is.

            The answer is read back out of the logbook rather than echoed, so
            a page that could not be stored - a full card, a read-only volume -
            is told the switch did not move instead of drawing a pause that
            was never taken.
            """
            body = self._drain()
            if body is None:
                return self._json({"error": "body too large"}, status=413)
            try:
                payload = json.loads(body or b"{}")
            except ValueError:
                return self._json({"error": "expected JSON"}, status=400)

            value = payload.get("enabled") if isinstance(payload, dict) else None
            # Only a real boolean (or the 0/1 a shell would send), never a
            # string: bool("false") is True, and a switch that means the
            # opposite of what was typed is worse than one that will not move.
            if value not in (True, False, 0, 1):
                return self._json({"error": 'expected {"enabled": true|false}'}, status=400)

            wanted = bool(value)
            before = logbook.control(history.CONTROL_ENABLED, True)
            logbook.set_control(history.CONTROL_ENABLED, wanted)
            after = logbook.control(history.CONTROL_ENABLED, True)
            if on_control is not None and after != before:
                on_control(after)
            return self._json({"enabled": after, "stored": after == wanted})

        def _drain(self, limit=MAX_BODY):
            """This request's body, or None if it is too big to be one of ours.

            The connection is kept alive between requests, so a body that is
            never read would be parsed as the next request line. Either it is
            read whole, or the socket is closed behind the answer.
            """
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length < 0 or length > limit:
                self.close_connection = True
                return None
            return self.rfile.read(length) if length else b""

        def _events(self, query):
            hours = _int(query, "hours", 24, 1, 24 * 90)
            before = query.get("before", [None])[0]
            return {
                "events": logbook.events(
                    time.time() - hours * 3600.0,
                    level=query.get("level", [None])[0],
                    search=(query.get("q", [""])[0] or "").strip() or None,
                    limit=_int(query, "limit", 300, 1, 2000),
                    before=float(before) if before else None,
                )
            }

        # -- writing it out -------------------------------------------------

        def _page(self):
            try:
                body = PAGE.read_bytes()
            except OSError:
                return self._json({"error": "dashboard.html is missing"}, status=500)
            self._send(body, "text/html; charset=utf-8")

        def _json(self, payload, status=200):
            body = json.dumps(payload, default=str).encode("utf-8")
            self._send(body, "application/json; charset=utf-8", status)

        def _csv(self, query):
            hours = _int(query, "hours", 168, 1, 24 * 90)
            rows = logbook.samples(time.time() - hours * 3600.0, max_points=0)
            lines = [",".join(CSV_COLUMNS)]
            for row in rows:
                cells = []
                for name in CSV_COLUMNS:
                    value = row.get(name)
                    if name == "ts":
                        value = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))
                    if value is None:
                        cells.append("")
                    elif isinstance(value, str):
                        cells.append('"%s"' % value.replace('"', '""'))
                    else:
                        cells.append(str(value))
                lines.append(",".join(cells))
            body = "\n".join(lines).encode("utf-8")
            self._send(body, "text/csv; charset=utf-8", filename="tapo-history.csv")

        def _send(self, body, content_type, status=200, filename=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if filename:
                self.send_header("Content-Disposition", 'attachment; filename="%s"' % filename)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass        # the watcher's own log is the interesting one

    return Handler


def _int(query, name, default, low, high):
    try:
        return max(low, min(high, int(query.get(name, [default])[0])))
    except (TypeError, ValueError):
        return default
