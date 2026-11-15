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
    """The plugin's usage stats with the environment out of the way: off until the owner turns them on. Events
    are recorded in ``usage.sent``, never sent."""
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


def turned_on(usage) -> None:
    """The owner turned usage stats on (`hermes telesec telemetry on` in another process)."""
    assert usage.module.create_client(version="1.2.3").set_enabled(True)


def restart(usage) -> None:
    """A new gateway run."""
    usage.module.reset()
    usage.client = usage.module.setup(version="1.2.3")


def offer_file(usage) -> Path:
    return Path(usage.client.status()["stateFile"]).with_name("usage-notice-sent")


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


def test_nothing_is_sent_by_default(usage):
    usage.module.started()
    usage.module.feature("voice transcribed")
    usage.module.error("stt.failed")

    assert usage.sent == []
    assert usage.state() == {}
    assert (usage.module._status()["enabled"], usage.module._status()["reason"]) == (False, "default")


ID = "8b0e6c1a-3f0d-4a51-9a7e-2f1c5d9e4b77"
UNSET = object()


@pytest.mark.parametrize("enabled", [None, UNSET, False, True], ids=["null", "missing", "off", "on"])
@pytest.mark.parametrize("env", [None, ("TELESEC_TELEMETRY", "1"), ("TELESEC_TELEMETRY", "on"),
                                 ("TELESEC_TELEMETRY", "true"), ("TELESEC_TELEMETRY", "debug"),
                                 ("TELESEC_TELEMETRY", "0"), ("DO_NOT_TRACK", "1")],
                         ids=["unset", "1", "on", "true", "debug", "0", "dnt"])
