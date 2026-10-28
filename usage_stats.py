"""Usage stats: opt-out usage counts through the shared client in ``telemetry.py``.

Sent to the maintainer (PostHog through https://usage.sergeybaus.com): ``app_started`` when the gateway
starts, ``feature_used`` with a name from ``FEATURES``, and ``error_occurred`` with an error code only,
never its text. In the ``plugin`` mode each event goes at most once a UTC day per feature or code. Never
message text, transcripts, captions, names, chat, user or connection ids, paths, or settings values.

Nothing is sent until the owner has seen the notice. The gateway sends it once to an owner who manages
Settings, in their private chat with the bot, with a button that turns usage stats off; it is marked seen
only after Telegram accepted it, and that gateway process sends nothing until its next start. A terminal
run of ``hermes telesec`` prints it once to stderr, and ``hermes telesec telemetry status`` shows it.

Off switches: ``TELESEC_TELEMETRY=0``, ``DO_NOT_TRACK=1``, ``hermes telesec telemetry off``, and Usage
stats in Settings (``/telesec``). The client never raises, never blocks, and is off in CI and tests.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any

from . import history_support as _history
from . import owner_panel as _owner_panel
from . import telemetry as _telemetry

logger = logging.getLogger(__name__)

PRODUCT = "telesec"
ENV_PREFIX = "TELESEC"
ENV_NAME = f"{ENV_PREFIX}_TELEMETRY"
CHANNEL = "plugin"
MODE = "plugin"
OFF_COMMAND = "hermes telesec telemetry off"
DETAILS_URL = _telemetry.NOTICE_URL
# Handler-level names only; the client sends anything else as "other".
GATEWAY_FEATURES = (
    "voice transcribed", "video note transcribed", "audio file transcribed", "transcript cleanup",
    "history capture", "dossier update", "dossier post", "stats command",
    "owner panel", "panel mode", "panel chat switch", "panel erase", "panel stats", "panel dossier",
    "settings", "settings changed",
)
CLI_FEATURES = (
    "settings", "settings set",
    "mode", "mode list", "mode get", "mode set", "mode reset",
    "chat", "chat list", "chat get", "chat set", "chat scope", "chat scope clear",
    "history", *(f"history {name}" for name in ("chats", "contacts", "catalog", "stats", "show", "search", "deletions",
                                     "export", "import", "verify", "maintain", "forget")),
    "stats",
    "dossier", *(f"dossier {name}" for name in ("status", "enable", "disable", "list", "show", "export", "preview",
                                                "note", "run", "forget", "gate", "clairic")),
)
FEATURES = tuple(dict.fromkeys((*GATEWAY_FEATURES, *CLI_FEATURES)))
_TRANSCRIBED = {"voice": "voice transcribed", "video_note": "video note transcribed",
                "audio": "audio file transcribed", "audio_document": "audio file transcribed"}
_PANEL_SECTIONS = {"stats": "panel stats", "dossier": "panel dossier", "settings": "settings"}
# Settings buttons: "u.<target value>", like the other switches.
PANEL_ARGS = ("u.on", "u.off")
NOTICE_RETRY_SECONDS = 15 * 60
# Rules the environment decides; Telegram and `telemetry on|off` cannot change them.
ENV_REASONS = {"no-endpoint": "no endpoint", "do-not-track": "DO_NOT_TRACK", "env": ENV_NAME, "ci": "CI",
               "test": "a test run", "dev-version": "a development version"}
REASON_TEXT = {
    "default": "on by default",
    "state": "set with 'hermes telesec telemetry on|off' or Settings",
    "env": f"set by {ENV_NAME}",
    "do-not-track": "DO_NOT_TRACK is set",
    "ci": "off in CI",
    "test": "off in a test run",
    "dev-version": "off in a development version",
    "no-endpoint": "no endpoint in this build",
    "error": "the state file could not be read",
}

STRINGS = {
    "en": {
        "usage.status": "Usage stats: {state}",
        "usage.status.env": "Usage stats: {state} (set by {name})",
        "usage.on": "Share usage stats",
        "usage.off": "Don't share",
        "usage.done.on": "Usage stats are on.",
        "usage.done.off": "Usage stats are off.",
        "usage.env": "Usage stats are set by {name} on the gateway.",
        "usage.save_error": "Couldn't save the usage stats switch.",
        "usage.notice.off": "Turn off usage stats",
    },
    "ru": {
        "usage.status": "Статистика использования: {state}",
        "usage.status.env": "Статистика использования: {state} (задаёт {name})",
        "usage.on": "Делиться статистикой",
        "usage.off": "Не делиться",
        "usage.done.on": "Статистика использования включена.",
        "usage.done.off": "Статистика использования выключена.",
        "usage.env": "Статистику использования на этом сервере задаёт {name}.",
        "usage.save_error": "Не удалось сохранить переключатель статистики.",
        "usage.notice.off": "Выключить статистику",
    },
}

_client: _telemetry.Telemetry | None = None
# Gateway notice delivery: done once delivered or not due; one attempt at a time; a retry pause after a
# failed proactive attempt.
_notice_done = False
_notice_busy = False
_notice_next_try = 0.0
_tasks: set[asyncio.Task[Any]] = set()


def plugin_version() -> str:
    """The version in plugin.yaml next to this file; empty (telemetry off) when it can't be read."""
    try:
        text = Path(__file__).with_name("plugin.yaml").read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r"""^version:[ \t]*["']?([^"'\s#]+)""", text, re.MULTILINE)
    return match.group(1) if match else ""


