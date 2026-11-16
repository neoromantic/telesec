"""Group media: voice notes, video notes and audio in Telegram groups listed in ``group_chats``.

Helpers for the group handler in ``__init__.py``: the configured chats, who may be transcribed, whether a
recording is a request for Hermes, forum topic routing, the handoff prompt, and a small state file. The handler,
the shared transcription and the transcript sends live in ``__init__.py``.

``$HERMES_HOME/data/telesec/group-media.json`` keeps one entry per source recording for 24 hours: its key (bot,
chat and message ids) and stage, never text. A restart turns unfinished entries into ``interrupted``, and a
recording that has an entry is never processed again. Stdlib only; PTB objects are read by attribute.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

from . import history_support as _history
from . import transcription_modes as _modes

logger = logging.getLogger(__name__)

CONFIG_KEY = "group_chats"
GENERAL_TOPIC = "1"  # Hermes's thread id for a forum's General topic; sends there carry no message_thread_id
VOICE_MAX_BYTES = 20 * 1024 * 1024  # the Bot API getFile limit; audio files keep their own limits
MAX_TRANSCRIPTIONS = 2
STATE_FILENAME = "group-media.json"
STATE_TTL_SECONDS = 24 * 60 * 60
STATE_MAX_ENTRIES = 2000
# Stages: started, published, handoff_submitted, then one of these final ones.
FINAL_STAGES = frozenset({"done", "withheld", "handoff_failed", "interrupted"})
HANDOFF_NOTE = "The voice transcript is already posted in the chat. Do not repeat it; do what it asks."
_CHAT_ID_RE = re.compile(r"-[1-9][0-9]*")

_config_warned = False
_trigger_warned = False
_state_lock = threading.Lock()
_state: dict[str, dict[str, Any]] | None = None


# --- Configuration ---------------------------------------------------------------------------------


def parse_group_chats(raw: Any) -> frozenset[int] | None:
    """The listed group chat ids; None when an entry is not a group chat id like ``-1001234567890``.

    Builds a new set: Hermes returns its cached config, which must never change.
    """
    if raw is None:
        return frozenset()
    if not isinstance(raw, list):
        return None
    chats = set()
    for entry in raw:
        if isinstance(entry, bool) or not isinstance(entry, int | str) or not _CHAT_ID_RE.fullmatch(str(entry)):
            return None
        chats.add(int(entry))
    return frozenset(chats)


def configured_chats(get_config: Any) -> frozenset[int]:
    """The chats from ``plugins.entries.telesec.settings.group_chats``; empty (off) when the list is malformed."""
    global _config_warned
    chats = parse_group_chats(get_config(CONFIG_KEY, []))
    if chats is not None:
        return chats
    if not _config_warned:
        _config_warned = True
        logger.warning("%s: %s must be a list of group chat IDs (negative numbers); group transcription is off",
                       _history.PLUGIN_NAME, CONFIG_KEY)
    return frozenset()


# --- Senders, addressing and topics ----------------------------------------------------------------


def human_sender(message: Any) -> Any | None:
    """The member who sent the message; None for bots (this one included), anonymous admins, channel posts and
    automatic forwards: Telesec leaves those to Hermes."""
    user = message.from_user
    if user is None or user.is_bot or message.sender_chat is not None or message.is_automatic_forward:
        return None
    return user


def real_reply(message: Any) -> Any | None:
    """The message this one replies to; None for a forum topic's root, which Telegram sets on every topic message."""
    reply = message.reply_to_message
    if reply is None or (message.is_topic_message and reply.message_id == message.message_thread_id):
        return None
    return reply


def _caption_targets(message: Any) -> tuple[set[str], set[int]]:
    """Handles named in the caption (``@name`` and ``/command@name``, lowercased) and ids of text mentions."""
    handles: set[str] = set()
    users: set[int] = set()
    for entity, text in message.parse_caption_entities().items():
        if entity.type == "mention":
            handles.add(text.removeprefix("@").casefold())
        elif entity.type == "bot_command" and "@" in text:
            handles.add(text.partition("@")[2].casefold())
        elif entity.type == "text_mention" and entity.user is not None:
            users.add(entity.user.id)
    return handles, users


