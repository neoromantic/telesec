from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import test_history
from test_history import FakeBot, make_business_text_update, make_deleted_update, set_switch

plugin = test_history.plugin  # fixtures
enabled_history = test_history.enabled_history

BASE = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)
TITLE = "🔔 Deleted message: Casey Customer (@customer991) · chat 991"


class AlertBot(FakeBot):
    username = "TelesecTestBot"

    def __init__(self) -> None:
        super().__init__(owner_id=1000)
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.answers: list[dict] = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=len(self.sent))

    async def edit_message_text(self, **kwargs):
        self.edits.append(kwargs)
        return True

    async def answer_callback_query(self, **kwargs):
        self.answers.append(kwargs)


@pytest.fixture
def alerts(enabled_history):
    module = enabled_history
    module._deletion_alerts.register_strings()
    module._history_support.set_deletion_listener(module._deletion_alerts.on_classified)
    yield module
    module._history_support.set_deletion_listener(None)


async def _delete(module, bot, *, text: str, from_user_id: int = 2000, then: str | None = None) -> None:
    module._deletion_alerts.bind(bot)
    observe = module._history_support.observe_ptb_update
    await observe(make_business_text_update(text=text, message_id=77, update_id=1, date=BASE,
                                            from_user_id=from_user_id), bot=bot, now=BASE)
    await observe(make_deleted_update(message_ids=(77,), update_id=2), bot=bot, now=BASE + timedelta(seconds=2))
    if then is not None:
        await observe(make_business_text_update(text=then, message_id=78, update_id=3, date=BASE,
                                                from_user_id=from_user_id), bot=bot, now=BASE + timedelta(seconds=4))
    module._history_support.maintain_history(now=BASE + timedelta(seconds=30))
    await _settle()


async def _settle() -> None:
    """Wait for the alerts the history listener scheduled on this loop."""
    await asyncio.sleep(0)
    await asyncio.gather(*(task for task in asyncio.all_tasks() if task is not asyncio.current_task()))


def _buttons(call: dict) -> list[tuple[str, str | None, str | None]]:
    markup = call["reply_markup"]
    return [(b.text, b.callback_data, b.url) for row in (markup.inline_keyboard if markup else ()) for b in row]


@pytest.mark.asyncio
async def test_an_unexplained_deletion_sends_one_alert_with_the_text_behind_a_button(alerts):
    bot = AlertBot()

    await _delete(alerts, bot, text="Meet me at the north gate at five")
    alerts._history_support.maintain_history(now=BASE + timedelta(minutes=5))
    await _settle()

    assert len(bot.sent) == 1
    assert bot.sent[0]["chat_id"] == 1000
    assert bot.sent[0]["text"] == f"{TITLE}\nUnexplained: no later message replaced it."
    assert "Meet me" not in bot.sent[0]["text"]
    assert _buttons(bot.sent[0]) == [
        ("👁 Show text", "tb:x:991:77", None),
        ("Chat panel", None, "https://t.me/TelesecTestBot?start=bizChat991"),
    ]

    query = SimpleNamespace(
        id="q1", data="tb:x:991:77", from_user=SimpleNamespace(id=1000, language_code="en"),
        message=SimpleNamespace(chat=SimpleNamespace(id=1000), message_id=1, text=bot.sent[0]["text"],
                                reply_markup=bot.sent[0]["reply_markup"]),
    )
    await alerts._handle_owner_panel_callback(query, bot=bot)

    assert len(bot.edits) == 1
    edit = bot.edits[0]
    prefix = f"{TITLE}\nUnexplained: no later message replaced it.\n\n"
    assert edit["text"] == prefix + "Meet me at the north gate at five"
    assert edit["entities"][0]["offset"] == alerts._telegram_text_length(prefix)
    assert _buttons(edit) == [("Chat panel", None, "https://t.me/TelesecTestBot?start=bizChat991")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "then"),
    [("Meet me at teh north gate at five", "Meet me at the north gate at five"),  # likely correction
     ("Meet me at the north gate at five", "Meet me at the north gate at five")],  # likely duplicate
)
async def test_a_deletion_replaced_by_a_later_message_stays_quiet(alerts, text, then):
    bot = AlertBot()

    await _delete(alerts, bot, text=text, then=then)

    assert bot.sent == []


@pytest.mark.asyncio
async def test_the_owners_own_deletion_stays_quiet(alerts):
    bot = AlertBot()

    await _delete(alerts, bot, text="Meet me at the north gate at five", from_user_id=1000)

    assert bot.sent == []


@pytest.mark.asyncio
async def test_alerts_off_in_settings_or_for_the_chat_stay_quiet(alerts):
    bot = AlertBot()
    set_switch(alerts, "alerts", False)
    await _delete(alerts, bot, text="Meet me at the north gate at five")
    assert bot.sent == []

    set_switch(alerts, "alerts", True)
    alerts._chat_settings.default_store().set_flag(1000, 991, "alerts", False, via="cli")
    await _delete(alerts, bot, text="Meet me at the south gate at five")
    assert bot.sent == []


@pytest.mark.asyncio
async def test_alerts_speak_the_owners_language(alerts):
    bot = AlertBot()
    alerts._deletion_alerts.note_language(SimpleNamespace(id=1000, language_code="ru"))

    await _delete(alerts, bot, text="Встретимся у северных ворот в пять")

    assert bot.sent[0]["text"] == ("🔔 Удалённое сообщение: Casey Customer (@customer991) · chat 991\n"
                                   "Без объяснения: позже его ничем не заменили.")
    assert [label for label, _data, _url in _buttons(bot.sent[0])] == ["👁 Показать текст", "Панель чата"]


@pytest.mark.asyncio
async def test_show_text_after_the_history_is_gone_says_so(alerts):
    bot = AlertBot()
    await _delete(alerts, bot, text="Meet me at the north gate at five")
    alerts._history_support.forget_chat("business-123", 991)
    query = SimpleNamespace(id="q1", data="tb:x:991:77", from_user=SimpleNamespace(id=1000, language_code="en"),
                            message=SimpleNamespace(chat=SimpleNamespace(id=1000), message_id=1, text="alert"))

    await alerts._handle_owner_panel_callback(query, bot=bot)

    assert bot.edits == []
    assert bot.answers[-1]["text"] == "The text is no longer saved"


def test_should_alert_only_for_a_contacts_unexplained_deletion(plugin):
    should = plugin._deletion_alerts.should_alert
    assert should({"direction": "inbound", "classification": "unexplained"})
    assert should({"direction": "inbound", "classification": "unclassifiable"})
    assert not should({"direction": "inbound", "classification": "likely_correction"})
    assert not should({"direction": "inbound", "classification": "likely_duplicate"})
    assert not should({"direction": "outbound", "classification": "unexplained"})
    assert not should({"direction": "unknown", "classification": "unclassifiable"})