def state_dir() -> Path:
    """The plugin's data directory, next to settings.json."""
    return _history.get_hermes_home() / "data" / PRODUCT


def create_client(*, version: str | None = None, directory: Path | None = None) -> _telemetry.Telemetry:
    return _telemetry.create_telemetry(
        product=PRODUCT, version=plugin_version() if version is None else version,
        state_dir=state_dir() if directory is None else directory, channel=CHANNEL, env_prefix=ENV_PREFIX,
        mode=MODE, features=FEATURES, off_command=OFF_COMMAND)


def setup(*, version: str | None = None, directory: Path | None = None) -> _telemetry.Telemetry | None:
    """Create this process's client once (``register()`` calls it). Never raises."""
    global _client
    try:
        if _client is None:
            register_strings()  # the notice's button needs them before Settings registers
            _client = create_client(version=version, directory=directory)
            _client.flush_at_exit()
    except Exception as exc:  # noqa: BLE001 - usage stats must never break plugin load
        logger.debug("%s: usage stats unavailable (%s)", _history.PLUGIN_NAME, type(exc).__name__)
    return _client


def reset() -> None:
    """Forget the client and the notice state (tests)."""
    global _client, _notice_done, _notice_busy, _notice_next_try
    _client, _notice_done, _notice_busy, _notice_next_try = None, False, False, 0.0


def client() -> _telemetry.Telemetry | None:
    return _client


# --- Events ----------------------------------------------------------------------------------------


def started() -> None:
    if _client is not None:
        _client.started()


def feature(name: str) -> None:
    if _client is not None:
        _client.feature(name)


def error(code: Any) -> None:
    """An error code or exception, sent as its type only: in Telesec an error's text can quote chat content."""
    if _client is None:
        return
    kind = code if isinstance(code, str) else type(code).__name__
    _client.error(kind, "")


def transcribed(media_label: str, *, cleaned: bool) -> None:
    feature(_TRANSCRIBED.get(media_label, "other"))
    if cleaned:
        feature("transcript cleanup")


def dossier_cycle(report: Any) -> None:
    """One dossier cycle: a refreshed dossier or persona, and the error codes of failed targets."""
    if _client is None:
        return
    if getattr(report, "refreshed", 0) or getattr(report, "persona", None) == "refreshed":
        feature("dossier update")
    for _label, kind in getattr(report, "failures", None) or ():
        error(f"dossier.{kind}")
    if getattr(report, "outcome", None) == "aborted_trust":
        error("dossier.trust_gate_denied")


def panel_action(action: str, arg: str | None, section: str | None, sub: str | None) -> None:
    """A tap on the owner panel; opening a section counts, paging inside one does not."""
    if action in ("m", "r"):
        feature("panel mode")
    elif action == "f" and arg == _owner_panel.ERASE_DO:
        feature("panel erase")
    elif action == "f" and arg not in _owner_panel.ERASE_ARGS:
        feature("panel chat switch")
    elif action == "s" and sub is None and section in _PANEL_SECTIONS:
        feature(_PANEL_SECTIONS[section])


# --- Settings in Telegram ------------------------------------------------------------------------


def register_strings() -> None:
    _owner_panel.register_strings(STRINGS)


def _status() -> dict[str, Any]:
    if _client is None:
        return {"enabled": False, "reason": "error"}
    return _client.status()


def panel_lines(language_code: str | None) -> list[str]:
    """The Usage stats line of Settings; none without a client (``register()`` never ran)."""
    if _client is None:
        return []
    current = _status()
    state = _owner_panel.tr(f"settings.state.{'on' if current['enabled'] else 'off'}", language_code)
    name = ENV_REASONS.get(current["reason"])
    if name is not None:
        return [_owner_panel.tr("usage.status.env", language_code, state=state, name=name)]
    return [_owner_panel.tr("usage.status", language_code, state=state)]