def is_addressed(message: Any, bot_id: int, username: str | None) -> bool:
    """The caption mentions this bot (``@bot``, a text mention, ``/command@bot``), or the message is a real reply to
    one of its messages and the caption names no other bot (Hermes checks other bots' handles before the reply).
    Spoken words never count, and a forward is never addressed."""
    if message.forward_origin is not None:
        return False
    handles, users = _caption_targets(message)
    if bot_id in users or (username and username.casefold() in handles):
        return True
    reply = real_reply(message)
    return (reply is not None and reply.from_user is not None and reply.from_user.id == bot_id
            and not names_other_bot(message, username))


def names_other_bot(message: Any, username: str | None) -> bool:
    """The caption names another bot (a handle ending in "bot")."""
    own = (username or "").casefold()
    return any(handle.endswith("bot") and handle != own for handle in _caption_targets(message)[0])


def answers_all_text(adapter: Any, message: Any) -> bool:
    """Hermes answers every text in this chat or topic: ``require_mention`` off, a free-response chat or topic.

    Hermes 0.22.6 has no public API for this, so it is read through the Telegram adapter's own getters, here
    only. A missing getter or an error means mentions are required.
    """
    global _trigger_warned
    try:
        return bool(not adapter._telegram_require_mention()
                    or str(message.chat.id) in adapter._telegram_free_response_chats()
                    or adapter._telegram_is_free_response_topic(message))
    except Exception as exc:  # noqa: BLE001 - an unknown adapter means the safe default
        if not _trigger_warned:
            _trigger_warned = True
            logger.warning("%s: Hermes trigger settings unreadable (%s); group recordings need a mention",
                           _history.PLUGIN_NAME, type(exc).__name__)
        return False


def topic_binding(adapter: Any, message: Any, thread: str | None) -> tuple[Any, Any]:
    """``(chat_topic, auto_skill)`` of a forum topic bound in Hermes's ``group_topics``, read through the Telegram
    adapter's own resolver, as for a typed message. A missing resolver or an error means no binding."""
    if thread is None:
        return None, None
    try:
        chat_topic, skill = adapter._resolve_topic_binding(message, "group", thread)
    except Exception as exc:  # noqa: BLE001 - an unknown adapter means no topic binding
        logger.info("%s: Hermes topic binding unreadable (%s)", _history.PLUGIN_NAME, type(exc).__name__)
        return None, None
    return chat_topic, skill


def is_request(message: Any, *, adapter: Any, bot_id: int, username: str | None) -> bool:
    """Hand the recording to Hermes: it is addressed to this bot, or Hermes answers every text here and the caption
    names no other bot. Otherwise it only gets a transcript."""
    if is_addressed(message, bot_id, username):
        return True
    return answers_all_text(adapter, message) and not names_other_bot(message, username)


def reply_expected(message: Any, bot_id: int, username: str | None) -> bool | None:
    """Hermes's ``reply_expected`` for a group request: True when addressed, False for a reply to another member
    (chatter the assistant may leave unanswered), else None."""
    if is_addressed(message, bot_id, username):
        return True
    reply = real_reply(message)
    if reply is not None and reply.from_user is not None and reply.sender_chat is None:
        return False
    return None


def thread_id(message: Any) -> str | None:
    """Hermes's routing thread for a group message: the forum topic, ``"1"`` for General, None outside forums."""
    forum = message.chat.is_forum is True
    raw = message.message_thread_id
    if raw is not None:
        return str(raw) if forum or message.is_topic_message else None
    return GENERAL_TOPIC if forum else None


