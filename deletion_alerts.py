"""Deletion alerts: tell the owner, in their private chat with the bot, that a contact deleted a message.

History classifies each deletion about two minutes after Telegram reports it (``history_support``). A
deletion explained by a later message, a likely correction or a likely duplicate, stays quiet, and so does
a deletion of the owner's own message. Any other deletion of the contact's message sends one alert: the
delivery is stored in history, so a restart never repeats a successful alert. The alert names the contact
and the class;
the deleted text shows only when the owner taps "Show text", read from history at that moment.

Alerts are on by default, need history (deletions are found in saved history), and can be turned off in
Settings for all chats or in a chat's panel for that chat. Nothing here is posted into a Business chat, and
the alert and its buttons never reach the Hermes agent.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from . import chat_settings as _chat_settings
from . import global_settings as _settings
from . import history_support as _history
from . import owner_panel as _owner_panel
from . import transcription_modes as _modes

logger = logging.getLogger(__name__)

# A later message replaced the deleted one: an edit by delete-and-resend, or a double send.
QUIET_CLASSES = frozenset({"likely_correction", "likely_duplicate"})
LANGUAGES_MAX = 1000

STRINGS = {
    "en": {
        "alert.title": "🔔 Deleted message: {chat}",
        "alert.class.unexplained": "Unexplained: no later message replaced it.",
        "alert.class.unclassifiable": "Its text was not saved.",
        "alert.show": "👁 Show text",
        "alert.panel": "Chat panel",
        "alert.gone": "The text is no longer saved",
    },
    "ru": {
        "alert.title": "🔔 Удалённое сообщение: {chat}",
        "alert.class.unexplained": "Без объяснения: позже его ничем не заменили.",
        "alert.class.unclassifiable": "Его текст не был сохранён.",
        "alert.show": "👁 Показать текст",
        "alert.panel": "Панель чата",
        "alert.gone": "Текст больше не сохранён",
    },
}

_loop: asyncio.AbstractEventLoop | None = None
_bot: Any = None
_in_flight: set[str] = set()
# Telegram user id -> the client language last seen for that user, so an alert speaks the owner's language.
_languages: dict[str, str] = {}


def register_strings() -> None:
    _owner_panel.register_strings(STRINGS)


def bind(bot: Any) -> None:
    """Bind the initialized gateway and replay pending delivery without new Business traffic."""
    global _loop, _bot
    _loop, _bot = asyncio.get_running_loop(), bot
    _loop.create_task(asyncio.to_thread(_history.set_deletion_listener, on_classified))


def note_language(user: Any) -> None:
    user_id, code = _history._get(user, "id"), _history._get(user, "language_code")
    if user_id is not None and isinstance(code, str) and code:
        _languages.pop(str(user_id), None)
        _languages[str(user_id)] = code
        while len(_languages) > LANGUAGES_MAX:
            _languages.pop(next(iter(_languages)))


def should_alert(record: dict[str, Any]) -> bool:
    """A contact's message deleted and not explained by a later message."""
    return record.get("direction") == "inbound" and record.get("classification") not in QUIET_CLASSES


def on_classified(record: dict[str, Any], original: Any) -> None:
    """History listener: runs under the chat's writer lock, so it only schedules the alert."""
    loop, bot = _loop, _bot
    if loop is None or bot is None or loop.is_closed() or not should_alert(record):
        return
    event_id = record["event_id"]
    if event_id in _in_flight:
        return
    _in_flight.add(event_id)
    asyncio.run_coroutine_threadsafe(send(bot, record, has_text=bool(original is not None and original.text)), loop)


def render(record: dict[str, Any], label: str | None, *, has_text: bool, language_code: str | None,
           bot_username: str | None) -> tuple[str, list[list[dict[str, str]]]]:
    """The alert text and its button rows (Telegram InlineKeyboardButton fields)."""
    chat = record["chat_id"]
    title = _owner_panel.tr("alert.title", language_code, chat=_owner_panel.chat_label_text(chat, label))
    kind = "unexplained" if record.get("classification") == "unexplained" else "unclassifiable"
    text = f"{title}\n{_owner_panel.tr(f'alert.class.{kind}', language_code)}"
    row = []
    if has_text:
        row.append({"text": _owner_panel.tr("alert.show", language_code),
                    "callback_data": _owner_panel.callback_data("x", chat, str(record["message_id"]))})
    if bot_username:
        row.append({"text": _owner_panel.tr("alert.panel", language_code),
                    "url": f"https://t.me/{bot_username}?start=bizChat{chat}"})
    return text, [row] if row else []


def _bot_username(bot: Any) -> str | None:
    try:
        username = getattr(bot, "username", None)
    except Exception:  # noqa: BLE001 - PTB raises before the bot is initialized
        return None
    return username if isinstance(username, str) and username else None


def _chat_label(business_connection_id: str, chat_id: str) -> str | None:
    """The contact's name from the history catalog; never message content."""
    for entry in _modes._history_entries():
        if (str(entry.get("business_connection_id")) == business_connection_id
                and str(entry.get("chat_id")) == chat_id and isinstance(entry.get("current_profile"), dict)):
            return _modes.build_chat_label(entry["current_profile"])
    return None


def markup(rows: list[list[dict[str, str]]]) -> Any:
    if not rows:
        return None
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup([[InlineKeyboardButton(**button) for button in row] for row in rows])


async def send(bot: Any, record: dict[str, Any], *, has_text: bool) -> None:
    """Send one alert to the owner's private chat with the bot, after checking the switches again."""
    connection, chat = str(record["business_connection_id"]), str(record["chat_id"])
    try:
        owner, reason = await _history.resolve_business_owner(bot, connection)
        if owner is None:
            logger.info("%s: deletion alert skipped, owner unknown (%s)", _history.PLUGIN_NAME, reason)
            return
        if _settings.alerts_active() and not await asyncio.to_thread(
            _chat_settings.paused, connection, chat, "alerts", owner
        ):
            label = await asyncio.to_thread(_chat_label, connection, chat)
            text, rows = render(record, label, has_text=has_text, language_code=_languages.get(owner),
                                bot_username=_bot_username(bot))
            await bot.send_message(chat_id=int(owner), text=text, reply_markup=markup(rows))
            logger.info("%s: deletion alert sent chat=%s class=%s", _history.PLUGIN_NAME, _history._safe_part(chat),
                        record.get("classification"))
        chat_dir = _history._history_chat_dir_path(connection, chat)

        def mark_handled(state: Any, appended: list[dict[str, Any]]) -> None:
            pending = state.pending_deletions.get(record["deleted_event_id"])
            if pending is None or pending.alert_handled:
                return
            handled = {**record, "event_type": "deletion.alert_handled", "observed_at": _history._isoformat_utc(_history._utcnow())}
            handled.pop("alert_pending")
            handled["event_id"] = _history._event_id(handled)
            _history._append_record_to_state(chat_dir, state, handled, appended_records=appended)

        await asyncio.to_thread(_history._mutate_chat_history_transactionally, chat_dir,
                                dirty_reason="alert_delivery_refresh_failed", mutate=mark_handled)
    except Exception as exc:  # noqa: BLE001 - a lost alert must not break anything else
        logger.warning("%s: deletion alert failed (%s)", _history.PLUGIN_NAME, type(exc).__name__)
    finally:
        _in_flight.discard(record["event_id"])


def saved_text(connections: tuple[str, ...], chat_id: str, message_id: str) -> str | None:
    """The deleted message's text from the owner's history of this chat. Blocking."""
    for connection in connections:
        text = _history.deleted_message_text(connection, chat_id, message_id)
        if text:
            return text
    return None