def test_only_the_owners_stored_choice_turns_usage_stats_on(usage, monkeypatch, enabled, env):
    """A 0.17 install saw the notice (``notice: 1``); only an owner's on counts, whatever the variable says."""
    legacy = {"id": ID, "notice": 1, "installed": True, "daily": {}}
    if enabled is not UNSET:
        legacy["enabled"] = enabled
    path = Path(usage.client.status()["stateFile"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(legacy), encoding="utf-8")
    if env:
        monkeypatch.setenv(*env)
    restart(usage)

    usage.module.started()
    usage.module.feature("voice transcribed")
    usage.module.error("stt.failed")

    veto = env in (("TELESEC_TELEMETRY", "0"), ("DO_NOT_TRACK", "1"))
    on = enabled is True and not veto
    assert usage.module._status()["enabled"] is on
    assert events(usage) == ([("app_started", None), ("feature_used", "voice transcribed"),
                              ("error_occurred", "stt.failed")] if on else [])
    assert (usage.module.panel_row(None, 0, "settings") is None) is veto  # the owner can switch unless vetoed
    assert usage.module._offer_due() is (not veto and enabled in (None, UNSET))


@pytest.mark.parametrize("value", ["1", "on", "true"])
def test_the_variable_neither_turns_usage_stats_on_nor_keeps_them_on(usage, plugin, monkeypatch, value):
    monkeypatch.setenv("TELESEC_TELEMETRY", value)
    restart(usage)
    usage.module.started()
    assert usage.sent == [] and usage.module._status()["reason"] == "default"

    assert ttm.run_cli(plugin, "telemetry", "on")[0] == 0
    usage.module.started()
    assert events(usage) == [("app_installed", None), ("app_started", None)]

    _code, output = ttm.run_cli(plugin, "telemetry", "off")
    assert "note:" not in output
    restart(usage)
    usage.module.started()
    usage.module.feature("voice transcribed")
    assert len(usage.sent) == 2
    assert (usage.module._status()["enabled"], usage.module._status()["reason"]) == (False, "state")


def test_events_carry_fixed_names_and_error_types_only(usage):
    turned_on(usage)

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
    turned_on(usage)

    for _ in range(3):
        usage.module.transcribed("voice", cleaned=True)
        usage.module.transcribed("video_note", cleaned=False)
        usage.module.transcribed("audio_document", cleaned=False)

    assert events(usage) == [("app_installed", None), ("feature_used", "voice transcribed"),
                             ("feature_used", "transcript cleanup"), ("feature_used", "video note transcribed"),
                             ("feature_used", "audio file transcribed")]


def test_dossier_cycles_count_updates_and_failure_codes(usage, plugin):
    turned_on(usage)
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
    turned_on(usage)
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
    assert call["text"].startswith("Telesec can share usage stats with its author")
    assert "Usage stats are off: nothing is sent unless you turn them on" in call["text"]
    assert "<code>hermes telesec telemetry on</code>" in call["text"] and "https://usage.sergeybaus.com" in call["text"]
    assert top.keyboard(call["reply_markup"]) == [[("Enable usage stats", "tb:s:0:settings/u.on")]]
    assert offer_file(usage).exists()
    # Sending the notice turns nothing on, in this gateway run or the next, and it is not sent again.
    usage.module.started()
    restart(usage)
    usage.module.started()
    usage.module.feature("voice transcribed")
    usage.module.offer_notice(bot, BCID)
    await settle(usage)
    assert usage.sent == [] and usage.state() == {} and len(bot.calls) == 1


@pytest.mark.asyncio
async def test_an_undelivered_notice_is_tried_again(usage, plugin):
    top.bind_owner(plugin)
    bot = top.PanelBot(business_owner_id=OWNER)
    bot.send_error = RuntimeError("Forbidden: bot can't initiate conversation with a user")

    usage.module.offer_notice(bot, BCID)
    await settle(usage)
    usage.module.offer_notice(bot, BCID)  # waits NOTICE_RETRY_SECONDS after a failure
    await settle(usage)

    assert len(bot.calls) == 1
    assert not offer_file(usage).exists()

    bot.send_error = None
    usage.module.offer_notice(bot, BCID, owner_id=OWNER, interactive=True)  # the owner opened the panel
    await settle(usage)
    assert len(bot.delivered_calls) == 1 and offer_file(usage).exists()


@pytest.mark.asyncio
async def test_the_notice_goes_only_to_an_owner_who_manages_settings(usage, plugin):
    top.bind_owner(plugin)
    top.bind_owner(plugin, PEER, OTHER, seen_chat=None)  # another account connected the same bot
    bot = top.MultiOwnerBot({BCID: OWNER, OTHER: PEER})

    usage.module.offer_notice(bot, BCID)
    await settle(usage)

    assert bot.calls == []
    assert not offer_file(usage).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("decided", ["variable", "off", "on"])
async def test_nothing_is_offered_once_decided(usage, plugin, monkeypatch, decided):
    if decided == "variable":
        monkeypatch.setenv("TELESEC_TELEMETRY", "0")
        restart(usage)
    else:
        assert usage.client.set_enabled(decided == "on")
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
    assert "\nUsage stats: off\nDossier model:" in settings["text"]
    assert top.keyboard(settings["reply_markup"])[-1] == OFF_ROW
    assert notice["text"].startswith("Telesec can share usage stats") and offer_file(usage).exists()
    assert usage.state() == {} and usage.sent == []


# --- Settings -------------------------------------------------------------------------------------


def test_settings_turns_usage_stats_on_and_off(usage, gs, plugin):
    panel = plugin._settings_panel
    top.bind_owner(plugin)

    view = panel.render(ts.ctx(plugin))
    assert "Usage stats: off" in view.text.splitlines()
    assert ts.rows(view)[-1] == OFF_ROW

    on = panel.render(ts.ctx(plugin, arg="u.on", lang="ru"))
    assert on.text.splitlines()[1] == "Статистика использования включена."
    assert "Статистика использования: вкл" in on.text.splitlines()
    assert usage.state()["enabled"] is True and usage.state()["notice"] == 1  # on is an explicit opt-in
    usage.module.feature("settings")
    assert events(usage) == [("app_installed", None), ("feature_used", "settings")]

    off = panel.render(ts.ctx(plugin, arg="u.off"))
    assert off.text.splitlines()[1] == "Usage stats are off."
    assert "Usage stats: off" in off.text.splitlines() and ts.rows(off)[-1] == OFF_ROW
    assert usage.client.status()["reason"] == "state" and usage.state()["enabled"] is False
    usage.module.feature("settings changed")
    assert len(usage.sent) == 2


@pytest.mark.asyncio
async def test_the_notice_button_turns_usage_stats_on(usage, gs, plugin):
    top.bind_owner(plugin)
    bot = top.PanelBot(business_owner_id=OWNER)

    assert await top.dispatch(plugin, top.callback_update("tb:s:0:settings/u.on"), bot) is True

    text, keyboard = top.last_edit(bot)
    assert text.splitlines()[1] == "Usage stats are on."
    assert keyboard[-1] == SWITCH_ROW
    assert usage.module._status() | {"id": None} == usage.client.status() | {"enabled": True, "reason": "state",
                                                                               "id": None}


def test_settings_names_the_variable_that_decides(usage, gs, plugin, monkeypatch):
    monkeypatch.setenv("TELESEC_TELEMETRY", "0")
    usage.module.reset()
    usage.module.setup(version="1.2.3")
    top.bind_owner(plugin)
    panel = plugin._settings_panel

    view = panel.render(ts.ctx(plugin))
    forged = panel.render(ts.ctx(plugin, arg="u.on"))

    assert "Usage stats: off (set by TELESEC_TELEMETRY)" in view.text.splitlines()
    assert ts.rows(view)[-1] != SWITCH_ROW and len(ts.rows(view)) == 5  # no switch while the variable decides
    assert forged.text.splitlines()[1] == "Usage stats are set by TELESEC_TELEMETRY on the gateway."
    assert usage.state() == {}


def test_another_owner_cannot_switch_usage_stats(usage, gs, plugin):
    turned_on(usage)
    top.bind_owner(plugin)
    top.bind_owner(plugin, PEER, OTHER, seen_chat=None)

    view = plugin._settings_panel.render(ts.ctx(plugin, PEER, OTHER, arg="u.off"))

    assert view.text.splitlines()[1] == plugin._owner_panel.tr("not_allowed")
    assert "Usage stats: on" in view.text.splitlines() and view.buttons == []
    assert usage.module._status()["enabled"] is True


# --- CLI ------------------------------------------------------------------------------------------


def test_cli_output_read_by_an_agent_turns_nothing_on(usage, plugin, monkeypatch):
    monkeypatch.setattr(usage.client, "agent", "codex")  # an AI agent runs the commands and reads the output
    exit_code, output = ttm.run_cli(plugin, "telemetry")

    assert exit_code == 0
    lines = output.splitlines()
    assert lines[0] == "usage stats: off (off until you turn them on)"
    assert "What is sent: https://usage.sergeybaus.com" in output
    assert "Turn on: 'hermes telesec telemetry on' or Usage stats in Settings" in output
    assert "Turn off: 'hermes telesec telemetry off', TELESEC_TELEMETRY=0 or DO_NOT_TRACK=1" in output

    assert ttm.run_cli(plugin, "history", "stats")[0] == 0
    assert json.loads(ttm.run_cli(plugin, "telemetry", "--json")[1])["enabled"] is False
    assert usage.state() == {} and usage.sent == []


def test_cli_off_deletes_the_id_and_on_opts_in(usage, plugin):
    turned_on(usage)
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
    assert status == current


def test_cli_on_names_the_variable_that_keeps_it_off(usage, plugin, monkeypatch):
    monkeypatch.setenv("TELESEC_TELEMETRY", "0")
    usage.module.reset()

    _code, output = ttm.run_cli(plugin, "telemetry", "on")

    assert "note: set by TELESEC_TELEMETRY, so this environment keeps them off." in output
    usage.module.started()
    assert usage.sent == []


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
    turned_on(usage)

    assert ttm.run_cli(plugin, "history", "stats")[0] == 0
    assert ttm.run_cli(plugin, "history", "stats")[0] == 0

    assert events(usage) == [("app_installed", None), ("feature_used", "history stats")]
