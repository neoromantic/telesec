# managed by devkit v1 — change it in neoromantic/devkit templates/telemetry/, not here.
# Spec: https://github.com/neoromantic/devkit/blob/main/docs/standards/telemetry.md
"""Usage telemetry: stdlib only, never raises, never blocks.

create_telemetry(...) returns a client. A CLI calls show_notice(); an app or plugin
shows notice_text() itself and calls mark_notice_seen(). Then started(), feature(),
error(), and flush() before exit (or flush_at_exit() once). Importing this module
starts no thread, opens no connection and registers nothing.
"""

import atexit
import contextlib
import functools
import json
import os
import platform
import re
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from datetime import UTC, datetime

ENDPOINT = "https://usage.sergeybaus.com/i/v0/e/"
API_KEY = "phc_np7sxHvzFH1HDTlQdMaqRDrpOVZ0xytnSDxyCMBwNO9"  # public. gitleaks:allow
LIB = "devkit-telemetry"
LIB_VERSION = "1"
NOTICE_VERSION = 1
NOTICE_URL = "https://usage.sergeybaus.com"
TIMEOUT = 3.0
THROTTLED_MODES = ("scheduled", "daemon", "plugin", "script")
CI_VARS = ("GITHUB_ACTIONS", "GITLAB_CI", "BUILDKITE", "CIRCLECI", "TF_BUILD",
           "JENKINS_URL", "TEAMCITY_VERSION", "CODEBUILD_BUILD_ID")
TEST_VARS = ("PYTEST_CURRENT_TEST", "PYTEST_VERSION")
AGENT_VARS = (("CLAUDECODE", "claude-code"), ("CODEX_SANDBOX", "codex"),
              ("CODEX_THREAD_ID", "codex"), ("CURSOR_AGENT", "cursor"),
              ("GEMINI_CLI", "gemini"))
OPTIONAL = ("$os_version", "channel", "agent")  # left out of an event when empty
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
                  re.IGNORECASE)

_inflight: list[threading.Thread] = []
_inflight_lock = threading.Lock()
_system_cache: dict = {}


def _safe(default=None):
    """Swallow every error: telemetry must never fail the product."""
    def wrap(fn):
        @functools.wraps(fn)
        def run(*args, **kwargs):
            with contextlib.suppress(Exception):
                return fn(*args, **kwargs)
            return default
        return run
    return wrap


def _env(name: str) -> str:
    """Values are trimmed; empty counts as unset."""
    return os.environ.get(name, "").strip()


def _now() -> datetime:
    return datetime.now(UTC)


def _is_tty(stream) -> bool:
    # Frozen GUI apps (PyInstaller windowed builds) have sys.stderr = None.
    with contextlib.suppress(Exception):
        return stream is not None and bool(stream.isatty())
    return False


def _agent() -> str | None:
    name = _env("AI_AGENT").split("_", 1)[0].lower()[:40]
    return name or next((agent for var, agent in AGENT_VARS if _env(var)), None)


def _mode(mode: str | None, agent: str | None) -> str:
    tty = _is_tty(sys.stdin) or _is_tty(sys.stderr)
    return mode or ("agent" if agent else "interactive" if tty else "script")


def _prefix(product: str, env_prefix: str | None) -> str:
    """MY_TOOL for my-tool: upper-cased, other characters as _."""
    if env_prefix is not None:
        return env_prefix
    return re.sub(r"[^A-Z0-9]", "_", str(product or "").upper())


def _env_decision(prefix: str, version: str, endpoint: str):
    """Rules 1-4 of the spec as (on, reason, debug); None leaves it to the state."""
    if not endpoint:
        return False, "no-endpoint", False
    if _env("DO_NOT_TRACK").lower() not in ("", "0", "false"):
        return False, "do-not-track", False
    value = _env(f"{prefix}_TELEMETRY").lower() if prefix else ""
    if value in ("0", "off", "false"):
        return False, "env", False
    if value in ("1", "on", "true", "debug"):
        return True, "env", value == "debug"
    ci = _env("CI").lower()
    if (ci and ci != "false") or any(_env(var) for var in CI_VARS):
        return False, "ci", False
    if any(_env(var) for var in TEST_VARS):
        return False, "test", False
    v = str(version or "").strip().lower().removeprefix("v")
    if v in ("", "0.0.0") or "dev" in v:  # dev, devel, (devel), 1.2.0-dev.1, v0.0.0
        return False, "dev-version", False
    return None


