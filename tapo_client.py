"""Config loading and connection helpers shared by main.py and check.py."""

import os
import socket
import time

from tapo import ApiClient

import overrides
from overrides import ENV_FILE


def load_env(path=ENV_FILE):
    """Minimal .env loader so credentials stay out of the source.

    .env only fills what the environment leaves unset, as before; what the
    page's Settings tab saved (overrides.FILE) then beats both, since it is
    the newest word on the value.
    """
    for key, value in overrides.read_env(path).items():
        os.environ.setdefault(key, value)
    for key, value in overrides.read_env(overrides.FILE).items():
        if os.environ.get(key) != value:
            os.environ[key] = value
            if key == "TZ":
                time.tzset()        # the C library has already read the old one


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


def host_answers(ip, port=80, timeout=2):
    """True if anything at `ip` is powered and on the LAN, by a TCP knock.

    Unlike is_online, a refused connection counts as an answer: the reset came
    from the host's own network stack, so it is switched on even if nothing
    listens on that port. Only silence - a timeout, no route, no ARP reply -
    means it is gone. TCP rather than ICMP because the container runs as an
    unprivileged user with no ping binary, and the standard library is all
    there is.
    """
    try:
        with socket.create_connection((ip, port), timeout):
            return True
    except ConnectionRefusedError:
        return True
    except OSError:
        return False
