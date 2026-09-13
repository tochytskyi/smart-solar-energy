"""Config loading and connection helpers shared by main.py and check.py."""

import os
import socket
from pathlib import Path

from tapo import ApiClient

ENV_FILE = Path(__file__).with_name(".env")


def load_env(path=ENV_FILE):
    """Minimal .env loader so credentials stay out of the source."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def config(name, default=None, required=True):
    load_env()
    # An empty value in .env means "not set", so blank optional keys (an
    # unfilled INVERTER_KW, say) fall back to the default instead of to "".
    value = os.environ.get(name) or default
    if required and not value:
        raise SystemExit(
            "Missing config %r. Set it in %s or export it in the shell." % (name, ENV_FILE)
        )
    return value


def client():
    """Build an authenticated API client from the configured credentials."""
    return ApiClient(config("TAPO_EMAIL"), config("TAPO_PASSWORD"), timeout_s=10)


def is_online(ip, port=80, timeout=2):
    """True if the plug accepts a TCP connection on its local-API port.

    Uses TCP (not ICMP ping) on purpose: these plugs sit in Wi-Fi power-save
    and drop pings, but they always answer the port that control talks to - so
    a TCP hit means "reachable AND controllable", with no false OFFLINE.
    """
    try:
        with socket.create_connection((ip, port), timeout):
            return True
    except OSError:
        return False