def _system() -> dict:
    if not _system_cache:
        name, os_version = platform.system(), ""
        if name == "Darwin":
            os_version = platform.mac_ver()[0]
        elif name == "Windows":
            os_version = platform.version()
        elif name == "Linux":  # the distribution's version, else the kernel's
            with contextlib.suppress(Exception):
                os_version = platform.freedesktop_os_release().get("VERSION_ID", "")
        os_version = os_version or platform.release()
        machine = platform.machine().lower()
        arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64"}.get(machine)
        names = {"Darwin": "macOS", "Windows": "Windows", "Linux": "Linux"}
        _system_cache.update({"$os": names.get(name, name.lower()),
                              "$os_version": os_version, "arch": arch or machine,
                              "runtime": f"python {platform.python_version()}"})
    return _system_cache


def _scrub(text: str, home: str, *names: str) -> str:
    """In one pass, ignoring case, wherever they are not part of a longer word: the
    home directory -> ~, a user name -> <user>. Then trimmed, at most 300 characters."""
    text = text[:4000]  # bounds the regex work; the result is cut to 300 below
    home = home.rstrip("/\\")
    swaps = dict.fromkeys({home.lower(), home.replace("\\", "/").lower()} - {""}, "~")
    for name in names:
        name = (name or "").strip().lower()
        if len(name) > 1:  # a one-letter name would mangle every word
            swaps.setdefault(name, "<user>")
    if swaps:
        alternatives = "|".join(map(re.escape, sorted(swaps, key=len, reverse=True)))
        pattern = rf"(?<![A-Za-z0-9])(?:{alternatives})(?![A-Za-z0-9])"
        text = re.sub(pattern, lambda m: swaps.get(m[0].lower(), "<user>"), text,
                      flags=re.IGNORECASE)
    return text.strip()[:300]


def _home_and_names() -> tuple[str, ...]:
    home = os.path.expanduser("~").rstrip("/\\")
    return (home, _env("USER"), _env("LOGNAME"), _env("USERNAME"),
            os.path.basename(home))


def _valid_id(value) -> str | None:
    return value if isinstance(value, str) and UUID.match(value) else None


def _seen(state: dict) -> bool:
    notice = state.get("notice")
    return type(notice) is int and notice >= NOTICE_VERSION


def _common(product, version, channel, mode: str, agent: str | None) -> dict:
    """The properties of every event; empty optional ones are left out."""
    props = {"product": product, "$app_version": version, **_system(),
             "channel": channel, "mode": mode, "agent": agent}
    return {k: v for k, v in props.items() if v or k not in OPTIONAL}


def _body(event: str, distinct_id: str, moment: datetime, properties: dict) -> dict:
    timestamp = moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    props = {"$process_person_profile": False, "$lib": LIB, "$lib_version": LIB_VERSION}
    return {"api_key": API_KEY, "event": event, "distinct_id": distinct_id,
            "timestamp": timestamp, "properties": {**props, **properties}}


def _send(endpoint: str, body: dict, product, version, debug: bool) -> None:
    data = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    if debug:  # print instead of sending
        if sys.stderr is not None:
            sys.stderr.write(f"[telemetry] {data}\n")
        return
    user_agent = f"{LIB}/{LIB_VERSION} ({product}/{version})"
    thread = threading.Thread(target=_post, args=(endpoint, data.encode(), user_agent),
                              name="telemetry", daemon=True)
    thread.start()
    with _inflight_lock:
        _inflight[:] = [t for t in _inflight if t.is_alive()] + [thread]


def _post(endpoint: str, data: bytes, user_agent: str) -> None:
    # One attempt; the response is ignored. urllib takes HTTPS_PROXY from the
    # environment. TIMEOUT bounds each socket step, and only flush() ever waits
    # for this daemon thread, up to its own timeout.
    headers = {"Content-Type": "application/json", "User-Agent": user_agent}
    with contextlib.suppress(Exception):
        request = urllib.request.Request(endpoint, data, headers, method="POST")
        with urllib.request.urlopen(request, timeout=TIMEOUT):
            pass


def flush(timeout: float = 0.5) -> None:
    """Wait up to `timeout` seconds for requests already in flight."""
    with contextlib.suppress(Exception):
        deadline = time.monotonic() + timeout
        with _inflight_lock:
            threads = list(_inflight)
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))


