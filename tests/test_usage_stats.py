"""Usage stats: the shared client's wiring, the notice in Telegram, Settings, and `hermes telesec telemetry`.

Nothing here reaches the network: the client is off in a test run, and the tests that turn it on record
each event instead of sending it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_owner_panel as top
import test_plugin
import test_settings as ts
import test_transcription_modes as ttm

isolate_plugin_environment = test_plugin.isolate_plugin_environment
isolate_modes_environment = ttm.isolate_modes_environment
isolate_dossier_route = ts.isolate_dossier_route
plugin = test_plugin.plugin
modes = ttm.modes
gs = ts.gs

OWNER = ttm.OWNER
PEER = ttm.PEER
BCID = ttm.BCID
OTHER = ts.OTHER
ROOT = Path(__file__).resolve().parents[1]
ENV = ("DO_NOT_TRACK", "TELESEC_TELEMETRY", "CI", "AI_AGENT", "CLAUDECODE", "CODEX_SANDBOX", "CODEX_THREAD_ID",
       "CURSOR_AGENT", "GEMINI_CLI")
SWITCH_ROW = [("• Share usage stats", "tb:s:0:settings/u.on"), ("Don't share", "tb:s:0:settings/u.off")]
OFF_ROW = [("Share usage stats", "tb:s:0:settings/u.on"), ("• Don't share", "tb:s:0:settings/u.off")]


@pytest.fixture
def usage(plugin, modes, monkeypatch):
    """The plugin's usage stats with the environment out of the way: on by default, behind the notice. Events are
    recorded in ``usage.sent``, never sent."""
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    module = plugin._usage_stats
    telemetry = module._telemetry
    # The client is off in a test run and in CI; these tests check what it does where it is on.
    monkeypatch.setattr(telemetry, "TEST_VARS", ())
    monkeypatch.setattr(telemetry, "CI_VARS", ())
    sent: list[dict] = []
    monkeypatch.setattr(telemetry, "_send", lambda _endpoint, body, *_args: sent.append(body))
    module.reset()
    client = module.setup(version="1.2.3")
    yield SimpleNamespace(module=module, client=client, sent=sent, state=lambda: _state(client))
    module.reset()


def _state(client) -> dict:
    path = Path(client.status()["stateFile"])
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def seen_elsewhere(usage) -> None:
    """The notice was seen in another process (the one that shows it sends nothing itself)."""
    usage.module.create_client(version="1.2.3").mark_notice_seen()


def events(usage) -> list[tuple[str, str | None]]:
    return [(body["event"], body["properties"].get("feature") or body["properties"].get("error_type"))
            for body in usage.sent]


async def settle(usage) -> None:
    while usage.module._tasks:
        await asyncio.gather(*list(usage.module._tasks))


# --- The client ----------------------------------------------------------------------------------


def test_the_client_is_the_managed_copy_set_up_for_telesec(plugin, tmp_path):
    header = (ROOT / "telemetry.py").read_text(encoding="utf-8").splitlines()[0]
    assert header.startswith("# managed by devkit")
    version = re.search(r"^version: (\S+)$", (ROOT / "plugin.yaml").read_text(encoding="utf-8"), re.MULTILINE)[1]
    assert re.search(rf'^version = "{re.escape(version)}"$', (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
                     re.MULTILINE)

    client = plugin._usage_stats.create_client()

    assert (client.product, client.version, client.channel, client.mode) == ("telesec", version, "plugin", "plugin")
    assert client.off_command == "hermes telesec telemetry off"
    assert client.status()["stateFile"] == str(tmp_path / "data" / "telesec" / "telemetry.json")
    assert set(plugin._usage_stats.GATEWAY_FEATURES) <= client.features


def test_a_test_run_sends_and_writes_nothing(plugin, tmp_path, monkeypatch):
    monkeypatch.delenv("TELESEC_TELEMETRY", raising=False)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    plugin._usage_stats.reset()
    client = plugin._usage_stats.setup()

    assert client.status()["enabled"] is False
    assert client.status()["reason"] in ("test", "ci")
    assert client.notice_text() is None
    plugin._usage_stats.started()
    plugin._usage_stats.feature("voice transcribed")
    assert not (tmp_path / "data" / "telesec" / "telemetry.json").exists()
    plugin._usage_stats.reset()


@pytest.mark.parametrize(("name", "value", "reason"), [("DO_NOT_TRACK", "1", "do-not-track"),
                                                       ("TELESEC_TELEMETRY", "0", "env"),
                                                       ("TELESEC_TELEMETRY", "off", "env")])
def test_the_environment_turns_it_off(usage, monkeypatch, name, value, reason):
    monkeypatch.setenv(name, value)
    client = usage.module.create_client(version="1.2.3")

    assert client.status() | {"id": None} == client.status() | {"enabled": False, "reason": reason, "id": None}
    assert client.notice_text() is None
    client.started()
    assert usage.sent == []


def test_nothing_is_sent_before_the_notice(usage):
    usage.module.started()
    usage.module.feature("voice transcribed")
    usage.module.error("stt.failed")

    assert usage.sent == []
    assert usage.state() == {}


def test_events_carry_fixed_names_and_error_types_only(usage):
    seen_elsewhere(usage)

    usage.module.started()
    usage.module.feature("voice transcribed")
    usage.module.feature("Chat with Alice")  # never a name off the list
    usage.module.error(ValueError("text from the chat"))
    usage.module.error("cleanup.TimeoutError")

    assert events(usage) == [("app_installed", None), ("app_started", None), ("feature_used", "voice transcribed"),
                             ("feature_used", "other"), ("error_occurred", "ValueError"),
                             ("error_occurred", "cleanup.TimeoutError")]
    props = usage.sent[1]["properties"]
    assert (props["product"], props["$app_version"], props["channel"], props["mode"]) == (
        "telesec", "1.2.3", "plugin", "plugin")
    assert all("error_message" not in body["properties"] for body in usage.sent)
    assert "Alice" not in json.dumps(usage.sent) and "text from the chat" not in json.dumps(usage.sent)


def test_each_event_goes_once_a_day(usage):
    seen_elsewhere(usage)

    for _ in range(3):
        usage.module.transcribed("voice", cleaned=True)
        usage.module.transcribed("video_note", cleaned=False)
        usage.module.transcribed("audio_document", cleaned=False)

    assert events(usage) == [("app_installed", None), ("feature_used", "voice transcribed"),
                             ("feature_used", "transcript cleanup"), ("feature_used", "video note transcribed"),
                             ("feature_used", "audio file transcribed")]


def test_dossier_cycles_count_updates_and_failure_codes(usage, plugin):
    seen_elsewhere(usage)
    report = plugin._dossier_support.CycleReport(refreshed=2, failures=[("contact 1", "timeout"),
                                                                        ("contact 2", "TimeoutError")])

    usage.module.dossier_cycle(report)
    usage.module.dossier_cycle(plugin._dossier_support.CycleReport(outcome="aborted_trust"))

    assert events(usage)[1:] == [("feature_used", "dossier update"), ("error_occurred", "dossier.timeout"),
                                 ("error_occurred", "dossier.TimeoutError"),
                                 ("error_occurred", "dossier.trust_gate_denied")]
    assert "contact" not in json.dumps(usage.sent)


@pytest.mark.parametrize(("data", "expected"), [
    ("tb:m:991:own", "panel mode"),
    ("tb:r:991", "panel mode"),
    ("tb:f:991:h.off", "panel chat switch"),
    ("tb:f:991:h.erase.yes", "panel erase"),
    ("tb:f:991:h.erase", None),
    ("tb:s:991:stats", "panel stats"),
    ("tb:s:991:stats/30d", None),  # paging inside a section is no new use
    ("tb:s:991:dossier", "panel dossier"),
    ("tb:s:991:settings", "settings"),
    ("tb:b:991", None),
])
def test_panel_taps_map_to_fixed_names(usage, plugin, data, expected):
    seen_elsewhere(usage)
    parsed = plugin._owner_panel.parse_callback(data)

    usage.module.panel_action(parsed.action, parsed.arg, parsed.section, parsed.sub)

    assert events(usage)[1:] == ([("feature_used", expected)] if expected else [])


# --- The notice in Telegram ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_gateway_sends_the_notice_once_to_the_owner(usage, plugin):
    top.bind_owner(plugin)
    bot = top.PanelBot(business_owner_id=OWNER)

    usage.module.offer_notice(bot, BCID)
    await settle(usage)
    usage.module.offer_notice(bot, BCID)
    await settle(usage)

    (call,) = bot.calls
    assert call["chat_id"] == OWNER and call["parse_mode"] == "HTML"
    assert call["text"].startswith("telesec shares usage stats with its author")
    assert "<code>hermes telesec telemetry off</code>" in call["text"]
    assert "<code>DO_NOT_TRACK=1</code>" in call["text"] and "https://usage.sergeybaus.com" in call["text"]
    assert top.keyboard(call["reply_markup"]) == [[("Turn off usage stats", "tb:s:0:settings/u.off")]]
    assert usage.state()["notice"] == 1
    # The process that showed the notice sends nothing; the next gateway start does.
    usage.module.started()
    assert usage.sent == []


@pytest.mark.asyncio
async def test_an_undelivered_notice_is_not_marked_seen(usage, plugin):
    top.bind_owner(plugin)
    bot = top.PanelBot(business_owner_id=OWNER)
    bot.send_error = RuntimeError("Forbidden: bot can't initiate conversation with a user")

    usage.module.offer_notice(bot, BCID)
    await settle(usage)
    usage.module.offer_notice(bot, BCID)  # waits NOTICE_RETRY_SECONDS after a failure
    await settle(usage)

    assert len(bot.calls) == 1
    assert usage.client.notice_text() is not None
    assert usage.state().get("notice", 0) == 0

    bot.send_error = None
    usage.module.offer_notice(bot, BCID, owner_id=OWNER, interactive=True)  # the owner opened the panel
    await settle(usage)
    assert len(bot.delivered_calls) == 1 and usage.state()["notice"] == 1


@pytest.mark.asyncio
async def test_the_notice_goes_only_to_an_owner_who_manages_settings(usage, plugin):
    top.bind_owner(plugin)
    top.bind_owner(plugin, PEER, OTHER, seen_chat=None)  # another account connected the same bot
    bot = top.MultiOwnerBot({BCID: OWNER, OTHER: PEER})

    usage.module.offer_notice(bot, BCID)
    await settle(usage)

    assert bot.calls == []
    assert usage.client.notice_text() is not None


@pytest.mark.asyncio
async def test_nothing_is_offered_while_usage_stats_are_off(usage, plugin, monkeypatch):
    monkeypatch.setenv("TELESEC_TELEMETRY", "0")
    usage.module.reset()
    usage.module.setup(version="1.2.3")
    top.bind_owner(plugin)
    bot = top.PanelBot(business_owner_id=OWNER)

    usage.module.offer_notice(bot, BCID)
    await settle(usage)

    assert bot.calls == []


@pytest.mark.asyncio
async def test_telesec_opens_settings_then_the_notice(usage, gs, plugin):
    top.bind_owner(plugin)
    bot = top.PanelBot(business_owner_id=OWNER)

    assert await top.dispatch(plugin, top.start_update("/telesec"), bot) is True
    await settle(usage)

    settings, notice = bot.calls
    assert "\nKeep history: forever\nUsage stats: on\nDossier model:" in settings["text"]
    assert top.keyboard(settings["reply_markup"])[-1] == SWITCH_ROW
    assert notice["text"].startswith("telesec shares usage stats") and usage.state()["notice"] == 1


# --- Settings -------------------------------------------------------------------------------------


def test_settings_turns_usage_stats_off_and_on(usage, gs, plugin):
    panel = plugin._settings_panel
    top.bind_owner(plugin)

    view = panel.render(ts.ctx(plugin))
    assert "Usage stats: on" in view.text.splitlines()
    assert ts.rows(view)[-1] == SWITCH_ROW

    off = panel.render(ts.ctx(plugin, arg="u.off"))
    assert off.text.splitlines()[1] == "Usage stats are off."
    assert "Usage stats: off" in off.text.splitlines() and ts.rows(off)[-1] == OFF_ROW
    assert usage.client.status()["reason"] == "state" and usage.state()["enabled"] is False
    assert usage.client.notice_text() is None

    on = panel.render(ts.ctx(plugin, arg="u.on", lang="ru"))
    assert on.text.splitlines()[1] == "Статистика использования включена."
    assert "Статистика использования: вкл" in on.text.splitlines()
    assert usage.state()["enabled"] is True and usage.state()["notice"] == 1  # on is an explicit opt-in
    usage.module.feature("settings")
    assert events(usage) == [("app_installed", None), ("feature_used", "settings")]


@pytest.mark.asyncio
async def test_the_notice_button_turns_usage_stats_off(usage, gs, plugin):
    top.bind_owner(plugin)
    bot = top.PanelBot(business_owner_id=OWNER)

    assert await top.dispatch(plugin, top.callback_update("tb:s:0:settings/u.off"), bot) is True

    text, keyboard = top.last_edit(bot)
    assert text.splitlines()[1] == "Usage stats are off."
    assert keyboard[-1] == OFF_ROW
    assert usage.client.status()["enabled"] is False


def test_settings_names_the_variable_that_decides(usage, gs, plugin, monkeypatch):
    monkeypatch.setenv("TELESEC_TELEMETRY", "0")
    usage.module.reset()
    usage.module.setup(version="1.2.3")
    top.bind_owner(plugin)
    panel = plugin._settings_panel

    view = panel.render(ts.ctx(plugin))
    forged = panel.render(ts.ctx(plugin, arg="u.on"))

    assert "Usage stats: off (set by TELESEC_TELEMETRY)" in view.text.splitlines()
    assert ts.rows(view)[-1] != SWITCH_ROW and len(ts.rows(view)) == 4  # no switch while the variable decides
    assert forged.text.splitlines()[1] == "Usage stats are set by TELESEC_TELEMETRY on the gateway."
    assert usage.state() == {}


def test_another_owner_cannot_switch_usage_stats(usage, gs, plugin):
    top.bind_owner(plugin)
    top.bind_owner(plugin, PEER, OTHER, seen_chat=None)

    view = plugin._settings_panel.render(ts.ctx(plugin, PEER, OTHER, arg="u.off"))

    assert view.text.splitlines()[1] == plugin._owner_panel.tr("not_allowed")
    assert "Usage stats: on" in view.text.splitlines() and view.buttons == []
    assert usage.client.status()["enabled"] is True


# --- CLI ------------------------------------------------------------------------------------------


def test_cli_status_shows_the_notice_once_and_marks_it_seen(usage, plugin):
    exit_code, output = ttm.run_cli(plugin, "telemetry")

    assert exit_code == 0
    lines = output.splitlines()
    assert lines[0] == "usage stats: on (on by default)"
    assert "telesec shares usage stats with its author" in output
    assert "Turn off: 'hermes telesec telemetry off', TELESEC_TELEMETRY=0 or DO_NOT_TRACK=1" in output
    assert usage.state()["notice"] == 1

    _code, again = ttm.run_cli(plugin, "telemetry", "status")
    assert "shares usage stats" not in again and "What is sent: https://usage.sergeybaus.com" in again


def test_cli_off_deletes_the_id_and_on_opts_in(usage, plugin):
    seen_elsewhere(usage)
    usage.module.started()
    assert usage.state()["id"]

    assert ttm.run_cli(plugin, "telemetry", "off")[1].startswith("usage stats: off; the install id")
    assert usage.state()["id"] is None and usage.state()["enabled"] is False and usage.state()["daily"] == {}

    exit_code, output = ttm.run_cli(plugin, "telemetry", "on", "--format", "json")
    current = json.loads(output)
    assert exit_code == 0
    assert (current["enabled"], current["reason"]) == (True, "state")
    assert set(current) == {"enabled", "debug", "reason", "id", "stateFile", "endpoint"}

    status = json.loads(ttm.run_cli(plugin, "telemetry", "--json")[1])
    assert status["notice"] is None and status["id"] == current["id"]


def test_cli_off_names_the_variable_that_keeps_it_on(usage, plugin, monkeypatch):
    monkeypatch.setenv("TELESEC_TELEMETRY", "1")
    usage.module.reset()

    _code, output = ttm.run_cli(plugin, "telemetry", "off")

    assert "note: set by TELESEC_TELEMETRY, so this environment keeps them on." in output


@pytest.mark.parametrize(("argv", "expected"), [
    (["settings"], "settings"),
    (["settings", "set", "history", "on"], "settings set"),
    (["mode"], "mode"),
    (["mode", "set", "991", "own"], "mode set"),
    (["chat", "list"], "chat list"),
    (["history"], "history"),
    (["history", "stats"], "history stats"),
    (["stats"], "stats"),
    (["dossier"], "dossier"),
    (["dossier", "status"], "dossier status"),
    (["telemetry", "off"], None),
    ([], None),
])
def test_cli_commands_map_to_their_subcommand_path(plugin, argv, expected):
    parser = argparse.ArgumentParser(prog="hermes telesec")
    plugin._setup_cli(parser)

    name = plugin._usage_stats.cli_feature(parser.parse_args(argv))

    assert name == expected
    assert name is None or name in plugin._usage_stats.FEATURES


def test_a_cli_command_counts_once(usage, plugin):
    seen_elsewhere(usage)

    assert ttm.run_cli(plugin, "history", "stats")[0] == 0
    assert ttm.run_cli(plugin, "history", "stats")[0] == 0

    assert events(usage) == [("app_installed", None), ("feature_used", "history stats")]
