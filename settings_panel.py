"""Owner-panel Settings: the switches for transcription, history, dossiers and deletion alerts in all chats, and usage stats.

Settings opens from the Settings button on any chat's panel, or with `/telesec` in the owner's
private chat with the bot (chat ``owner_panel.HOME_CHAT``, with no Back row). Only an owner of every
Business connection the plugin knows may change the switches and history retention
(``dossier_panel.manages_global``); other verified owners see them read-only. Buttons carry target
values, so a stale or repeated tap changes nothing. The models in use are shown, never
changed, here. Nothing here is ever posted into a Business chat.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from . import dossier_panel as _dossier_panel
from . import dossier_support as _ds
from . import dossier_view as _dossier_view
from . import global_settings as _settings
from . import history_support as _history
from . import owner_panel as _owner_panel
from . import usage_stats as _usage

logger = logging.getLogger(__name__)

SECTION_KEY = _owner_panel.SETTINGS_SECTION
SECTION_LABEL = "⚙️ Settings"
ORDER = 900  # after every chat section
# "<feature>.<target value>"; "d.ask" opens the dossier confirm, since dossiers send chat text to a model,
# and only the confirm's "d.yes" turns them on.
SWITCH_ARGS = ("t.on", "t.off", "h.on", "h.off", "d.ask", "d.yes", "d.on", "d.off", "a.on", "a.off")
# "r" opens the history retention picker.
RETENTION_PICKER = "r"
# "r.<days>" keeps history that many days (0: forever). A shorter value than the one in effect opens a
# confirm, and only its "r.<days>.yes" applies it. A value outside the choices (a migrated one) is
# accepted only while it is in effect, so a forged button cannot store an arbitrary number.
RETENTION_ARGS = (*(f"r.{days}" for days in _settings.RETENTION_CHOICES),
                  *(f"r.{days}.yes" for days in _settings.RETENTION_CHOICES if days))
ARGS = (*SWITCH_ARGS, RETENTION_PICKER, *RETENTION_ARGS, *_usage.PANEL_ARGS)
_FEATURES = {"t": "transcription", "h": "history", "d": "dossiers", "a": "alerts"}
_RETENTION_ARG_RE = re.compile(r"^r\.(\d{1,5})(\.yes)?\Z")
CURRENT_MARK = "• "
MODEL_NAME_MAX = 120

STRINGS = {
    "en": {
        f"section.{SECTION_KEY}": SECTION_LABEL,
        # Never "Owner panel:": the agent treats a reply to that as a question about one chat.
        "settings.title": "Telesec settings · all chats",
        "settings.one_chat": "One chat: Manage Bot in that chat.",
        "settings.done.t.on": "Transcription is on.",
        "settings.done.t.off": "Transcription is off in all chats.",
        "settings.done.h.on": "History is on.",
        "settings.done.h.off": "History is off in all chats.",
        "settings.done.d.on": "Dossiers are on.",
        "settings.done.d.off": "Dossiers are off in all chats.",
        "settings.done.a.on": "Deletion alerts are on.",
        "settings.done.a.off": "Deletion alerts are off in all chats.",
        "settings.needs_history": "Dossiers need history. Turn history on first.",
        "settings.paused": "Dossiers are paused while history is off.",
        "settings.paused.alerts": "Deletion alerts are paused while history is off.",
        "settings.paused.both": "Dossiers and deletion alerts are paused while history is off.",
        "settings.back": "‹ Settings",
        "settings.save_error": "Couldn't save: the settings file is unreadable (see hermes telesec settings).",
        "settings.status.transcription": "Transcription: {state}",
        "settings.status.history": "History: {state}",
        "settings.status.dossiers": "Dossiers: {state}",
        "settings.status.alerts": "Deletion alerts: {state}",
        "settings.state.on": "on",
        "settings.state.off": "off",
        "settings.read_only": "Only the owner of every connected account can change these.",
        "settings.retention.forever": "forever",
        "settings.retention.forever_button": "Forever",
        "settings.retention.year": "1 year",
        "settings.retention.day": "{days} day",
        "settings.retention.few": "{days} days",
        "settings.retention.days": "{days} days",
        "settings.status.retention": "Keep history: {value}",
        "settings.retention.ask": "Keep {value}?",
        "settings.retention.explain": ("Saved messages are deleted a whole month at a time: a month is deleted at "
                                       "the next cleanup once all of it is older than that."),
        "settings.retention.yes": "Keep {value}",
        "settings.retention.cancel": "Cancel",
        "settings.retention.done": "History is kept {value}.",
        "settings.model.dossiers": "Dossier model: {name}",
        "settings.model.main": "your main model",
        "settings.model.gateway_options": ("Warning: its extra_body has providerOptions for ai-gateway only, so this "
                                           "model may reject dossier calls (see hermes telesec dossier status)."),
        "settings.model.cleanup": "Voice cleanup: {name}",
    },
    "ru": {
        f"section.{SECTION_KEY}": "⚙️ Настройки",
        "settings.title": "Настройки Telesec · все чаты",
        "settings.one_chat": "Для одного чата — кнопка управления ботом в нём.",
        "settings.done.t.on": "Расшифровка включена.",
        "settings.done.t.off": "Расшифровка выключена во всех чатах.",
        "settings.done.h.on": "История включена.",
        "settings.done.h.off": "История выключена во всех чатах.",
        "settings.done.d.on": "Досье включены.",
        "settings.done.d.off": "Досье выключены во всех чатах.",
        "settings.done.a.on": "Уведомления об удалении включены.",
        "settings.done.a.off": "Уведомления об удалении выключены во всех чатах.",
        "settings.needs_history": "Для досье нужна история. Сначала включите её.",
        "settings.paused": "Досье на паузе, пока история выключена.",
        "settings.paused.alerts": "Уведомления об удалении на паузе, пока история выключена.",
        "settings.paused.both": "Досье и уведомления об удалении на паузе, пока история выключена.",
        "settings.back": "‹ Настройки",
        "settings.save_error": "Не удалось сохранить: файл настроек не читается (см. hermes telesec settings).",
        "settings.status.transcription": "Расшифровка: {state}",
        "settings.status.history": "История: {state}",
        "settings.status.dossiers": "Досье: {state}",
        "settings.status.alerts": "Уведомления об удалении: {state}",
        "settings.state.on": "вкл",
        "settings.state.off": "выкл",
        "settings.read_only": "Менять их может только владелец всех подключённых аккаунтов.",
        "settings.retention.forever": "всегда",
        "settings.retention.forever_button": "Всегда",
        "settings.retention.year": "1 год",
        "settings.retention.day": "{days} день",
        "settings.retention.few": "{days} дня",
        "settings.retention.days": "{days} дней",
        "settings.status.retention": "Хранить историю: {value}",
        "settings.retention.ask": "Хранить {value}?",
        "settings.retention.explain": ("Сохранённые сообщения удаляются целыми месяцами: месяц удаляется при "
                                       "ближайшей очистке, когда весь он старше этого срока."),
        "settings.retention.yes": "Хранить {value}",
        "settings.retention.cancel": "Отмена",
        "settings.retention.done": "История хранится {value}.",
        "settings.model.dossiers": "Модель досье: {name}",
        "settings.model.main": "ваша основная модель",
        "settings.model.gateway_options": ("Внимание: в её extra_body есть providerOptions только для ai-gateway, "
                                           "поэтому эта модель может отклонять вызовы досье "
                                           "(см. hermes telesec dossier status)."),
        "settings.model.cleanup": "Очистка расшифровок: {name}",
    },
}


# The voice cleanup model as (provider, model), or None while cleanup is off; bound by register_section,
# since the plugin module owns that configuration.
_voice_cleanup: Callable[[], tuple[str, str] | None] | None = None


def _tr(key: str, ctx: _owner_panel.PanelContext, **values: Any) -> str:
    return _owner_panel.tr(key, ctx.language_code, **values)


def _days_key(days: int, lang: str) -> str:
    """The plural form: English has day/days, Russian день/дня/дней (1, 21 / 2-4, 22 / 5-20, 25)."""
    if lang != "ru":
        return "day" if days == 1 else "days"
    if days % 10 == 1 and days % 100 != 11:
        return "day"
    if 2 <= days % 10 <= 4 and not 12 <= days % 100 <= 14:
        return "few"
    return "days"


def _retention_value(ctx: _owner_panel.PanelContext, days: int) -> str:
    if days == 0:
        return _tr("settings.retention.forever", ctx)
    if days == 365:
        return _tr("settings.retention.year", ctx)
    key = _days_key(days, _owner_panel.language(ctx.language_code))
    return _tr(f"settings.retention.{key}", ctx, days=days)


def _model_name(provider: str, model: str) -> str:
    """``provider / model`` as configured, without URL credentials, queries or fragments."""
    parts = [_ds.without_credentials(str(part)) for part in (provider, model)]
    return _ds.clip(" / ".join(part for part in parts if part) or "?", MODEL_NAME_MAX)


def _model_lines(ctx: _owner_panel.PanelContext) -> list[str]:
    cfg = _ds.load_config()
    # "auto" is the dossiers task following the main model; its "main model" is a placeholder, not a name.
    # The deprecated TELESEC_DOSSIER_PROVIDER=auto route names its model, as in the enable confirm.
    main = cfg.provider == "auto" and not cfg.legacy_env
    name = _tr("settings.model.main", ctx) if main else _model_name(cfg.provider, cfg.model)
    lines = [_tr("settings.model.dossiers", ctx, name=name)]
    if "gateway_options" in cfg.warning_codes:
        lines.append(_tr("settings.model.gateway_options", ctx))
    if _voice_cleanup is not None:
        cleanup = _voice_cleanup()
        name = _model_name(*cleanup) if cleanup else _tr("settings.state.off", ctx)
        lines.append(_tr("settings.model.cleanup", ctx, name=name))
    return lines


def _callback(ctx: _owner_panel.PanelContext, sub: str | None = None) -> str:
    return _owner_panel.section_callback(ctx.chat_id, SECTION_KEY, sub)


def _apply(ctx: _owner_panel.PanelContext, arg: str) -> str:
    """Write one switch; returns the notice line. Blocking."""
    feature, on = _FEATURES[arg[0]], arg.endswith(".on")
    if feature == "dossiers" and on and not _settings.enabled("history"):
        return _tr("settings.needs_history", ctx)
    try:
        if feature == "dossiers":
            _dossier_panel.switch(on)
        else:
            _settings.set_enabled(feature, on, via="telegram")
            if feature == "history" and on and _settings.enabled("dossiers"):
                _dossier_panel.wake()  # paused dossiers resume without waiting for the next poll
    except (_settings.SettingsFileCorrupt, OSError) as exc:
        # Telegram never overwrites a corrupt file; `hermes telesec settings set` repairs it.
        logger.warning("%s: could not save settings (%s)", _history.PLUGIN_NAME, type(exc).__name__)
        return _tr("settings.save_error", ctx)
    _usage.feature("settings changed")
    return _tr(f"settings.done.{arg}", ctx)


def _retention_target(arg: str) -> tuple[int, bool] | None:
    """(days, confirmed) of a retention arg, else None."""
    match = _RETENTION_ARG_RE.match(arg)
    return None if match is None else (int(match.group(1)), match.group(2) is not None)


def _apply_retention(ctx: _owner_panel.PanelContext, days: int) -> str:
    """Store the retention; returns the notice line. Blocking."""
    try:
        _settings.set_retention(days, via="telegram")
    except (_settings.SettingsFileCorrupt, OSError) as exc:
        logger.warning("%s: could not save settings (%s)", _history.PLUGIN_NAME, type(exc).__name__)
        return _tr("settings.save_error", ctx)
    _usage.feature("settings changed")
    return _tr("settings.retention.done", ctx, value=_retention_value(ctx, days))


def _retention_row(ctx: _owner_panel.PanelContext, current: int) -> list[tuple[str, str]]:
    """Forever first, then shorter and shorter; a value in effect outside the choices gets its own button."""
    choices = sorted({*_settings.RETENTION_CHOICES, current}, key=lambda days: -days if days else float("-inf"))
    row = []
    for days in choices:
        # Short labels, like the stats periods: four or five buttons share one row on a phone.
        label = _tr("settings.retention.forever_button", ctx) if days == 0 else _retention_value(ctx, days)
        row.append(((CURRENT_MARK if days == current else "") + label, _callback(ctx, f"r.{days}")))
    return row


def _toggle(ctx: _owner_panel.PanelContext, feature: str, on: bool, on_arg: str | None = None) -> list[tuple[str, str]]:
    """One switch button that sets the other value; ``on_arg`` replaces "<letter>.on"."""
    letter = feature[0]
    target = f"{letter}.off" if on else (on_arg or f"{letter}.on")
    return [(_owner_panel.toggle_label(feature, on, ctx.language_code), _callback(ctx, target))]


def _paused_line(ctx: _owner_panel.PanelContext, current: dict[str, bool]) -> str | None:
    if current["history"]:
        return None
    if current["dossiers"] and current["alerts"]:
        return _tr("settings.paused.both", ctx)
    if current["dossiers"]:
        return _tr("settings.paused", ctx)
    return _tr("settings.paused.alerts", ctx) if current["alerts"] else None


def _main_view(ctx: _owner_panel.PanelContext, manager: bool, notice: str | None) -> _owner_panel.PanelView:
    current = {feature: _settings.enabled(feature) for feature in _settings.FEATURES}
    retention = _settings.retention_days()
    lines = [_tr("settings.title", ctx)]
    if notice:
        lines.append(notice)
    if manager and ctx.chat_id == str(_owner_panel.HOME_CHAT):
        lines.append(_tr("settings.one_chat", ctx))
    paused = _paused_line(ctx, current)
    if paused:
        lines.append(paused)
    if not manager:
        for feature in _settings.FEATURES:
            state = _tr(f"settings.state.{'on' if current[feature] else 'off'}", ctx)
            icon = _owner_panel.ICONS[feature]
            lines.append(f"{icon} {_tr(f'settings.status.{feature}', ctx, state=state)}")
            if feature == "history":
                value = _retention_value(ctx, retention)
                lines.append(f"{_owner_panel.ICONS['retention']} {_tr('settings.status.retention', ctx, value=value)}")
        lines += _usage.panel_lines(ctx.language_code)
        lines += _model_lines(ctx)
        lines.append(_tr("settings.read_only", ctx))
        return _owner_panel.PanelView(text="\n".join(lines))
    lines += _usage.panel_lines(ctx.language_code)
    lines += _model_lines(ctx)
    retention_label = _owner_panel.picker_label("retention", _retention_value(ctx, retention), ctx.language_code)
    buttons = [
        _toggle(ctx, "transcription", current["transcription"]),
        _toggle(ctx, "history", current["history"]),
        # Shown while history is off too: maintenance keeps pruning what was saved.
        [(retention_label, _callback(ctx, RETENTION_PICKER))],
        # Always shown, so dossiers can always be turned off; turning them on asks first.
        _toggle(ctx, "dossiers", current["dossiers"], "d.ask"),
        _toggle(ctx, "alerts", current["alerts"]),
    ]
    usage_row = _usage.panel_row(ctx.language_code, ctx.chat_id, SECTION_KEY)
    if usage_row:  # none while the gateway's environment decides
        buttons.append(usage_row)
    return _owner_panel.PanelView(text="\n".join(lines), buttons=buttons)


def _retention_picker_view(ctx: _owner_panel.PanelContext) -> _owner_panel.PanelView:
    """The retention picker; "‹ Settings" differs from the router's "« Back" to the chat's panel."""
    retention = _settings.retention_days()
    value = _retention_value(ctx, retention)
    text = "\n".join([_tr("settings.title", ctx),
                      f"{_owner_panel.ICONS['retention']} {_tr('settings.status.retention', ctx, value=value)}",
                      _tr("settings.retention.explain", ctx)])
    return _owner_panel.PanelView(text=text, buttons=[_retention_row(ctx, retention),
                                                      [(_tr("settings.back", ctx), _callback(ctx))]])


def _confirm_view(ctx: _owner_panel.PanelContext) -> _owner_panel.PanelView:
    text, yes, cancel = _dossier_view.enable_confirm(_ds.load_config(), _dossier_view.lang_for(ctx.language_code))
    return _owner_panel.PanelView(text=text, buttons=[[(yes, _callback(ctx, "d.yes")), (cancel, _callback(ctx))]])


def _retention_confirm_view(ctx: _owner_panel.PanelContext, days: int) -> _owner_panel.PanelView:
    value = _retention_value(ctx, days)
    text = f"{_tr('settings.retention.ask', ctx, value=value)}\n\n{_tr('settings.retention.explain', ctx)}"
    return _owner_panel.PanelView(text=text, buttons=[[
        (_tr("settings.retention.yes", ctx, value=value), _callback(ctx, f"r.{days}.yes")),
        (_tr("settings.retention.cancel", ctx), _callback(ctx)),
    ]])


def _known_arg(arg: str | None) -> str | None:
    if arg in ARGS or arg == f"r.{_settings.retention_days()}":
        return arg
    return None


def render(ctx: _owner_panel.PanelContext) -> _owner_panel.PanelView:
    """The Settings screen; ``ctx.arg`` is a tapped button from ARGS. Blocking: the router runs it in a thread."""
    connection = ctx.business_connection_id or next(iter(ctx.owner_connections), None)
    manager = _dossier_panel.manages_global(ctx.owner_user_id, connection)
    arg = _known_arg(ctx.arg)
    notice = None
    if arg is not None and not manager:
        logger.info("%s: settings change refused reason=owner_scope", _history.PLUGIN_NAME)
        notice = _owner_panel.tr("not_allowed", ctx.language_code)
    elif arg == RETENTION_PICKER:
        return _retention_picker_view(ctx)
    elif arg == "d.ask" or (arg == "d.on" and not _settings.enabled("dossiers")):
        # A stale "• Keep dossiers" from before dossiers were turned off asks again, never enables.
        if _settings.enabled("history"):
            return _confirm_view(ctx)
        notice = _tr("settings.needs_history", ctx)
    elif arg in _usage.PANEL_ARGS:
        notice = _usage.panel_apply(arg, ctx.language_code)
    elif arg is not None and (target := _retention_target(arg)) is not None:
        days, confirmed = target
        # Judged against the value in effect now, so a stale button for a shorter value asks again.
        if not confirmed and _settings.keeps_less(days, _settings.retention_days()):
            return _retention_confirm_view(ctx, days)
        notice = _apply_retention(ctx, days)
    elif arg is not None:
        notice = _apply(ctx, "d.on" if arg == "d.yes" else arg)
    return _main_view(ctx, manager, notice)


def register_section(voice_cleanup: Callable[[], tuple[str, str] | None] | None = None) -> None:
    """Register Settings; ``voice_cleanup`` returns the voice cleanup (provider, model), or None while it is off."""
    global _voice_cleanup
    if voice_cleanup is not None:
        _voice_cleanup = voice_cleanup
    _owner_panel.register_strings(STRINGS)
    _usage.register_strings()
    _owner_panel.register_section(SECTION_KEY, SECTION_LABEL, render, order=ORDER)