@_safe()
def capture(event: str, properties: dict, distinct_id: str, *, product: str,
            version: str, channel: str | None = None, mode: str | None = None,
            env_prefix: str | None = None, endpoint: str | None = None) -> None:
    """Send one event with no state, notice or throttling, for server code with its
    own ids (random or keyed, never an email or an account id; random when empty).
    Rules 1-4 of "Turning it off" still apply."""
    endpoint = ENDPOINT if endpoint is None else endpoint
    decision = _env_decision(_prefix(product, env_prefix), version, endpoint)
    if not event or (decision is not None and not decision[0]):
        return
    agent = _agent()
    props = {**_common(product, version, channel, _mode(mode, agent), agent),
             **(properties or {})}
    _send(endpoint, _body(event, distinct_id or str(uuid.uuid4()), _now(), props),
          product, version, bool(decision and decision[2]))


class Telemetry:
    """Made by create_telemetry(). Every method swallows its own errors."""

    def __init__(self, product: str, version: str, state_dir, channel: str,
                 env_prefix: str | None = None, mode: str | None = None,
                 features=(), off_command: str | None = None,
                 endpoint: str | None = None) -> None:
        self.product, self.version, self.channel = product, version, channel
        self.off_command = off_command or f"{product} telemetry off"
        self.endpoint = ENDPOINT if endpoint is None else endpoint
        self.state_file, self.features, self.agent = "", frozenset(), None
        self.mode = mode or "script"
        self._decision = (False, "error", False)  # until the setup below succeeds
        self._quiet = False  # this instance showed the notice, so it sends nothing
        self._lock = threading.RLock()
        with contextlib.suppress(Exception):
            state_dir = os.fspath(state_dir)
            if state_dir:  # an empty state_dir must not mean the current directory
                self.state_file = os.path.join(state_dir, "telemetry.json")
            self.features = frozenset(f for f in features if isinstance(f, str))
            self.agent = _agent()
            self.mode = _mode(mode, self.agent)
            self._decision = _env_decision(_prefix(product, env_prefix), version,
                                           self.endpoint)

    # telemetry.json is read again on every call, so `telemetry off` in another
    # process stops a long-running daemon or plugin at its next event.
    def _load(self) -> dict:
        with contextlib.suppress(Exception), open(self.state_file, "rb") as f:
            state = json.load(f)
            return state if isinstance(state, dict) else {}
        return {}

    def _write(self, state: dict) -> bool:
        """One line of JSON, to a temp file in the same directory renamed into place."""
        enabled, notice, daily = (state.get(k) for k in ("enabled", "notice", "daily"))
        clean = {"id": _valid_id(state.get("id")),
                 "enabled": enabled if isinstance(enabled, bool) else None,
                 "notice": notice if type(notice) is int else 0,
                 "installed": state.get("installed") is True,
                 "daily": {k: v for k, v in daily.items() if isinstance(v, str)}
                 if isinstance(daily, dict) else {}}
        tmp = None
        with contextlib.suppress(Exception):
            directory = os.path.dirname(self.state_file)
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".telemetry-", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(clean, separators=(",", ":")) + "\n")
            os.replace(tmp, self.state_file)
            return True
        if tmp:
            with contextlib.suppress(Exception):
                os.unlink(tmp)
        return False

    def _decide(self, state: dict) -> tuple[bool, str, bool]:
        if self._decision is not None:
            return self._decision
        if isinstance(state.get("enabled"), bool):
            return state["enabled"], "state", False
        return True, "default", False

    def _mark_seen(self, enabled: bool | None = None) -> bool:
        """Record the notice (and an id). Unless it is `telemetry on` (enabled=True),
        an explicit opt-in, this instance then sends nothing."""
        with self._lock:
            state = self._load()
            was_seen = _seen(state)
            state["notice"] = state["notice"] if was_seen else NOTICE_VERSION
            state["id"] = _valid_id(state.get("id")) or str(uuid.uuid4())
            if enabled is not None:
                state["enabled"] = enabled
            if not self._write(state):
                return False
            if not was_seen and enabled is None:
                self._quiet = True  # the run that shows the notice sends nothing
            return True

    @_safe()
    def notice_text(self) -> str | None:
        """The notice when it is due (telemetry on, notice not seen), else None."""
        state = self._load()
        if not self._decide(state)[0] or _seen(state):
            return None
        return (f"{self.product} shares usage stats with its author: how often it "
                "runs, which features are used, errors, version and OS, tied to a "
                "random install ID. It never sends your files, arguments or account "
                "names. Nothing has been sent yet. Turn it off with "
                f"`{self.off_command}` or `DO_NOT_TRACK=1`. Details: {NOTICE_URL}")

    @_safe(False)
    def show_notice(self, stream=None) -> bool:
        """Print the notice once (to stream, default stderr) when it is due and
        stderr is a terminal or an AI agent runs the process."""
        stream = sys.stderr if stream is None else stream
        # The notice goes to stderr, so only a terminal there counts.
        if stream is None or not (_is_tty(sys.stderr) or self.agent):
            return False
        text = self.notice_text()
        # Record it first: a notice that can't be recorded would print on every run.
        if not text or not self._mark_seen():
            return False
        stream.write(text + "\n")
        with contextlib.suppress(Exception):
            stream.flush()
        return True

    @_safe()
    def mark_notice_seen(self) -> None:
        """For apps, plugins and install scripts that showed the notice themselves."""
        state = self._load()
        if self._decide(state)[0] and not _seen(state):
            self._mark_seen()

    @_safe(False)
    def set_enabled(self, enabled: bool) -> bool:
        """`telemetry on` also marks the notice seen; `off` deletes id and counters.
        Returns whether the state file was written."""
        with self._lock:
            if enabled:
                return self._mark_seen(enabled=True)
            notice = self._load().get("notice", 0)
            return self._write({"enabled": False, "notice": notice})

    @_safe()
    def started(self) -> None:
        self._track("app_started", {})

    @_safe()
    def feature(self, name: str) -> None:
        name = name if isinstance(name, str) and name in self.features else "other"
        self._track("feature_used", {"feature": name}, name)

    @_safe()
    def error(self, exc_or_type, message: str | None = None) -> None:
        """An exception, an exception class or an error code, plus optional text.
        Pass message="" where the exception text could hold user content."""
        if isinstance(exc_or_type, BaseException):
            kind = type(exc_or_type).__name__
            message = str(exc_or_type) if message is None else message
        else:
            kind = getattr(exc_or_type, "__name__", None) or str(exc_or_type)
        kind = kind[:100] or "Error"
        props = {"error_type": kind}
        message = _scrub(str(message), *_home_and_names()) if message else ""
        if message:
            props["error_message"] = message
        self._track("error_occurred", props, kind)

    def flush(self, timeout: float = 0.5) -> None:
        """Wait up to `timeout` seconds for requests already in flight."""
        flush(timeout)

    @_safe()
    def flush_at_exit(self, timeout: float = 0.5) -> None:
        """Opt in to one flush when the interpreter exits."""
        atexit.register(flush, timeout)

    def status(self) -> dict:
        # The same keys as the other clients, so `status --json` reads alike.
        result = {"enabled": False, "debug": False, "reason": "error", "id": None,
                  "stateFile": self.state_file, "endpoint": self.endpoint}
        with contextlib.suppress(Exception):
            state = self._load()
            result["enabled"], result["reason"], result["debug"] = self._decide(state)
            result["id"] = _valid_id(state.get("id"))
        return result

    def _track(self, event: str, extra: dict, detail: str | None = None) -> None:
        with self._lock:
            state = self._load()
            on, _, debug = self._decide(state)
            if self._quiet or not on or not _seen(state):
                return
            dirty = False
            distinct_id = _valid_id(state.get("id"))
            if not distinct_id:
                distinct_id = state["id"] = str(uuid.uuid4())
                dirty = True
            events = []
            if state.get("installed") is not True:
                state["installed"] = dirty = True
                events.append(("app_installed", {}))
            now = _now()
            throttled = False
            if self.mode in THROTTLED_MODES:
                # At most once per UTC day for the event and its feature or error type.
                day = now.date().isoformat()
                key = f"{event}:{detail}" if detail else event
                daily = state.get("daily")
                daily = daily if isinstance(daily, dict) else {}
                daily = {k: v for k, v in daily.items() if v == day}  # drop older days
                throttled = daily.get(key) == day
                daily[key] = day
                state["daily"] = daily
                dirty = dirty or not throttled
            if not throttled:
                events.append((event, extra))
            # A state directory that cannot be written means nothing is sent.
            if not events or (dirty and not self._write(state)):
                return
        common = _common(self.product, self.version, self.channel, self.mode,
                         self.agent)
        for name, props in events:
            body = _body(name, distinct_id, now, {**common, **props})
            _send(self.endpoint, body, self.product, self.version, debug)


def create_telemetry(*, product: str, version: str, state_dir, channel: str,
                     env_prefix: str | None = None, mode: str | None = None,
                     features=(), off_command: str | None = None,
                     endpoint: str | None = None) -> Telemetry:
    """A client for one product. env_prefix defaults to the slug in upper case,
    off_command to "<product> telemetry off"; endpoint="" turns telemetry off."""
    return Telemetry(product, version, state_dir, channel, env_prefix=env_prefix,
                     mode=mode, features=features, off_command=off_command,
                     endpoint=endpoint)