def send_thread_kwargs(message: Any) -> dict[str, int]:
    """``message_thread_id`` for a send into the message's forum topic; none for General and outside forums."""
    topic = thread_id(message)
    return {} if topic in (None, GENERAL_TOPIC) else {"message_thread_id": int(topic)}


def identity(bot_id: Any, message: Any) -> tuple[str, str, str, str]:
    """One source recording, for in-process duplicate suppression and the state file."""
    return "group", str(bot_id), str(message.chat.id), f"message:{message.message_id}"


# --- Handoff text ----------------------------------------------------------------------------------


def caption_text(message: Any, bot_id: int, username: str | None) -> str:
    """The typed caption, without this bot's ``@handle`` when it names no one else (as Hermes cleans a trigger)."""
    caption = (message.caption or "").strip()
    if not caption or not username:
        return caption
    handles, users = _caption_targets(message)
    if handles - {username.casefold()} or users - {bot_id}:
        return caption
    return re.sub(rf"(?i)@{re.escape(username)}\b[,:\-]*\s*", "", caption).strip()


def reply_context(message: Any) -> tuple[str | None, str | None]:
    """``(reply_to_message_id, reply_to_text)`` of a real reply, as Hermes reads them: the quoted part first."""
    reply = real_reply(message)
    if reply is None:
        return None, None
    quote = message.quote.text if message.quote is not None else None
    return str(reply.message_id), quote or reply.text or reply.caption or None


def channel_prompt(configured: str | None, username: str | None) -> str:
    """The chat's own channel prompt, Hermes's group identity line, and the note that the transcript is posted."""
    identity_line = (
        f"Your Telegram bot username in this group: @{username.lower()}. "
        "Mentions of other bots are not requests for you to relay the message."
    ) if username else None
    return "\n\n".join(part for part in (configured, identity_line, HANDOFF_NOTE) if part)


# --- State file ------------------------------------------------------------------------------------


def state_path() -> Path:
    return _history.get_hermes_home() / "data" / _history.PLUGIN_NAME / STATE_FILENAME


def _read_state() -> dict[str, dict[str, Any]]:
    path = state_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        entries = {str(key): {"stage": str(item["stage"]), "ts": float(item["ts"])} for key, item in raw.items()}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        logger.warning("%s: group media state unreadable (%s); starting empty", _history.PLUGIN_NAME,
                       type(exc).__name__)
        return {}
    now = time.time()
    interrupted = [key for key, item in entries.items() if item["stage"] not in FINAL_STAGES]
    for key in interrupted:
        entries[key] = {"stage": "interrupted", "ts": now}
    if interrupted:
        logger.warning("%s: %d group recording(s) were interrupted by a restart; they are not processed again",
                       _history.PLUGIN_NAME, len(interrupted))
        _write_state(entries)
    return entries


def _write_state(entries: dict[str, dict[str, Any]]) -> None:
    cutoff = time.time() - STATE_TTL_SECONDS
    for key in [key for key, item in entries.items() if item["ts"] < cutoff]:
        del entries[key]
    for key in sorted(entries, key=lambda key: entries[key]["ts"])[:max(0, len(entries) - STATE_MAX_ENTRIES)]:
        del entries[key]
    path = state_path()
    _history._ensure_private_dir(path.parent)
    _modes._write_private_json_atomic(path, entries)


def _entries() -> dict[str, dict[str, Any]]:
    global _state
    if _state is None:
        _state = _read_state()
    return _state


def begin(key: str) -> bool:
    """Record a new source as started; False when it already has an entry (handled, running or interrupted)."""
    with _state_lock:
        entries = _entries()
        item = entries.get(key)
        if item is not None and item["ts"] >= time.time() - STATE_TTL_SECONDS:
            return False
        entries[key] = {"stage": "started", "ts": time.time()}
        _write_state(entries)
        return True


def set_stage(key: str, stage: str) -> None:
    with _state_lock:
        entries = _entries()
        entries[key] = {"stage": stage, "ts": time.time()}
        _write_state(entries)