def panel_row(language_code: str | None, chat_id: Any, section_key: str) -> list[tuple[str, str]] | None:
    """The Share / Don't share row, or None while the environment decides (or there is no client)."""
    current = _status()
    if _client is None or current["reason"] in ENV_REASONS or current["reason"] == "error":
        return None
    on = bool(current["enabled"])
    mark = "• "
    return [
        ((mark if on else "") + _owner_panel.tr("usage.on", language_code),
         _owner_panel.section_callback(chat_id, section_key, "u.on")),
        ((mark if not on else "") + _owner_panel.tr("usage.off", language_code),
         _owner_panel.section_callback(chat_id, section_key, "u.off")),
    ]


def panel_apply(arg: str, language_code: str | None) -> str:
    """Store the Settings switch; returns the notice line. Blocking. ``u.on`` also marks the notice seen."""
    current = _status()
    name = ENV_REASONS.get(current["reason"])
    if name is not None:
        return _owner_panel.tr("usage.env", language_code, name=name)
    on = arg == "u.on"
    if _client is None or not _client.set_enabled(on):
        logger.warning("%s: could not save the usage stats switch", _history.PLUGIN_NAME)
        return _owner_panel.tr("usage.save_error", language_code)
    logger.info("%s: usage stats switched enabled=%s via=telegram", _history.PLUGIN_NAME, on)
    return _owner_panel.tr(f"usage.done.{'on' if on else 'off'}", language_code)


# --- The notice in Telegram ----------------------------------------------------------------------


def notice_html(text: str) -> str:
    """The client's notice as Telegram HTML: `code` spans become <code>."""
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", html.escape(text, quote=False))


def _notice_markup(language_code: str | None) -> Any:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    data = _owner_panel.section_callback(_owner_panel.HOME_CHAT, _owner_panel.SETTINGS_SECTION, "u.off")
    return InlineKeyboardMarkup([[InlineKeyboardButton(_owner_panel.tr("usage.notice.off", language_code),
                                                       callback_data=data)]])


def _no_link_preview() -> dict[str, Any]:
    try:
        from telegram import LinkPreviewOptions
    except ImportError:  # python-telegram-bot before 20.8
        return {"disable_web_page_preview": True}
    return {"link_preview_options": LinkPreviewOptions(is_disabled=True)}


def update_connection(update: Any) -> Any:
    """The Business connection id of a Business update, else None."""
    for attribute in ("business_message", "edited_business_message", "deleted_business_messages"):
        payload = getattr(update, attribute, None)
        if payload is not None:
            return _history._get(payload, "business_connection_id")
    return None


def offer_notice(bot: Any, connection: Any, *, owner_id: Any = None, language_code: str | None = None,
                 interactive: bool = False) -> None:
    """Send the notice once to the owner of ``connection`` when it is due (gateway). Never raises.

    ``owner_id`` is a verified owner (the panel or Settings), else the connection's owner is looked up. Only an
    owner who manages Settings (an owner of every known connection) gets it, since they are the one who can
    turn usage stats off there. A failed proactive attempt waits ``NOTICE_RETRY_SECONDS``; an owner's own
    panel or Settings tap (``interactive``) tries again at once.
    """
    global _notice_busy
    try:
        if _client is None or _notice_done or _notice_busy or not connection:
            return
        if not interactive and time.monotonic() < _notice_next_try:
            return
        loop = asyncio.get_running_loop()
        _notice_busy = True
        task = loop.create_task(_deliver(bot, str(connection), owner_id, language_code))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    except Exception as exc:  # noqa: BLE001 - the notice must never break dispatch
        _notice_busy = False
        logger.debug("%s: usage stats notice not scheduled (%s)", _history.PLUGIN_NAME, type(exc).__name__)


async def _deliver(bot: Any, connection: str, owner_id: Any, language_code: str | None) -> bool:
    global _notice_done, _notice_busy, _notice_next_try
    try:
        client_ = _client
        text = await asyncio.to_thread(client_.notice_text) if client_ is not None else None
        if text is None:
            _notice_done = True  # off, or already seen
            return False
        if owner_id is None:
            owner_id, _reason = await _history.resolve_business_owner(bot, connection)
        from . import dossier_panel  # lazy: dossier_panel imports most of the plugin

        if owner_id is None or not await asyncio.to_thread(dossier_panel.manages_global, owner_id, connection):
            _notice_next_try = time.monotonic() + NOTICE_RETRY_SECONDS
            return False
        await bot.send_message(chat_id=int(str(owner_id)), text=notice_html(text), parse_mode="HTML",
                               reply_markup=_notice_markup(language_code), **_no_link_preview())
        # Seen only once Telegram accepted it. This process then sends nothing until the gateway restarts.
        await asyncio.to_thread(client_.mark_notice_seen)
        _notice_done = True
        logger.info("%s: usage stats notice sent to the owner", _history.PLUGIN_NAME)
        return True
    except Exception as exc:  # noqa: BLE001 - e.g. the owner never started the bot; tried again later
        _notice_next_try = time.monotonic() + NOTICE_RETRY_SECONDS
        logger.warning("%s: usage stats notice not delivered (%s)", _history.PLUGIN_NAME, type(exc).__name__)
        return False
    finally:
        _notice_busy = False


