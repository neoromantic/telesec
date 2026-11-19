from __future__ import annotations

import html
import importlib
import logging
import re
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import test_history as th
import test_owner_panel as top
import test_transcription_modes as ttm
from test_history import _build_history_parser, snapshot_history_state
from test_stats import (
    NOW,
    STATS_DOC,
    _alice_fixture,
    _break_history,
    _doc_section,
    _entry,
    _fenced_blocks,
    _hermes_tz,
    _msg,
    _overview_fixture,
)

plugin = th.plugin
enabled_history = th.enabled_history
modes = ttm.modes

ICON = "\U0001f4ca"
# (sub-argument, the CLI's --since on 2026-10-19): whole local days, today included.
PERIODS = [("7d", "2026-10-13"), ("30d", "2026-09-20"), ("90d", "2026-07-22"), ("all", None)]
CHATS = [(991, "business-123"), (992, "business-123"), (994, "business-456"), (995, "business-123")]
SUB_RE = re.compile(r"[A-Za-z0-9_.-]{1,32}")
TITLE = f"{ICON} Stats"
TITLE_RU = f"{ICON} Статистика"
HISTORY_OFF = f"{TITLE}\nHistory is off. Turn it on in Settings (/telesec)."
HISTORY_OFF_RU = f"{TITLE_RU}\nИстория выключена. Включите её в настройках (/telesec)."
NO_CONNECTION = (
    f"{TITLE}\nThe Business connection for this chat is not known yet. Send any message in the chat and try again."
)
NO_CONNECTION_RU = (
    f"{TITLE_RU}\nBusiness-подключение этого чата пока неизвестно. "
    "Отправьте любое сообщение в этом чате и попробуйте снова."
)
NO_HISTORY = "No history for this chat yet. History records new text messages only."
NO_HISTORY_RU = "Истории этого чата пока нет. История записывает только новые текстовые сообщения."
READ_ERROR = (
    f"{TITLE}\nHistory could not be read. For details, run on the gateway host:\n"
    "hermes telesec stats --chat 991 --connection business-123"
)
READ_ERROR_RU = (
    f"{TITLE_RU}\nИсторию не удалось прочитать. Подробности — на хосте шлюза:\n"
    "hermes telesec stats --chat 991 --connection business-123"
)
_TAG_RE = re.compile(r"<[^>]+>")

# Real views of the Alice fixture with Hermes time zone Europe/Moscow, clock at 2026-10-19 11:32 UTC.
# The week matches test_stats.ALICE_WEEK_MOSCOW number for number.
ALICE_WEEK_RU = """
<h3>📊 Alice Example · 7 дней</h3>
<p>13 окт – 19 окт · 💬 <b>3</b> · <b>2</b> из 7 дней</p>
<p>67% 🟦🟦🟦🟦🟦🟦🟦🟩🟩🟩 33%</p>
<table striped compact>
<tr><th></th><th align="right">🟦 Alice</th><th align="right">🟩 Вы</th></tr>
<tr><td>сообщений</td><td align="right"><b>2</b> · 67%</td><td align="right"><b>1</b> · 33%</td></tr>
<tr><td>⚡ ответ</td><td align="right"><b>1 ч 34 мин</b></td><td align="right">–</td></tr>
<tr><td>p90</td><td align="right">1 ч 34 мин</td><td align="right">–</td></tr>
<tr><td>подряд</td><td align="right">2,0</td><td align="right">1,0</td></tr>
<tr><td>длина</td><td align="right">10</td><td align="right">10</td></tr>
</table>
<blockquote>⏳ Alice ждёт ответа <b>1 дн 17 ч</b></blockquote>
<h4>🗓 Когда</h4>
<table compact>
<tr><th>пн</th><th>вт</th><th>ср</th><th>чт</th><th>пт</th><th>сб</th><th>вс</th></tr>
<tr><td></td><td>·</td><td>·</td><td>·</td><td>·</td><td>🟥</td><td>·</td></tr>
<tr><td>🟧</td><td></td><td></td><td></td><td></td><td></td><td></td></tr>
</table>
<table compact>
<tr><th>0</th><th>3</th><th>6</th><th>9</th><th>12</th><th>15</th><th>18</th><th>21</th></tr>
<tr><td>🟥</td><td>·</td><td>·</td><td>·</td><td>·</td><td>·</td><td>🟥</td><td>🟥</td></tr>
</table>
<p>🎯 Пик — <mark>сб, 0–1</mark></p>
<table compact>
<tr><td>🔥 серия</td><td align="right"><b>1</b> день</td><td>🏆 рекорд</td><td align="right"><b>1</b> день</td></tr>
<tr><td>💤 пауза</td><td align="right"><b>1</b> день</td><td>🗣 бесед</td><td align="right"><b>2</b></td></tr>
<tr><td>👋 первым</td><td align="right">🟦 <b>50%</b></td><td>🗑 удалено</td><td align="right"><b>1</b></td></tr>
</table>
<p>📈 К прошлым 7 дням: сообщений <b>+0%</b> · ответ 🟦 10 мин → 1 ч 34 мин</p>
<footer>история с 3 окт 2026 · только текст</footer>
""".strip("\n")

