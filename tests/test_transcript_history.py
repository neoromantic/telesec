"""Transcripts Telesec delivers reach history without Telegram's copy, which the bot never gets."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta

import pytest
import test_history
from test_history import load_records, make_business_text_update, set_switch
from test_plugin import RAW_FIRST_CLEAN, RAW_FIRST_RAW, _raw_first_setup

plugin = test_history.plugin  # fixtures
enabled_history = test_history.enabled_history

RAW = f"🎙️ {RAW_FIRST_RAW}"
CLEAN = f"🎙️ {RAW_FIRST_CLEAN}"
BASE = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)


def _setup(**kwargs):
    message, event, bot, gateway = _raw_first_setup(**kwargs)
    message.chat.type = "private"
    return message, bot, gateway.adapters[event.source.platform]


async def _publish(module, message, bot, adapter, *, transcript=RAW_FIRST_RAW, cleaned=RAW_FIRST_CLEAN):
    timings: dict = {}
    await module._publish_transcript(bot=bot, adapter=adapter, message=message, transcript=transcript,
                                     cleanup_fn=lambda _text: cleaned, llm=None, timings=timings)
    return timings


def _texts(module):
    return [(r["event_type"], r["source"], r["message_id"], r.get("text"), r.get("edit_origin"))
            for r in load_records(module)]


def _stats(module):
    cs = module.correspondence_stats
    window = cs.build_window(argparse.Namespace(since=None, until=None, tz="UTC", session_gap=None),
                             now=datetime.now(UTC) + timedelta(days=1))
    entry = {"business_connection_id": "business-123", "chat_id": 991, "chat_type": "private"}
    return cs.compute_stats(entry, load_records(module), window)


def _dossier_view(module):
    dossiers = module._dossier_support
    entry = {"business_connection_id": "business-123", "chat_id": 991, "chat_type": "private"}
    return dossiers.read_chat_view(entry, since=datetime(2000, 1, 1, tzinfo=UTC)).messages


@pytest.mark.asyncio
async def test_reply_transcript_and_its_cleanup_are_saved_without_any_echo(enabled_history):
    module = enabled_history
    message, bot, adapter = _setup()

    timings = await _publish(module, message, bot, adapter)

    assert timings["cleanup"] == "edited"
    assert _texts(module) == [
        ("message.created", "transcript", 500, RAW, None),
        ("message.edited", "transcript", 500, CLEAN, "transcript_cleanup"),
    ]
    created, edited = load_records(module)
    assert (created["direction"], created["reply_to_message_id"]) == ("outbound", 77)
    assert edited["reply_to_message_id"] == 77
    stats = _stats(module)
    assert (stats.messages.received, stats.messages.sent, stats.edited.total) == (0, 0, 0)
    view = _dossier_view(module)
    assert (view["500"].label, view["500"].text) == ("transcript", CLEAN)


@pytest.mark.asyncio
async def test_a_failed_cleanup_edit_keeps_the_raw_transcript_only(enabled_history):
    module = enabled_history
    message, bot, adapter = _setup()
    bot.text_edit_error = RuntimeError("Bad Request: message can not be edited")

    timings = await _publish(module, message, bot, adapter)

    assert timings["cleanup"] == "edit_failed"
    assert _texts(module) == [("message.created", "transcript", 500, RAW, None)]


@pytest.mark.asyncio
async def test_a_failed_send_saves_nothing(enabled_history):
    module = enabled_history
    message, bot, adapter = _setup()
    bot.send_error = RuntimeError("Forbidden: bot was blocked")

    with pytest.raises(RuntimeError):
        await _publish(module, message, bot, adapter)

    assert load_records(module) == []


@pytest.mark.asyncio
async def test_a_late_telegram_copy_adds_nothing(enabled_history):
    module = enabled_history
    message, bot, adapter = _setup()
    await _publish(module, message, bot, adapter)
    before = load_records(module)

    observe = module._history_support.observe_ptb_update
    await observe(make_business_text_update(text=RAW, message_id=500, update_id=7, from_user_id=1000), bot=bot)
    await observe(make_business_text_update(text=CLEAN, message_id=500, update_id=8, from_user_id=1000,
                                            edited=True), bot=bot)

    assert load_records(module) == before
    assert _stats(module).edited.total == 0


@pytest.mark.asyncio
async def test_an_early_telegram_copy_is_not_saved_twice(enabled_history):
    module = enabled_history
    message, bot, adapter = _setup()
    send = bot.send_message

    async def send_with_copy(**kwargs):
        sent = await send(**kwargs)
        await module._history_support.observe_ptb_update(make_business_text_update(
            text=kwargs["text"], message_id=sent.message_id, update_id=6, from_user_id=1000), bot=bot)
        return sent

    bot.send_message = send_with_copy
    bot.text_edit_error = RuntimeError("Bad Request: message can not be edited")
    await _publish(module, message, bot, adapter)

    assert [(event, source) for event, source, *_ in _texts(module)] == [("message.created", "business_message")]


@pytest.mark.asyncio
async def test_cleanup_never_overwrites_an_owner_edit_saved_first(enabled_history):
    module = enabled_history
    message, bot, adapter = _setup()

    async def edit_then_owner(**kwargs):
        bot.text_edit_calls.append(kwargs)
        # Telegram applied the cleanup, then the owner edited the reply before the API answered.
        await module._history_support.observe_ptb_update(make_business_text_update(
            text="🎙️ Owner's own words", message_id=500, update_id=9, from_user_id=1000, edited=True), bot=bot)
        return True

    bot.edit_message_text = edit_then_owner
    await _publish(module, message, bot, adapter)

    records = load_records(module)
    assert [(r["source"], r.get("text")) for r in records] == [
        ("transcript", RAW), ("edited_business_message", "🎙️ Owner's own words")]
    assert _dossier_view(module)["500"].text == "🎙️ Owner's own words"


@pytest.mark.asyncio
async def test_an_owner_edit_during_cleanup_cancels_it_and_stays(enabled_history):
    module = enabled_history
    message, bot, adapter = _setup()

    def cleanup(_text):
        update = make_business_text_update(text="🎙️ My words", message_id=500, update_id=9, from_user_id=1000,
                                           edited=True)
        module._note_transcript_target_edit(update)
        return RAW_FIRST_CLEAN

    observed = []

    async def cleanup_after_capture(text):
        observed.append(await module._history_support.observe_ptb_update(make_business_text_update(
            text="🎙️ My words", message_id=500, update_id=9, from_user_id=1000, edited=True), bot=bot))
        return cleanup(text)

    timings: dict = {}
    await module._publish_transcript(bot=bot, adapter=adapter, message=message, transcript=RAW_FIRST_RAW,
                                     cleanup_fn=cleanup_after_capture, llm=None, timings=timings)

    assert timings["cleanup"] == "owner_edited" and observed == [True]
    assert [r.get("text") for r in load_records(module)] == [RAW, "🎙️ My words"]


@pytest.mark.asyncio
async def test_history_off_or_paused_saves_no_transcript(enabled_history):
    module = enabled_history
    message, bot, adapter = _setup()
    set_switch(module, "history", False)
    await _publish(module, message, bot, adapter)
    assert load_records(module) == []

    set_switch(module, "history", True)
    module.chat_settings.default_store().set_flag("1000", 991, "history", False, via="chat")
    await _publish(module, message, bot, adapter)
    assert load_records(module) == []


@pytest.mark.asyncio
async def test_an_erase_during_cleanup_is_not_undone(enabled_history):
    module = enabled_history
    message, bot, adapter = _setup()
    history = module._history_support

    def erase_then_clean(_text):
        assert history.erase_chat_history("business-123", 991) is not None
        return RAW_FIRST_CLEAN

    timings: dict = {}
    await module._publish_transcript(bot=bot, adapter=adapter, message=message, transcript=RAW_FIRST_RAW,
                                     cleanup_fn=erase_then_clean, llm=None, timings=timings)

    assert timings["cleanup"] == "edited"
    assert not history._history_chat_dir_path("business-123", 991).exists()


@pytest.mark.asyncio
async def test_an_owner_voice_caption_transcript_is_saved_as_that_voice_note(enabled_history):
    module = enabled_history
    message, bot, adapter = _setup(from_user_id=1000, caption="Notes")

    timings = await _publish(module, message, bot, adapter)

    assert timings["cleanup"] == "edited" and bot.delivered_calls == []
    assert _texts(module) == [
        ("message.created", "transcript", 77, RAW, None),
        ("message.edited", "transcript", 77, CLEAN, "transcript_cleanup"),
    ]
    stats = _stats(module)
    assert (stats.messages.sent, stats.edited.total) == (0, 0)
    assert _dossier_view(module)["77"].label == "transcript"


@pytest.mark.asyncio
async def test_a_long_transcript_saves_every_part_once_and_counts_none(enabled_history):
    module = enabled_history
    message, bot, adapter = _setup()
    raw = " ".join([RAW_FIRST_RAW] * 30)

    await _publish(module, message, bot, adapter, transcript=raw, cleaned=" ".join([RAW_FIRST_CLEAN] * 30))

    records = load_records(module)
    assert len(records) == len(bot.delivered_calls) > 1
    assert [r["text"] for r in records] == [call["text"] for call in bot.delivered_calls]
    assert [r["reply_to_message_id"] for r in records] == [77] + [None] * (len(records) - 1)
    assert {r["source"] for r in records} == {"transcript"}
    assert _stats(module).messages.total == 0
    assert {m.label for m in _dossier_view(module).values()} == {"transcript"}
    # An owner message right after the parts is the owner's, not one more part.
    await module._history_support.observe_ptb_update(make_business_text_update(
        text="ok", message_id=900, update_id=10, from_user_id=1000), bot=bot)
    assert _dossier_view(module)["900"].label == "owner"
    assert _stats(module).messages.sent == 1
