"""The page's Settings tab: every key in .env.example, editable, kept in data/.

What the page saves goes to FILE, which tapo_client.load_env reads after .env
and lets win, so the watcher, check.py and a restart all see
the same values. .env itself is never written: in Docker it is not even in the
container, and it stays the file a person fixes things in by hand. Deleting
data/settings.env puts everything back to what .env says.

The catalogue is .env.example itself - its sections, its comments, its example
values - so a key documented there appears on the page with its explanation,
and a key that is not documented cannot be set at all. That last part matters
more than it looks: the page is unauthenticated, and an arbitrary environment
variable (LD_PRELOAD, PYTHONPATH) handed to a restarting interpreter is code
execution, not configuration.

Nothing is stored that the watcher would refuse. A save is first run past
main.preflight() in a separate interpreter with the new values, and only a
clean exit moves the file into place - a typo in CHECK_INTERVAL must not leave
a container restarting forever with the page that could fix it gone.

Credentials are write-only. The page is told whether one is set, never what it
is, and an empty box leaves it alone.

Standard library only, and no tapo_client: the dashboard imports this, and
demo.py has to run the dashboard on an interpreter with nothing installed.
"""

import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXAMPLE = ROOT / ".env.example"
ENV_FILE = ROOT / ".env"

# Beside the logbook rather than in .env, because in Docker .env is the
# compose file's to read - the container is handed its values and never sees
# the file - while data/ is the one directory it can write and that outlives
# an image update.
FILE = ROOT / "data" / "settings.env"

# The environment exactly as the process was started, before either file was
# read into it - this module is imported before the first config() call. A
# restart is handed this and nothing later: what load_env() put in os.environ
# would otherwise be inherited, and an override the page has just removed
# would survive the restart that was meant to drop it.
STARTED_WITH = dict(os.environ)

# A key is a credential when its name says so. By pattern rather than by list,
# so the next account added to .env.example is hidden without anyone
# remembering to hide it.
SECRET = re.compile(r"PASSWORD|SECRET|TOKEN|EMAIL|_KEY$")

# The most a value may be. Nothing in .env.example is near it; a longer one is
# not a setting.
MAX_VALUE = 500

# When this process started, so the page can tell the restart it asked for
# from the process that took the request.
STARTED = time.time()

# Run in a fresh interpreter against a candidate file: the same import-time
# parsing and the same refusals the watcher starts with, and nothing more - no
# logbook, no plug, no network.
PREFLIGHT = (
    "import sys, pathlib, overrides;"
    " overrides.FILE = pathlib.Path(sys.argv[1]);"
    " import main; main.preflight()"
)

HEADER_RULE = re.compile(r"^#\s*-{10,}\s*$")
KEY_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


class Refused(Exception):
    """A save the page has to be told no about, with the HTTP status to say it with."""

    def __init__(self, message, status=400):
        Exception.__init__(self, message)
        self.status = status


def read_env(path):
    """KEY=value lines of an env file as a dict - {} when there is no file."""
    try:
        text = Path(path).read_text()
    except OSError:
        return {}
    values = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def is_secret(key):
    return bool(SECRET.search(key))


def catalogue(text):
    """.env.example as sections of groups: the comment block, then its keys.

    A block of comments and the keys right under it are one group - NIGHT_START
    and NIGHT_END share a sentence - and a blank line ends it. The ruled
    headers start a section. Keys that are commented out are compose's
    (TAPO_IMAGE, NGROK_*): changing them inside the container would do
    nothing, so they are not offered.
    """
    sections = [{"title": "Account and plug", "note": "", "groups": []}]
    header = None
    comment, group = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if HEADER_RULE.match(line):
            if header is None:
                header = []
            else:
                # The header's first sentence is its title; the lines wrap
                # mid-sentence, so they are joined before being split.
                title, _, note = " ".join(header).partition(". ")
                sections.append({"title": title.rstrip("."), "note": note, "groups": []})
                header = None
            comment, group = [], None
            continue
        if header is not None:
            header.append(line.lstrip("#").strip())
            continue
        match = KEY_LINE.match(line)
        if match:
            if group is None:
                group = {"help": " ".join(comment), "keys": []}
                sections[-1]["groups"].append(group)
                comment = []
            group["keys"].append({"key": match.group(1), "example": match.group(2).strip()})
            continue
        if line.startswith("#"):
            body = line.lstrip("#").strip()
            if KEY_LINE.match(body):
                comment, group = [], None       # compose's key, not ours
            else:
                group = None
                comment.append(body)
            continue
        comment, group = [], None
    return [section for section in sections if section["groups"]]


def keys(sections):
    return [entry["key"] for section in sections
            for group in section["groups"] for entry in group["keys"]]