ALICE_WEEK_EN = """
<h3>📊 Alice Example · 7 days</h3>
<p>13 Oct – 19 Oct · 💬 <b>3</b> · <b>2</b> of 7 days</p>
<p>67% 🟦🟦🟦🟦🟦🟦🟦🟩🟩🟩 33%</p>
<table striped compact>
<tr><th></th><th align="right">🟦 Alice</th><th align="right">🟩 You</th></tr>
<tr><td>messages</td><td align="right"><b>2</b> · 67%</td><td align="right"><b>1</b> · 33%</td></tr>
<tr><td>⚡ reply</td><td align="right"><b>1 h 34 min</b></td><td align="right">–</td></tr>
<tr><td>p90</td><td align="right">1 h 34 min</td><td align="right">–</td></tr>
<tr><td>per turn</td><td align="right">2.0</td><td align="right">1.0</td></tr>
<tr><td>length</td><td align="right">10</td><td align="right">10</td></tr>
</table>
<blockquote>⏳ Alice is waiting for your reply <b>1 d 17 h</b></blockquote>
<h4>🗓 When</h4>
<table compact>
<tr><th>Mo</th><th>Tu</th><th>We</th><th>Th</th><th>Fr</th><th>Sa</th><th>Su</th></tr>
<tr><td></td><td>·</td><td>·</td><td>·</td><td>·</td><td>🟥</td><td>·</td></tr>
<tr><td>🟧</td><td></td><td></td><td></td><td></td><td></td><td></td></tr>
</table>
<table compact>
<tr><th>0</th><th>3</th><th>6</th><th>9</th><th>12</th><th>15</th><th>18</th><th>21</th></tr>
<tr><td>🟥</td><td>·</td><td>·</td><td>·</td><td>·</td><td>·</td><td>🟥</td><td>🟥</td></tr>
</table>
<p>🎯 Peak — <mark>Sat, 0–1</mark></p>
<table compact>
<tr><td>🔥 streak</td><td align="right"><b>1</b> day</td><td>🏆 record</td><td align="right"><b>1</b> day</td></tr>
<tr><td>💤 pause</td><td align="right"><b>1</b> day</td><td>🗣 sessions</td><td align="right"><b>2</b></td></tr>
<tr><td>👋 first</td><td align="right">🟦 <b>50%</b></td><td>🗑 deleted</td><td align="right"><b>1</b></td></tr>
</table>
<p>📈 vs the previous 7 days: messages <b>+0%</b> · reply 🟦 10 min → 1 h 34 min</p>
<footer>history since 3 Oct 2026 · text only</footer>
""".strip("\n")