# --- CLI: hermes telesec telemetry [status|on|off] [--format json] -------------------------------


def add_parser(subs: Any) -> None:
    parser = subs.add_parser("telemetry", help="Show or change usage stats (on unless turned off)")
    parser.add_argument("action", nargs="?", choices=("status", "on", "off"), default="status")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--json", dest="format", action="store_const", const="json", help="Same as --format json")
    parser.set_defaults(_telemetry_action=True)


def cli_feature(args: argparse.Namespace) -> str | None:
    """The subcommand path without arguments (``mode set``); None for ``telemetry`` and bare ``telesec``."""
    command = getattr(args, "telesec_command", None)
    if command in (None, "telemetry"):
        return None
    if command == "settings":
        return "settings set" if getattr(args, "_settings_action", None) == "set" else "settings"
    action = {"mode": "_mode_action", "chat": "_chat_action", "history": "_history_action"}.get(command)
    if action is not None:
        value = getattr(args, action, None)
        return f"{command} {value.replace('_', ' ')}" if value else command
    if command == "dossier":
        value = getattr(args, "_dossier_action", None)
        return "dossier" if value in (None, "usage") else f"dossier {value}"
    return command  # "stats"; a name off the list is sent as "other"


def before_cli(args: argparse.Namespace) -> None:
    """Print the notice once in a terminal (stderr), then count the subcommand. Never raises."""
    if _client is None:
        return
    _client.show_notice()
    name = cli_feature(args)
    if name is not None:
        feature(name)


def _status_lines(current: dict[str, Any]) -> list[str]:
    state = "on" if current["enabled"] else "off"
    lines = [f"usage stats: {state} ({REASON_TEXT.get(current['reason'], current['reason'])})",
             f"install id: {current['id'] or 'none yet'}",
             f"state file: {current['stateFile'] or '-'}",
             f"endpoint: {current['endpoint'] or '-'}"]
    if current.get("debug"):
        lines.append("debug: events are printed to stderr and not sent")
    return lines


def _footer() -> str:
    return (f"What is sent: {DETAILS_URL}\n"
            f"Turn off: '{OFF_COMMAND}', {ENV_NAME}=0 or DO_NOT_TRACK=1 in the gateway's environment, or Usage "
            "stats in Settings (/telesec in your private chat with the bot).")


def handle_cli(args: argparse.Namespace) -> int:
    client_ = _client or setup()
    if client_ is None:
        print("error: usage stats are unavailable")
        return 1
    action = getattr(args, "action", None) or "status"
    as_json = getattr(args, "format", None) == "json"
    if action in ("on", "off"):
        on = action == "on"
        if not client_.set_enabled(on):
            print(f"error: could not write {client_.status()['stateFile'] or 'the state file'}")
            return 1
        current = client_.status()
        if as_json:
            print(json.dumps(current, ensure_ascii=True, indent=2))
            return 0
        print(f"usage stats: {action}" + ("" if on else "; the install id and daily counters are deleted"))
        if current["enabled"] is not on:
            print(f"note: {REASON_TEXT.get(current['reason'], current['reason'])}, so this environment keeps them "
                  f"{'off' if current['enabled'] is False else 'on'}.")
        print("The gateway applies it at its next event; no restart is needed.")
        return 0
    current = client_.status()
    notice = client_.notice_text()
    if as_json:
        print(json.dumps({**current, "notice": notice}, ensure_ascii=True, indent=2))
        return 0
    lines = _status_lines(current)
    if notice:
        lines += ["", notice]
    lines += ["", _footer()]
    print("\n".join(lines))
    if notice and (_reads_stdout() or client_.agent):
        client_.mark_notice_seen()  # shown in full to a person in a terminal or to an AI agent
    return 0


def _reads_stdout() -> bool:
    """Whether stdout is a terminal: a script, pipe, or ssh without a terminal has not shown anyone the notice."""
    try:
        return bool(sys.stdout.isatty())
    except Exception:  # noqa: BLE001 - a closed or replaced stdout
        return False