def started_values():
    """Every key as the watcher would see it with no overrides at all."""
    values = read_env(ENV_FILE)
    values.update(STARTED_WITH)
    return values


def preflight(path, timeout=90):
    """None if the watcher would start on these overrides, else why not.

    The last line the interpreter wrote is the reason: a SystemExit's own
    sentence, or the exception at the bottom of a traceback - both name the
    key that was wrong.
    """
    try:
        done = subprocess.run([sys.executable, "-c", PREFLIGHT, str(path)], cwd=str(ROOT),
                              env=STARTED_WITH, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return "the check did not finish in %d s" % timeout
    except OSError as exc:
        return "the check could not run: %s" % exc
    if done.returncode == 0:
        return None
    lines = [line.strip() for line in done.stderr.splitlines() if line.strip()]
    return lines[-1] if lines else "the watcher refused these settings (exit %d)" % done.returncode


class Editor(object):
    """What the Settings tab shows, and the one way it saves.

    `base` is every key as the watcher sees it without the page - the
    environment it started with, filled from .env - and `check(path)` is the
    preflight above. Both are arguments so the tests and the demo can hand
    over their own and never start an interpreter or read the site's .env.
    """

    def __init__(self, path=None, example=EXAMPLE, base=None, check=preflight):
        self.path = Path(path) if path is not None else FILE
        try:
            text = Path(example).read_text()
        except OSError:
            text = ""
        self.sections = catalogue(text)
        self.allowed = set(keys(self.sections))
        self.base = started_values() if base is None else dict(base)
        self.check = check
        self.started = STARTED

    def stored(self):
        return {key: value for key, value in read_env(self.path).items()
                if key in self.allowed}

    def view(self):
        """The catalogue with the values the next start will use. No credential values."""
        stored = self.stored()
        sections = []
        for section in self.sections:
            groups = []
            for group in section["groups"]:
                entries = []
                for entry in group["keys"]:
                    key = entry["key"]
                    value = stored.get(key, self.base.get(key, ""))
                    item = {"key": key, "example": entry["example"],
                            "overridden": key in stored, "secret": is_secret(key)}
                    if item["secret"]:
                        # Whether it is set, and nothing that would narrow it down.
                        item["set"] = bool(value)
                        item["example"] = ""
                    else:
                        item["value"] = value
                        item["env"] = self.base.get(key, "")
                    entries.append(item)
                groups.append({"help": group["help"], "keys": entries})
            sections.append({"title": section["title"], "note": section["note"],
                             "groups": groups})
        return {"started": self.started, "file": str(self.path), "sections": sections}

    def save(self, values, reset):
        """Store these changes if the watcher would start on them; the keys that changed.

        `values` maps a key to its new value, `reset` lists keys to hand back
        to .env. A value equal to what is already in force is not stored, so
        submitting the whole form unchanged changes nothing and restarts
        nothing.
        """
        if not isinstance(values, dict) or not isinstance(reset, list):
            raise Refused('expected {"values": {KEY: "value"}, "reset": [KEY]}')
        unknown = sorted(str(key) for key in list(values) + reset if key not in self.allowed)
        if unknown:
            raise Refused("not a setting: %s" % ", ".join(unknown))
        for key, value in values.items():
            if not isinstance(value, str):
                raise Refused("%s: expected a string" % key)
            # One line, or the file would grow a second key out of the first.
            if any(c in value for c in "\r\n\0") or len(value) > MAX_VALUE:
                raise Refused("%s: one line of at most %d characters" % (key, MAX_VALUE))

        stored = self.stored()
        updated = dict(stored)
        for key in reset:
            updated.pop(key, None)
        for key, value in values.items():
            value = value.strip()
            # An override only ever records a difference from .env, so a
            # value typed back to what .env says simply stops overriding.
            if value == self.base.get(key, ""):
                updated.pop(key, None)
            else:
                updated[key] = value
        changed = sorted(key for key in self.allowed
                         if updated.get(key) != stored.get(key))
        if not changed:
            return []

        self.path.parent.mkdir(parents=True, exist_ok=True)
        candidate = self.path.with_name(self.path.name + ".new")
        lines = ["# Saved from the dashboard's Settings tab. These beat .env;",
                 "# delete this file to go back to .env alone."]
        lines += ["%s=%s" % (key, updated[key]) for key in keys(self.sections) if key in updated]
        # Owner-only from the first byte: it may hold a password.
        fd = os.open(str(candidate), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write("\n".join(lines) + "\n")
        try:
            problem = self.check(candidate)
        except Exception as exc:
            problem = "%s: %s" % (type(exc).__name__, exc)
        if problem:
            os.unlink(str(candidate))
            raise Refused("not saved - the watcher would not start: %s" % problem, status=422)
        os.replace(str(candidate), str(self.path))
        return changed