ALICE_MONTH = """
<h3>📊 Alice Example · 30 days</h3>
<p>20 Sep – 19 Oct · 💬 <b>10</b> · <b>5</b> of 30 days</p>
<p>67% 🟦🟦🟦🟦🟦🟦🟦🟩🟩🟩 33%</p>
<table striped compact>
<tr><th></th><th align="right">🟦 Alice</th><th align="right">🟩 You</th></tr>
<tr><td>messages</td><td align="right"><b>6</b> · 67%</td><td align="right"><b>3</b> · 33%</td></tr>
<tr><td>⚡ reply</td><td align="right"><b>52 min</b></td><td align="right"><b>30 min</b></td></tr>
<tr><td>p90</td><td align="right">1 h 34 min</td><td align="right">30 min</td></tr>
<tr><td>per turn</td><td align="right">1.5</td><td align="right">1.0</td></tr>
<tr><td>length</td><td align="right">10</td><td align="right">10</td></tr>
<tr><td>edits</td><td align="right">0</td><td align="right">1</td></tr>
</table>
<blockquote>⏳ Alice is waiting for your reply <b>1 d 17 h</b></blockquote>
<h4>🗓 When</h4>
<table compact>
<tr><th>Mo</th><th>Tu</th><th>We</th><th>Th</th><th>Fr</th><th>Sa</th><th>Su</th></tr>
<tr><td></td><td></td><td></td><td></td><td></td><td>🟥</td><td>🟨</td></tr>
<tr><td>·</td><td>·</td><td>·</td><td>·</td><td>·</td><td>🟥</td><td>·</td></tr>
<tr><td>·</td><td>·</td><td>·</td><td>·</td><td>·</td><td>🟧</td><td>·</td></tr>
<tr><td>🟨</td><td></td><td></td><td></td><td></td><td></td><td></td></tr>
</table>
<table compact>
<tr><th>0</th><th>3</th><th>6</th><th>9</th><th>12</th><th>15</th><th>18</th><th>21</th></tr>
<tr><td>🟧</td><td>·</td><td>·</td><td>🟧</td><td>🟥</td><td>·</td><td>🟨</td><td>🟧</td></tr>
</table>
<p>🎯 Peak — <mark>Sat, 12–13</mark></p>
<table compact>
<tr><td>🔥 streak</td><td align="right"><b>1</b> day</td><td>🏆 record</td><td align="right"><b>2</b> days</td></tr>
<tr><td>💤 pause</td><td align="right"><b>7</b> days</td><td>🗣 sessions</td><td align="right"><b>5</b></td></tr>
<tr><td>👋 first</td><td align="right">🟦 <b>60%</b></td><td>🗑 deleted</td><td align="right"><b>4</b> · ❓1</td></tr>
</table>
<footer>history since 3 Oct 2026 · text only</footer>
""".strip("\n")


@pytest.fixture(autouse=True)
def _fixed_clock(plugin, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("HERMES_TIMEZONE", raising=False)  # no Hermes time zone
    monkeypatch.setitem(sys.modules, "hermes_cli.config", None)
    monkeypatch.setattr(plugin._history_support, "_utcnow", lambda: NOW)


@pytest.fixture
def sp(plugin):
    return importlib.import_module(f"{plugin.__name__}.stats_panel")


@pytest.fixture
def cs(plugin):
    return importlib.import_module(f"{plugin.__name__}.correspondence_stats")


def _view(sp, period: str | None = None, *, chat: Any = 991, conn: str | None = "business-123", **kwargs: Any):
    return sp.build_stats_view(chat_id=chat, business_connection_id=conn, period=period, **kwargs)


def _panel_records(caplog: pytest.LogCaptureFixture, sp) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == sp.__name__]


def _buttons(current: str, language: str = "en") -> tuple[tuple[str, str], ...]:
    labels = {
        "en": {"7d": "7d", "30d": "30d", "90d": "90d", "all": "All"},
        "ru": {"7d": "7 дн.", "30d": "30 дн.", "90d": "90 дн.", "all": "Всё"},
    }[language]
    return tuple((f"• {label}" if sub == current else label, sub) for sub, label in labels.items())


def _visible(text: str) -> str:
    """Rich message HTML as text: tags dropped, entities decoded."""
    return html.unescape(_TAG_RE.sub("", text))


class RichPanelBot(top.PanelBot):
    """A panel bot that also takes Bot API rich message edits (``do_api_request``)."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.api_calls: list[tuple[str, dict[str, Any]]] = []
        self.api_error: Exception | None = None

    async def do_api_request(self, endpoint: str, api_kwargs: dict[str, Any] | None = None, **_: Any):
        self.api_calls.append((endpoint, dict(api_kwargs or {})))
        if self.api_error is not None:
            raise self.api_error
        return True


def _rich_edit(bot) -> tuple[str, list]:
    """The last rich edit of the panel message: its HTML and buttons."""
    endpoint, call = bot.api_calls[-1]
    assert endpoint == "editmessagetext" and (call["chat_id"], call["message_id"]) == (top.OWNER, 500)
    assert call["rich_message"]["skip_entity_detection"] is True
    return call["rich_message"]["html"], top.keyboard(call["reply_markup"])


# --- Windows and body ------------------------------------------------------------


@pytest.mark.parametrize("tz", [None, "Europe/Moscow"])
def test_period_windows_equal_cli_windows(plugin, sp, cs, monkeypatch: pytest.MonkeyPatch, tz: str | None):
    if tz is not None:
        _hermes_tz(monkeypatch, tz)
    parser = _build_history_parser(plugin)
    for sub, since in PERIODS:
        args = parser.parse_args(["stats", *(["--since", since] if since else [])])
        window = sp.period_window(sub, now=NOW)
        assert window == cs.build_window(args, now=NOW)
        assert window.until is None and window.now == NOW and window.session_gap == timedelta(hours=6)
        assert window.tz_label == (tz or "UTC")
    assert sp.period_window("7d", now=NOW).since == datetime(2026, 10, 13, tzinfo=ZoneInfo(tz) if tz else UTC)
    assert sp.period_window("all", now=NOW).since is None
    # Unknown or missing periods mean the default.
    for period in (None, "", "bogus"):
        assert sp.period_window(period, now=NOW) == sp.period_window("30d", now=NOW)


# --- Buttons ---------------------------------------------------------------------


def test_period_buttons(enabled_history, sp):
    _alice_fixture(enabled_history)
    for sub, _ in PERIODS:
        assert sp.period_buttons(sub) == _buttons(sub)
        assert sp.period_buttons(sub, "ru-RU") == _buttons(sub, "ru")
    for period in (None, "bogus", "30D"):
        view = _view(sp, period)
        assert view.buttons == _buttons("30d")
        assert view.text.splitlines()[0].endswith("· 30 days")

    # Every period's callback data fits Telegram's 64 bytes, even for the longest chat id.
    panel = sp._owner_panel
    for _, sub in _buttons("all"):
        assert SUB_RE.fullmatch(sub)
        data = panel.section_callback(-(10**19), sp.SECTION_KEY, sub)
        assert len(data.encode()) <= 64 and panel.parse_callback(data).sub == sub


# --- States ----------------------------------------------------------------------


def test_state_order(plugin, sp):
    # History off wins over a missing connection.
    assert _view(sp, conn=None).text == HISTORY_OFF
    th.set_switch(plugin, "history", True)
    view = _view(sp, conn=None)
    assert view.text == NO_CONNECTION and view.buttons == ()


def test_no_history_for_chat(enabled_history, sp):
    # No history at all yet, and the panel creates none.
    assert snapshot_history_state(enabled_history) == {}
    assert _view(sp, chat=12345).text == f"{ICON} chat 12345\n{NO_HISTORY}"
    assert _view(sp, chat=12345, language_code="ru").text == f"{ICON} чат 12345\n{NO_HISTORY_RU}"
    assert snapshot_history_state(enabled_history) == {}
    _alice_fixture(enabled_history)
    before = snapshot_history_state(enabled_history)
    view = _view(sp, chat=12345, chat_label="Zoë")
    assert view.text == f"{ICON} Zoë\n{NO_HISTORY}" and view.html is None
    assert view.buttons == ()
    assert snapshot_history_state(enabled_history) == before
    # The label is untrusted: escaped like contact names and clipped (plain text, so no HTML escaping).
    view = _view(sp, chat=12345, chat_label="<Eve>&\nX\u202e" + "y" * 200)
    first = view.text.splitlines()[0]
    assert first.startswith(f"{ICON} <Eve>&\\nX\\u202e") and first.endswith("…")
    assert len(first) == len(f"{ICON} ") + 64
    assert "\u202e" not in view.text


def test_same_chat_under_another_connection_is_not_used(enabled_history, sp):
    _overview_fixture(enabled_history)
    # Carol (chat 994) exists only under business-456.
    view = _view(sp, chat=994, conn="business-123")
    assert view.text == f"{ICON} chat 994\n{NO_HISTORY}"
    assert "Carol" not in view.text
    assert _view(sp, chat=994, conn="business-456").html.startswith(f"<h3>{ICON} Carol · 30 days</h3>\n")


@pytest.mark.parametrize("case", ["stale", "chat_lock"])
def test_read_error_shows_cli_command(enabled_history, sp, caplog: pytest.LogCaptureFixture, case: str):
    _alice_fixture(enabled_history)
    cli_error = _break_history(enabled_history, case)
    before = snapshot_history_state(enabled_history)
    caplog.set_level(logging.WARNING)

    view = _view(sp, "7d")

    assert view.text == READ_ERROR
    assert view.buttons == _buttons("7d")
    assert _view(sp, "7d", language_code="ru").text == READ_ERROR_RU
    record = _panel_records(caplog, sp)[0]
    assert record.levelno == logging.WARNING
    assert re.fullmatch(r"telesec stats panel: history could not be read \((\w+)\)", record.getMessage())
    logged = "\n".join(record.getMessage() for record in caplog.records)
    for secret in ("Alice", "alice", "business-123", "991", str(enabled_history._history_support.history_root())):
        assert secret not in logged
    assert "Alice" not in caplog.text
    assert cli_error.removeprefix("error: ")[:20] not in logged
    # Read-only: nothing was repaired or rebuilt.
    assert snapshot_history_state(enabled_history) == before


@pytest.mark.parametrize("error", [OSError, ValueError, OverflowError])
def test_read_error_logs_only_exception_type(
    enabled_history, sp, cs, monkeypatch, caplog: pytest.LogCaptureFixture, error: type[Exception]
):
    _alice_fixture(enabled_history)
    caplog.set_level(logging.WARNING)
    message = "cannot read /home/user/history/Alice Example SECRET-101"

    def explode(*_args: Any, **_kwargs: Any):
        raise error(message)

    monkeypatch.setattr(cs, "collect_contact_stats", explode)
    view = _view(sp)

    assert view.text == READ_ERROR
    assert view.buttons == _buttons("30d")
    assert [record.getMessage() for record in _panel_records(caplog, sp)] == [
        f"telesec stats panel: history could not be read ({error.__name__})"
    ]
    for secret in (message, "Alice", "SECRET", "/home/user"):
        assert secret not in caplog.text


@pytest.mark.parametrize(
    ("conn", "expected"),
    [
        ("-Ab C", "hermes telesec stats --chat=-100123 --connection='-Ab C'"),
        ("<b>&x", "hermes telesec stats --chat=-100123 --connection '<b>&x'"),
    ],
)
def test_read_error_command_quotes_values(
    enabled_history, sp, cs, monkeypatch, caplog: pytest.LogCaptureFixture, conn: str, expected: str
):
    entries = [_entry(conn=conn), _entry(conn=conn)]
    # Two catalog entries for one (connection, chat) mean the catalog is damaged.
    monkeypatch.setattr(enabled_history._history_support, "_filter_catalog_entries", lambda **_kwargs: entries)
    caplog.set_level(logging.WARNING)

    view = _view(sp, chat=-100123, conn=conn)

    assert view.text.splitlines()[-1] == expected
    assert expected.endswith(cs._cli_option("--connection", conn)) and view.html is None
    assert [record.getMessage() for record in _panel_records(caplog, sp)] == [
        "telesec stats panel: history could not be read (ValueError)"
    ]


# --- Names, size, and privacy -----------------------------------------------------


@pytest.mark.parametrize(
    ("first_name", "title", "column"),
    [
        ("A" * 64, "A" * 64, "A" * 15 + "…"),
        ("A" * 65, "A" * 63 + "…", "A" * 15 + "…"),
        ("\x01" * 60, "\\x01" * 15 + "\\x0…", "\\x01" * 3 + "\\x0…"),
    ],
)
def test_contact_name_is_clipped(plugin, sp, cs, first_name: str, title: str, column: str):
    window = sp.period_window("7d", now=NOW)
    profile = {"id": 991, "type": "private", "first_name": first_name}
    records = [_msg(plugin, 1, NOW - timedelta(hours=1), "inbound", profile=profile)]
    stats = cs.compute_stats(_entry(profile=profile), records, window)
    view = sp.render_report(stats, window, period="7d", contact_name=first_name)
    lines = view.text.splitlines()
    # 64 characters in the title, 16 in the columns.
    assert lines[0] == f"{ICON} {title} · 7 days"
    assert lines[2].startswith(f"{chr(0x1F7E6)} {column}: ")
    assert f'<th align="right">{chr(0x1F7E6)} {html.escape(column)}</th>' in view.html


def test_message_text_never_appears(enabled_history, sp, monkeypatch: pytest.MonkeyPatch, caplog):
    _overview_fixture(enabled_history)
    caplog.set_level(logging.DEBUG)
    views = []
    for tz in (None, "Europe/Moscow"):
        _hermes_tz(monkeypatch, tz)
        for sub, _ in PERIODS:
            for chat, conn in CHATS:
                for language in (None, "ru"):
                    views.append(_view(sp, sub, chat=chat, conn=conn, language_code=language))
    _break_history(enabled_history, "torn_tail")
    views.append(_view(sp))
    assert views[-1].text == READ_ERROR
    for view in views:
        assert "SECRET" not in view.text and "SECRET" not in (view.html or "")
        assert all("SECRET" not in label for label, _ in view.buttons)
    assert "SECRET" not in caplog.text


# --- Owner panel -----------------------------------------------------------------


def test_register_adds_the_stats_section(plugin):
    ctx = SimpleNamespace(
        llm=None,
        register_telegram_handler=lambda handler: None,
        register_hook=lambda name, callback: None,
        get_config={}.get,
        register_cli_command=lambda **kwargs: None,
        register_skill=lambda name, path, description="": None,
    )
    panel = plugin._owner_panel
    assert panel.get_section("stats") is None

    plugin.register(ctx)

    section = panel.get_section("stats")
    assert (section.label, section.order) == (f"{ICON} Stats", 20)
    assert section.render is plugin._stats_panel.render_section
    assert panel.section_label(section, "ru") == f"{ICON} Статистика"
    assert panel.section_label(section, "en") == f"{ICON} Stats"


def _edit_call(bot) -> dict[str, Any]:
    call = bot.text_edit_calls[-1]
    assert (call["chat_id"], call["message_id"]) == (top.OWNER, 500)
    return call


@pytest.mark.asyncio
async def test_panel_opens_stats_and_switches_periods(enabled_history, sp, modes):
    plugin = enabled_history
    _alice_fixture(plugin)
    top.bind_owner(plugin)
    sp.register_section()
    bot = RichPanelBot(business_owner_id=top.OWNER)

    assert await top.dispatch(plugin, top.start_update(), bot) is True
    assert (f"{ICON} Stats", "tb:s:991:stats") in [row[0] for row in top.keyboard(bot.calls[0]["reply_markup"])]
    # The main panel stays plain text.
    assert "parse_mode" not in bot.calls[0] and "link_preview_options" not in bot.calls[0]

    assert await top.dispatch(plugin, top.callback_update("tb:s:991:stats"), bot) is True
    edits = len(bot.text_edit_calls)
    text, rows = _rich_edit(bot)
    assert text == _view(sp).html
    assert text.startswith(f"<h3>{ICON} Alice Example · 30 days</h3>\n")
    assert rows == [
        [
            ("7d", "tb:s:991:stats/7d"),
            ("• 30d", "tb:s:991:stats/30d"),
            ("90d", "tb:s:991:stats/90d"),
            ("All", "tb:s:991:stats/all"),
        ],
        [("« Back", "tb:b:991")],
    ]

    assert await top.dispatch(plugin, top.callback_update("tb:s:991:stats/all"), bot) is True
    text, rows = _rich_edit(bot)
    assert text == _view(sp, "all").html
    assert rows[0][3] == ("• All", "tb:s:991:stats/all")
    # The rich edits applied, so no plain-text edit was needed.
    assert len(bot.text_edit_calls) == edits

    # Back to the main panel: plain text again.
    assert await top.dispatch(plugin, top.callback_update("tb:b:991"), bot) is True
    assert "parse_mode" not in _edit_call(bot) and "link_preview_options" not in _edit_call(bot)
    # Everything went to the owner's private chat; nothing reached the Business chat.
    calls = [*bot.calls, *bot.text_edit_calls, *(call for _, call in bot.api_calls)]
    assert {call["chat_id"] for call in calls} == {top.OWNER}


@pytest.mark.asyncio
async def test_rejected_rich_message_falls_back_to_the_plain_text(enabled_history, sp, modes):
    plugin = enabled_history
    _alice_fixture(plugin)
    top.bind_owner(plugin)
    sp.register_section()
    bot = RichPanelBot(business_owner_id=top.OWNER)
    bot.api_error = ttm.BadRequest("Bad Request: can't parse the rich message")

    assert await top.dispatch(plugin, top.callback_update("tb:s:991:stats"), bot) is True

    [(endpoint, _)] = bot.api_calls
    assert endpoint == "editmessagetext"
    text, rows = top.last_edit(bot)
    assert text == _view(sp).text and rows[0][1] == ("• 30d", "tb:s:991:stats/30d")
    assert "parse_mode" not in _edit_call(bot) and "link_preview_options" not in _edit_call(bot)


# --- History paused for this chat (Don't save) -----------------------------------

PAUSED = "History is paused for this chat."
PAUSED_RU = "История этого чата сейчас не сохраняется."


def _pause(plugin, chat: int = 991) -> None:
    plugin._chat_settings.default_store().set_flag(top.OWNER, chat, "history", False, via="cli")


async def _open_stats(plugin, bot, data: str, language: str | None) -> tuple[str, list]:
    """The stats section as the owner sees it: the rich HTML of a report, else a state's plain text."""
    edits, rich = len(bot.text_edit_calls), len(bot.api_calls)
    assert await top.dispatch(plugin, top.callback_update(data, language_code=language), bot) is True
    if len(bot.api_calls) > rich:
        assert len(bot.text_edit_calls) == edits
        return _rich_edit(bot)
    assert "parse_mode" not in _edit_call(bot)
    return top.last_edit(bot)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "report", "note", "back"),
    [(None, ALICE_WEEK_EN, PAUSED, "« Back"), ("ru", ALICE_WEEK_RU, PAUSED_RU, "« Назад")],
)
async def test_paused_chat_shows_the_saved_report_then_the_note(
    enabled_history, sp, modes, monkeypatch: pytest.MonkeyPatch, language, report: str, note: str, back: str
):
    plugin = enabled_history
    _alice_fixture(plugin)
    _hermes_tz(monkeypatch, "Europe/Moscow")
    top.bind_owner(plugin)
    sp.register_section()
    bot = RichPanelBot(business_owner_id=top.OWNER)

    text, _ = await _open_stats(plugin, bot, "tb:s:991:stats/7d", language)
    assert text == report and note not in text

    _pause(plugin)
    text, rows = await _open_stats(plugin, bot, "tb:s:991:stats/7d", language)
    # What was saved still shows; the note is its own last paragraph, before the footer.
    head, footer = report.rsplit("\n", 1)
    assert text == f"{head}\n<p>{note}</p>\n{footer}"
    assert rows == [
        [(label, f"tb:s:991:stats/{sub}") for label, sub in _buttons("7d", language or "en")],
        [(back, "tb:b:991")],
    ]
    paused = _view(sp, "7d", language_code=language, history_paused=True)
    assert paused.html == text and paused.text.splitlines()[-2] == note

    # The other states end with it too, but not history off: there is nothing to pause.
    lang = language or "en"
    no_history = NO_HISTORY_RU if lang == "ru" else NO_HISTORY
    assert _view(sp, chat=12345, language_code=language, history_paused=True).text == (
        f"{ICON} {'чат' if lang == 'ru' else 'chat'} 12345\n{no_history}\n{note}"
    )
    assert _view(sp, conn=None, language_code=language, history_paused=True).text == (
        f"{NO_CONNECTION_RU if lang == 'ru' else NO_CONNECTION}\n{note}"
    )
    _break_history(plugin, "torn_tail")
    view = _view(sp, language_code=language, history_paused=True)
    assert view.text == f"{READ_ERROR_RU if lang == 'ru' else READ_ERROR}\n{note}"
    assert view.buttons == _buttons("30d", lang)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "expected", "back"), [(None, HISTORY_OFF, "« Back"), ("ru", HISTORY_OFF_RU, "« Назад")]
)
async def test_paused_note_is_not_shown_while_history_is_off(plugin, sp, modes, language, expected: str, back: str):
    _alice_fixture(plugin)
    top.bind_owner(plugin)
    sp.register_section()
    _pause(plugin)
    bot = RichPanelBot(business_owner_id=top.OWNER)

    text, rows = await _open_stats(plugin, bot, "tb:s:991:stats", language)

    assert (text, rows) == (expected, [[(back, "tb:b:991")]])
    assert _view(sp, language_code=language, history_paused=True).text == expected


# --- Docs ------------------------------------------------------------------------


def test_docs_owner_panel_example_matches_view(enabled_history, sp, monkeypatch: pytest.MonkeyPatch):
    _overview_fixture(enabled_history)
    _hermes_tz(monkeypatch, "Europe/Moscow")
    section = _doc_section(STATS_DOC.read_text(encoding="utf-8"), "In Telegram (owner panel)")
    view = _view(sp)
    assert view.html == ALICE_MONTH

    [(language, documented)] = _fenced_blocks(section)

    assert language == "html"
    assert documented == view.html
