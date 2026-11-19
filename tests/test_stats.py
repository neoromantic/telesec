from __future__ import annotations

import argparse
import importlib
import itertools
import json
import re
import shlex
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest
import test_history as th
from test_history import (
    FakeBot,
    _build_history_parser,
    _run_history_cli,
    append_raw_history_records,
    catalog_dirty_path,
    make_business_text_update,
    make_deleted_update,
    make_history_record,
    snapshot_history_state,
)

plugin = th.plugin
enabled_history = th.enabled_history

ROOT = Path(__file__).resolve().parents[1]
STATS_DOC = ROOT / "docs" / "STATS.md"
SKILL_DOC = ROOT / "skills" / "history" / "SKILL.md"
_FENCED_BLOCK_RE = re.compile(r"^```(\w+)\n(.*?)\n```$", flags=re.MULTILINE | re.DOTALL)

NOW = datetime(2026, 10, 19, 11, 32, tzinfo=UTC)
ALICE = {"id": 991, "type": "private", "username": "alice", "first_name": "Alice", "last_name": "Example"}
BOB = {"id": 992, "type": "private", "username": "bob", "first_name": "Bob", "last_name": "Builder"}
CAROL = {"id": 994, "type": "private", "first_name": "Carol"}
DAVE = {"id": 995, "type": "private", "username": "dave", "first_name": "Dave"}
MOSCOW_RANGE = ("--since", "2026-10-01", "--until", "2026-10-18", "--tz", "Europe/Moscow")
_UPDATE_IDS = itertools.count(10_000)
_UNSET: Any = object()

ALICE_MOSCOW = """
range 2026-10-01..2026-10-18 Europe/Moscow, by Telegram send time; text messages only
Alice Example (@alice) connection=business-123 chat=991
  messages 9: received 5, sent 3, unknown direction 1
  active days 4 (received 3, sent 3)
  latest 2026-10-17 21:04 received; last sent 2026-10-17 19:30
  last message: received 2026-10-17 21:04, no observed reply after it
  sessions 4 (gap 6h): started by contact 2, by owner 2; ended on received 2
  share received 63%, sent 37%
  turns 7 (received 4, sent 3); messages per turn received 1.3, sent 1.0
  replies received 2: median 52m, p90 1h34m; sent 1: median 30m, p90 30m
  text length median received 10, sent 10; edited 2 (received 0, sent 1, unknown direction 1)
  peak Sat 12:00; hours 0..23: 1 0 0 0 0 0 0 0 0 0 0 2 3 0 0 0 0 0 0 1 0 1 0 1; weekdays Mon..Sun: 0 0 0 0 0 8 1
  activity by day from 2026-10-01: 0 0 3 1 0 0 0 0 0 3 0 0 0 0 0 0 2 0
  streak current 1d, longest 2d (2026-10-03..2026-10-04); longest silence 7d07h (2026-10-10 12:14..2026-10-17 19:30)
  deletions 4: unexplained 1 (received 1, sent 0), likely correction 1, likely duplicate 1, pending 1
  missing history: retained from 2026-10-03 (range is partial); 1 deletion of a message not in history; 1 message known only from an edit
  deletion details: hermes telesec history deletions --connection business-123 --chat 991 --status unexplained
""".strip("\n")

# The last 7 days run to now, so the report has a waiting line and, with history
# retained from 2026-10-03, the previous 7 days to compare with.
ALICE_WEEK_MOSCOW = """
range 2026-10-12 14:32..now Europe/Moscow, by Telegram send time; text messages only
Alice Example (@alice) connection=business-123 chat=991
  messages 3: received 2, sent 1, unknown direction 0
  active days 2 (received 2, sent 1)
  latest 2026-10-19 00:00 received; last sent 2026-10-17 19:30
  last message: received 2026-10-19 00:00, no observed reply after it
  sessions 2 (gap 6h): started by contact 1, by owner 1; ended on received 2
  share received 67%, sent 33%
  turns 2 (received 1, sent 1); messages per turn received 2.0, sent 1.0
  replies received 1: median 1h34m, p90 1h34m; sent 0
  waiting received since 2026-10-17 21:04 (1d17h)
  text length median received 10, sent 10; edited 0
  peak Sat 00:00; hours 0..23: 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 1 0 1 0 0; weekdays Mon..Sun: 1 0 0 0 0 2 0
  activity by day from 2026-10-12: 0 0 0 0 0 2 0 1
  streak current 1d, longest 1d (2026-10-17..2026-10-17); longest silence 1d02h (2026-10-17 21:04..2026-10-19 00:00)
  previous 2026-10-05 14:32..2026-10-12 14:31: messages 3 -> 3 (+0%); active days 1 -> 2; reply median received 10m -> 1h34m, sent -
  deletions 1: pending 1
  missing history: none (retained from 2026-10-03)
""".strip("\n")

OVERVIEW_MOSCOW = """
range 2026-10-01..2026-10-18 Europe/Moscow, by Telegram send time; text messages only
latest            last      msgs  recv  sent  unk  days  sess  del  unexpl  partial  connection    chat  contact
2026-10-18 09:15  sent         2     1     1    0     1     1    0       0  yes      business-123  992   Bob Builder (@bob)
2026-10-17 21:04  received     9     5     3    1     4     4    4       1  yes      business-123  991   Alice Example (@alice)
-                 -            0     0     0    0     0     0    1       0  no       business-456  994   Carol
shown 3 of 4 contacts; not shown: 1 with no activity in range
missing history in shown contacts: 1 deletion of a message not in history; 1 message known only from an edit; use --contact for details
deletion details: hermes telesec history deletions --status unexplained
""".strip("\n")


@pytest.fixture(autouse=True)
def _fixed_clock(plugin, monkeypatch: pytest.MonkeyPatch):
    # No Hermes time zone, even when the tests run inside a Hermes install.
    monkeypatch.delenv("HERMES_TIMEZONE", raising=False)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", None)
    monkeypatch.setattr(plugin._history_support, "_utcnow", lambda: NOW)


def _hermes_tz(monkeypatch: pytest.MonkeyPatch, name: Any) -> None:
    """Stub ``timezone`` in Hermes's config (``hermes_cli.config``); an exception value is raised."""

    def load_config_readonly():
        if isinstance(name, Exception):
            raise name
        return {"timezone": name}

    package = ModuleType("hermes_cli")
    package.__path__ = []
    package.config = SimpleNamespace(load_config_readonly=load_config_readonly)
    monkeypatch.delenv("HERMES_TIMEZONE", raising=False)
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", package.config)


@pytest.fixture
def cs(plugin):
    return importlib.import_module(f"{plugin.__name__}.correspondence_stats")


def _utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


def _msg(
    plugin,
    mid: int,
    at: datetime | None,
    direction: str,
    *,
    edited: bool = False,
    observed: datetime | None = None,
    message_at: Any = _UNSET,
    conn: str = "business-123",
    chat: int = 991,
    profile: dict[str, Any] | None = ALICE,
) -> dict[str, Any]:
    assert at is not None or observed is not None
    record = make_history_record(
        plugin,
        event_type="message.edited" if edited else "message.created",
        source="edited_business_message" if edited else "business_message",
        observed_at=observed or at + timedelta(seconds=1),
        telegram_update_id=next(_UPDATE_IDS),
        business_connection_id=conn,
        chat_id=chat,
        message_id=mid,
        message_at=at if message_at is _UNSET else message_at,
        direction=direction,
        text=f"SECRET-{mid}",
    )
    if profile is not None:
        # Profiles sit outside the event_id signature, so they can be attached afterwards.
        record["chat_profile"] = dict(profile, id=chat)
    return record


def _tomb(
    plugin,
    mid: int,
    observed: datetime,
    direction: str = "unknown",
    *,
    message_at: datetime | None = None,
    conn: str = "business-123",
    chat: int = 991,
) -> dict[str, Any]:
    return make_history_record(
        plugin,
        event_type="message.deleted",
        source="deleted_business_messages",
        observed_at=observed,
        telegram_update_id=next(_UPDATE_IDS),
        business_connection_id=conn,
        chat_id=chat,
        message_id=mid,
        message_at=message_at,
        sender_id=None if message_at is None else 2000,
        direction=direction,
    )


def _cls(
    plugin,
    tomb: dict[str, Any],
    classification: str,
    observed: datetime,
    reason: str,
    *,
    replacement: int | None = None,
    score: float | None = None,
) -> dict[str, Any]:
    history = plugin._history_support
    return make_history_record(
        plugin,
        event_type="deletion.classified",
        source=tomb["source"],
        observed_at=observed,
        telegram_update_id=tomb["telegram_update_id"],
        business_connection_id=tomb["business_connection_id"],
        chat_id=tomb["chat_id"],
        message_id=tomb["message_id"],
        message_at=history._parse_datetime(tomb["message_at"]),
        sender_id=tomb["sender_id"],
        direction=tomb["direction"],
        deleted_event_id=tomb["event_id"],
        classification=classification,
        replacement_message_id=replacement,
        classification_reason=reason,
        classification_score=score,
        evaluated_at=observed,
        deleted_observed_at=history._parse_datetime(tomb["observed_at"]),
    )


def _profile(chat: int, username: str) -> dict[str, Any]:
    return {"id": chat, "type": "private", "username": username, "first_name": username.title()}


def _seed(plugin, *records: dict[str, Any]) -> None:
    append_raw_history_records(plugin, *records)
    plugin._history_support.rebuild_contact_catalog()


def _stats(plugin, *argv: str) -> tuple[int, str]:
    return _run_history_cli(plugin, "stats", *argv)


def _stats_json(plugin, *argv: str) -> dict[str, Any]:
    exit_code, output = _stats(plugin, *argv, "--format", "json")
    assert exit_code == 0, output
    return json.loads(output)


def _contact_json(plugin, *argv: str) -> dict[str, Any]:
    document = _stats_json(plugin, *argv)
    assert document["mode"] == "contact"
    return document["contacts"][0]


def _entry(
    *,
    conn: str = "business-123",
    chat: int = 991,
    profile: dict[str, Any] = ALICE,
    first_seen_at: str | None = None,
) -> dict[str, Any]:
    return {
        "business_connection_id": conn,
        "chat_id": chat,
        "chat_type": "private",
        "current_profile": dict(profile, id=chat),
        "first_seen_at": first_seen_at,
    }


def _window(cs, *, since: str | None = None, until: str | None = None, tz: str | None = None, gap: str | None = None):
    args = argparse.Namespace(since=since, until=until, tz=tz, session_gap=gap)
    return cs.build_window(args, now=NOW)


def _alice_records(plugin) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    def add(record: dict[str, Any]) -> dict[str, Any]:
        records.append(record)
        return record

    add(_msg(plugin, 101, _utc(2026, 10, 3, 8, 0), "inbound", observed=_utc(2026, 10, 3, 8, 0, 5)))
    add(_msg(plugin, 102, _utc(2026, 10, 3, 8, 30), "outbound", observed=_utc(2026, 10, 3, 8, 30, 1)))
    add(_msg(plugin, 103, _utc(2026, 10, 3, 20, 0), "inbound", observed=_utc(2026, 10, 3, 20, 0, 1)))
    add(_msg(plugin, 104, _utc(2026, 10, 3, 21, 30), "unknown", observed=_utc(2026, 10, 3, 21, 30, 1)))
    add(_msg(plugin, 104, _utc(2026, 10, 3, 21, 30), "unknown", edited=True, observed=_utc(2026, 10, 3, 21, 40)))
    add(_msg(plugin, 100, _utc(2026, 10, 2, 10, 0), "inbound", edited=True, observed=_utc(2026, 10, 4, 7, 0)))
    add(_msg(plugin, 105, _utc(2026, 10, 10, 9, 0), "unknown", observed=_utc(2026, 10, 10, 9, 0, 2)))
    add(_msg(plugin, 105, _utc(2026, 10, 10, 9, 0), "outbound", edited=True, observed=_utc(2026, 10, 10, 9, 5)))
    add(_msg(plugin, 106, _utc(2026, 10, 10, 9, 10), "inbound", observed=_utc(2026, 10, 10, 9, 10, 1)))
    t106 = add(_tomb(plugin, 106, _utc(2026, 10, 10, 9, 11), "inbound", message_at=_utc(2026, 10, 10, 9, 10)))
    add(_msg(plugin, 107, _utc(2026, 10, 10, 9, 12), "inbound", observed=_utc(2026, 10, 10, 9, 12, 1)))
    t107 = add(_tomb(plugin, 107, _utc(2026, 10, 10, 9, 12, 30), "inbound", message_at=_utc(2026, 10, 10, 9, 12)))
    add(_cls(plugin, t106, "unexplained", _utc(2026, 10, 10, 9, 13), "no_strong_match"))
    add(_msg(plugin, 108, _utc(2026, 10, 10, 9, 13), "inbound", observed=_utc(2026, 10, 10, 9, 13, 1)))
    add(
        _cls(
            plugin,
            t107,
            "likely_duplicate",
            _utc(2026, 10, 10, 9, 14),
            "normalized_exact_duplicate",
            replacement=108,
        )
    )
    t108 = add(_tomb(plugin, 108, _utc(2026, 10, 10, 9, 14), "inbound", message_at=_utc(2026, 10, 10, 9, 13)))
    add(_msg(plugin, 109, _utc(2026, 10, 10, 9, 14, 30), "inbound", observed=_utc(2026, 10, 10, 9, 14, 31)))
    add(
        _cls(
            plugin,
            t108,
            "likely_correction",
            _utc(2026, 10, 10, 9, 16),
            "high_similarity_small_edit",
            replacement=109,
            score=0.93,
        )
    )
    t200 = add(_tomb(plugin, 200, _utc(2026, 10, 12, 10, 0), "unknown"))
    add(_cls(plugin, t200, "unclassifiable", _utc(2026, 10, 12, 10, 2), "missing_original"))
    add(_msg(plugin, 110, _utc(2026, 10, 17, 16, 30), "outbound", observed=_utc(2026, 10, 17, 16, 30, 1)))
    add(_msg(plugin, 111, _utc(2026, 10, 17, 18, 4), "inbound", observed=_utc(2026, 10, 17, 18, 4, 2)))
    add(_tomb(plugin, 102, _utc(2026, 10, 17, 19, 0), "outbound", message_at=_utc(2026, 10, 3, 8, 30)))
    add(_msg(plugin, 112, _utc(2026, 10, 18, 21, 0), "inbound", observed=_utc(2026, 10, 18, 21, 0, 2)))
    return records


def _overview_extra_records(plugin) -> list[dict[str, Any]]:
    carol_sent = _utc(2026, 9, 20, 10, 0)
    five = timedelta(seconds=5)
    return [
        _msg(
            plugin,
            601,
            _utc(2026, 10, 18, 6, 0),
            "inbound",
            observed=_utc(2026, 10, 18, 6, 0, 5),
            chat=992,
            profile=BOB,
        ),
        _msg(
            plugin,
            602,
            _utc(2026, 10, 18, 6, 15),
            "outbound",
            observed=_utc(2026, 10, 18, 6, 15, 5),
            chat=992,
            profile=BOB,
        ),
        _msg(
            plugin, 501, carol_sent, "inbound", observed=carol_sent + five, conn="business-456", chat=994, profile=CAROL
        ),
        _tomb(plugin, 501, _utc(2026, 10, 5, 10, 0), "inbound", message_at=carol_sent, conn="business-456", chat=994),
        _msg(
            plugin, 701, _utc(2026, 9, 10, 8, 0), "inbound", observed=_utc(2026, 9, 10, 8, 0, 5), chat=995, profile=DAVE
        ),
    ]


def _alice_fixture(plugin) -> None:
    _seed(plugin, *_alice_records(plugin))


def _overview_fixture(plugin) -> None:
    _seed(plugin, *_alice_records(plugin), *_overview_extra_records(plugin))


def _keys_everywhere(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _keys_everywhere(item)}
    if isinstance(value, list):
        return {key for item in value for key in _keys_everywhere(item)}
    return set()


def _doc_section(text: str, heading: str) -> str:
    marker = f"\n## {heading}\n"
    assert marker in text, f"missing section: ## {heading}"
    return re.split(r"^## ", text.split(marker, 1)[1], maxsplit=1, flags=re.MULTILINE)[0]


def _fenced_blocks(text: str) -> list[tuple[str, str]]:
    return [(match[1], match[2]) for match in _FENCED_BLOCK_RE.finditer(text)]


def _documented_stats_commands(text: str) -> list[list[str]]:
    """Every documented `hermes telesec stats ...` line, as CLI argv after the program name."""
    commands = []
    for language, body in _fenced_blocks(text):
        if language != "bash":
            continue
        for line in body.splitlines():
            argv = shlex.split(line, comments=True)
            if argv[:3] == ["hermes", "telesec", "stats"]:
                commands.append(argv[2:])
    return commands


def _assert_json_subset(documented: Any, actual: Any, path: str = "$") -> None:
    """A trimmed doc example may drop keys but must keep real keys, real order, and real values."""
    if isinstance(documented, dict):
        assert isinstance(actual, dict), path
        assert list(documented) == [key for key in actual if key in documented], f"{path}: unknown or reordered keys"
        for key, value in documented.items():
            _assert_json_subset(value, actual[key], f"{path}.{key}")
    elif isinstance(documented, list):
        assert isinstance(actual, list) and len(documented) == len(actual), path
        for index, (item, actual_item) in enumerate(zip(documented, actual, strict=True)):
            _assert_json_subset(item, actual_item, f"{path}[{index}]")
    else:
        assert documented == actual, path


# --- Golden output and docs ------------------------------------------------------


def test_worked_example_json(plugin):
    _alice_fixture(plugin)

    document = _stats_json(plugin, "--contact", "@alice", *MOSCOW_RANGE)

    assert document["schema_version"] == 2
    assert document["report"] == "correspondence_stats"
    assert document["mode"] == "contact"
    assert document["generated_at"] == "2026-10-19T14:32:00+03:00"
    assert document["range"] == {
        "since": "2026-10-01T00:00:00+03:00",
        "until": "2026-10-18T23:59:59.999999+03:00",
        "timezone": "Europe/Moscow",
        "time_basis": "telegram_send_time",
        "deletion_time_basis": "deletion_observed_time",
        "content": "text_messages_only",
        "session_gap_seconds": 21600,
    }
    assert document["overview"] is None
    [alice] = document["contacts"]
    assert alice["business_connection_id"] == "business-123"
    assert alice["chat_id"] == 991
    assert alice["chat_type"] == "private"
    assert alice["display_name"] == "Alice Example (@alice)"
    assert alice["name"] == "Alice Example"
    assert alice["username"] == "alice"
    assert alice["messages"] == {"total": 9, "received": 5, "sent": 3, "unknown_direction": 1}
    assert alice["active_days"] == {"total": 4, "received": 3, "sent": 3}
    assert alice["latest"] == {
        "at": "2026-10-17T21:04:00+03:00",
        "side": "received",
        "received_at": "2026-10-17T21:04:00+03:00",
        "sent_at": "2026-10-17T19:30:00+03:00",
        "observed_reply": "none",
    }
    assert alice["sessions"] == {
        "total": 4,
        "started_by_contact": 2,
        "started_by_owner": 2,
        "started_by_unknown": 0,
        "ended_on_received": 2,
    }
    assert alice["deletions"] == {
        "total": 4,
        "unexplained": {"total": 1, "received": 1, "sent": 0, "unknown_direction": 0},
        "likely_correction": 1,
        "likely_duplicate": 1,
        "unclassifiable": 0,
        "pending": 1,
        "details_command": (
            "hermes telesec history deletions --connection business-123 --chat 991 --status unexplained"
        ),
    }
    assert alice["missing_history"] == {
        "retained_from": "2026-10-03T11:00:05+03:00",
        "range_partial": True,
        "deletions_of_messages_not_in_history": 1,
        "messages_known_only_from_edit": 1,
    }
    # 5 of 8 known-side messages is 62.5%, rounded half up.
    assert alice["share"] == {"received": 63, "sent": 37}
    # Turns: 101 | 102 | 103 | (104 unknown) 105 | 106 109 | 110 | 111; 5 / 4 = 1.25 rounds to 1.3.
    assert alice["turns"] == {
        "total": 7,
        "received": 4,
        "sent": 3,
        "messages_per_turn": {"received": 1.3, "sent": 1.0},
    }
    # 103 comes 11.5h after 102 (a new conversation) and 105 follows an unknown message,
    # so the replies are 102 (30m), 106 (10m), and 111 (1h34m).
    assert alice["replies"] == {
        "received": {"count": 2, "median_seconds": 3120, "p90_seconds": 5640},
        "sent": {"count": 1, "median_seconds": 1800, "p90_seconds": 1800},
    }
    assert alice["waiting"] is None  # --until is set
    assert alice["text_length_median"] == {"received": 10, "sent": 10}
    assert alice["edited"] == {"total": 2, "received": 0, "sent": 1}
    assert alice["hours"] == [1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2, 3, 0, 0, 0, 0, 0, 0, 1, 0, 1, 0, 1]
    assert alice["weekdays"] == [0, 0, 0, 0, 0, 8, 1]
    assert (alice["peak_hour"], alice["peak_weekday"]) == (12, 5)
    assert alice["activity"] == {
        "bucket": "day",
        "start": "2026-10-01",
        "counts": [0, 0, 3, 1, 0, 0, 0, 0, 0, 3, 0, 0, 0, 0, 0, 0, 2, 0],
    }
    assert alice["streaks"] == {
        "current_days": 1,
        "longest_days": 2,
        "longest_start": "2026-10-03",
        "longest_end": "2026-10-04",
    }
    assert alice["silence"] == {
        "seconds": 7 * 86400 + 7 * 3600 + 15 * 60 + 30,
        "from": "2026-10-10T12:14:30+03:00",
        "to": "2026-10-17T19:30:00+03:00",
    }
    # Retained history starts 2026-10-03, after the previous 18 days would start.
    assert alice["previous"] is None


def test_docs_examples_match_output(plugin):
    _overview_fixture(plugin)
    blocks = _fenced_blocks(_doc_section(STATS_DOC.read_text(encoding="utf-8"), "Example (synthetic)"))

    # Each documented command is followed by its output: text verbatim, JSON as a trimmed subset.
    checked = []
    for (language, command), (output_language, documented) in zip(blocks[::2], blocks[1::2], strict=True):
        assert language == "bash"
        argv = shlex.split(command)
        assert argv[:3] == ["hermes", "telesec", "stats"]
        exit_code, output = _run_history_cli(plugin, *argv[2:])
        assert exit_code == 0, output
        if output_language == "text":
            assert documented == output
        else:
            assert output_language == "json"
            _assert_json_subset(json.loads(documented), json.loads(output))
        checked.append((tuple(argv[3:]), output_language))

    assert checked == [
        (("--contact", "@alice", *MOSCOW_RANGE), "text"),
        (("--contact", "@alice", "--since", "7d", "--tz", "Europe/Moscow"), "text"),
        (MOSCOW_RANGE, "text"),
        (("--contact", "@alice", *MOSCOW_RANGE, "--format", "json"), "json"),
    ]
    assert [body for _, body in blocks[1:6:2]] == [ALICE_MOSCOW, ALICE_WEEK_MOSCOW, OVERVIEW_MOSCOW]


# --- Counting definitions --------------------------------------------------------


def test_message_counted_once(plugin):
    at = _utc(2026, 10, 10, 9, 0)
    created = _msg(plugin, 1, at, "inbound")
    redelivered = _msg(plugin, 1, at, "inbound", observed=at + timedelta(seconds=30))
    _seed(
        plugin,
        created,
        _msg(plugin, 1, at, "inbound", edited=True, observed=at + timedelta(minutes=1)),
        _msg(plugin, 1, at, "inbound", edited=True, observed=at + timedelta(minutes=2)),
        dict(created),
        redelivered,
    )

    contact = _contact_json(plugin, "--contact", "@alice")

    assert redelivered["event_id"] != created["event_id"]
    assert contact["messages"] == {"total": 1, "received": 1, "sent": 0, "unknown_direction": 0}
    assert contact["sessions"]["total"] == 1


def test_telesec_cleaning_its_own_transcript_reply_is_no_sent_edit(plugin, cs):
    at = _utc(2026, 10, 10, 9, 0)
    later = at + timedelta(seconds=40)
    transcript, transcript_edit = _msg(plugin, 1, at, "outbound"), _msg(plugin, 1, at, "outbound", edited=True,
                                                                         observed=later)
    transcript["text"], transcript_edit["text"] = "🎙️ raw words", "🎙️ Clean words."
    transcript_edit["edit_origin"] = "transcript_cleanup"
    cleanup = _msg(plugin, 2, at, "outbound", edited=True, observed=at + timedelta(seconds=1))
    manual = _msg(plugin, 2, at, "outbound", edited=True, observed=at + timedelta(seconds=2))
    manual["text"] = "🎙️ Owner's revised words"
    confirmation = {**cleanup, "event_type": "transcript.cleanup_confirmed", "edited_event_id": cleanup["event_id"]}
    records = [transcript, transcript_edit, _msg(plugin, 2, at, "outbound"), cleanup, manual, confirmation]

    stats = cs.compute_stats(_entry(), records, _window(cs))

    assert (stats.edited.total, stats.edited.sent) == (1, 1)


def test_send_time_placement(plugin):
    _seed(
        plugin,
        _msg(plugin, 1, _utc(2026, 10, 10, 9, 0), "inbound", observed=_utc(2026, 10, 12, 9, 0)),
        _msg(plugin, 2, _utc(2026, 9, 29, 9, 0), "inbound", observed=_utc(2026, 10, 2, 9, 0)),
        _msg(plugin, 3, None, "outbound", observed=_utc(2026, 10, 5, 9, 0), message_at=None),
        _msg(plugin, 3, None, "outbound", edited=True, observed=_utc(2026, 10, 15, 9, 0), message_at=None),
    )

    contact = _contact_json(plugin, "--contact", "@alice", "--since", "2026-10-01", "--until", "2026-10-11")

    assert contact["messages"] == {"total": 2, "received": 1, "sent": 1, "unknown_direction": 0}
    assert contact["latest"]["at"] == "2026-10-10T09:00:00+00:00"
    assert contact["latest"]["sent_at"] == "2026-10-05T09:00:00+00:00"


def test_foreign_records_ignored(plugin, cs):
    at = _utc(2026, 10, 10, 9, 0)
    records = [
        _msg(plugin, 1, at, "inbound"),
        _msg(plugin, 2, at, "inbound", chat=992),
        _msg(plugin, 3, at, "inbound", conn="business-456"),
        _tomb(plugin, 1, at + timedelta(minutes=1), "inbound", message_at=at, chat=992),
    ]

    stats = cs.compute_stats(_entry(), records, _window(cs))

    assert stats.messages.total == 1
    assert stats.deletions.total == 0


# --- Deletions -------------------------------------------------------------------


def test_deleted_still_counts_unless_replaced(plugin):
    base = _utc(2026, 10, 10, 9, 0)

    def at(minutes: int) -> datetime:
        return base + timedelta(minutes=minutes)

    records = [
        _msg(plugin, 1, at(0), "inbound"),
        _msg(plugin, 2, at(1), "outbound"),
        _msg(plugin, 3, at(2), "inbound"),
        _msg(plugin, 4, at(3), "inbound"),
        _msg(plugin, 5, at(4), "outbound"),
        _msg(plugin, 6, at(5), "inbound"),
        _msg(plugin, 7, at(6), "outbound"),
    ]
    tombs = {
        mid: _tomb(plugin, mid, at(10 + mid), direction, message_at=at(mid - 1))
        for mid, direction in (
            (1, "inbound"),
            (2, "outbound"),
            (3, "inbound"),
            (4, "inbound"),
            (5, "outbound"),
        )
    }
    records.extend(tombs.values())
    records.extend(
        [
            _cls(plugin, tombs[1], "unexplained", at(30), "no_strong_match"),
            _cls(plugin, tombs[2], "unclassifiable", at(30), "missing_text"),
            _cls(plugin, tombs[4], "likely_duplicate", at(30), "normalized_exact_duplicate", replacement=6),
            _cls(
                plugin, tombs[5], "likely_correction", at(30), "high_similarity_small_edit", replacement=7, score=0.95
            ),
        ]
    )
    _seed(plugin, *records)

    exit_code, output = _stats(plugin, "--contact", "@alice")

    assert exit_code == 0
    lines = output.splitlines()
    assert "  messages 5: received 3, sent 2, unknown direction 0" in lines
    assert (
        "  deletions 5: unexplained 1 (received 1, sent 0), likely correction 1, likely duplicate 1, "
        "unclassifiable 1, pending 1"
    ) in lines


def test_latest_classification_in_file_order_wins(plugin):
    # history_support's own state rebuild keeps the last classification in file
    # (append) order; stats must agree even when observed_at stepped backwards.
    at = _utc(2026, 10, 10, 9, 0)
    tomb_one = _tomb(plugin, 1, at + timedelta(minutes=1), "inbound", message_at=at)
    tomb_two = _tomb(plugin, 2, at + timedelta(minutes=1), "inbound", message_at=at + timedelta(seconds=1))
    late = _cls(plugin, tomb_two, "unexplained", at + timedelta(minutes=10), "no_strong_match")
    early = _cls(plugin, tomb_two, "likely_duplicate", at + timedelta(minutes=5), "normalized_exact_duplicate")
    tomb_three = _tomb(plugin, 3, at + timedelta(minutes=2), "outbound", message_at=at + timedelta(seconds=2))
    _seed(
        plugin,
        _msg(plugin, 1, at, "inbound"),
        _cls(plugin, tomb_one, "unexplained", at + timedelta(minutes=3), "no_strong_match"),
        tomb_one,
        _msg(plugin, 2, at + timedelta(seconds=1), "inbound"),
        tomb_two,
        late,
        early,
        _msg(plugin, 3, at + timedelta(seconds=2), "outbound"),
        tomb_three,
        dict(tomb_three),
    )

    contact = _contact_json(plugin, "--contact", "@alice")

    # Message 2's last classification in file order is likely_duplicate, so it is not counted.
    assert contact["messages"]["total"] == 2
    assert contact["deletions"]["total"] == 3
    assert contact["deletions"]["unexplained"] == {"total": 1, "received": 1, "sent": 0, "unknown_direction": 0}
    assert contact["deletions"]["likely_duplicate"] == 1
    assert contact["deletions"]["pending"] == 1


def test_latest_known_direction_follows_file_order(plugin, cs):
    # The capture clock stepped back: the edit was appended last but observed earlier.
    at = _utc(2026, 10, 10, 9, 0)
    records = [
        _msg(plugin, 1, at, "inbound", observed=at + timedelta(minutes=5)),
        _msg(plugin, 1, at, "outbound", edited=True, observed=at + timedelta(minutes=1)),
        _msg(plugin, 1, at, "unknown", edited=True, observed=at + timedelta(minutes=9)),
    ]

    counts = cs.compute_stats(_entry(), records, _window(cs)).messages

    assert (counts.received, counts.sent, counts.unknown) == (0, 1, 0)


_CLASSIFICATION_REASONS = {
    "likely_duplicate": "normalized_exact_duplicate",
    "likely_correction": "high_similarity_small_edit",
    "unexplained": "no_strong_match",
    "unclassifiable": "missing_text",
}
_DETAILS_991 = "hermes telesec history deletions --connection business-123 --chat 991 --status unexplained"


# Every adjacent pair of likely_duplicate > likely_correction > unexplained >
# unclassifiable > pending, with the winner on the first and on the second tombstone.
@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("likely_duplicate", "likely_correction", "likely_duplicate"),
        ("unexplained", "likely_correction", "likely_correction"),
        ("unexplained", "unclassifiable", "unexplained"),
        ("pending", "unclassifiable", "unclassifiable"),
    ],
)
def test_deleted_twice_precedence(plugin, first: str, second: str, expected: str):
    sent = _utc(2026, 10, 10, 9, 0)
    first_tomb = _tomb(plugin, 1, sent + timedelta(minutes=1), "inbound", message_at=sent)
    second_tomb = _tomb(plugin, 1, sent + timedelta(minutes=2), "inbound", message_at=sent)
    records = [_msg(plugin, 1, sent, "inbound"), first_tomb, second_tomb]
    for tomb, status in ((first_tomb, first), (second_tomb, second)):
        if status != "pending":
            records.append(_cls(plugin, tomb, status, sent + timedelta(minutes=5), _CLASSIFICATION_REASONS[status]))
    _seed(plugin, *records)

    contact = _contact_json(plugin, "--contact", "@alice")

    replaced = expected in {"likely_duplicate", "likely_correction"}
    assert contact["messages"]["total"] == (0 if replaced else 1)
    unexplained = int(expected == "unexplained")
    assert contact["deletions"] == {
        "total": 1,
        "unexplained": {"total": unexplained, "received": unexplained, "sent": 0, "unknown_direction": 0},
        "likely_correction": int(expected == "likely_correction"),
        "likely_duplicate": int(expected == "likely_duplicate"),
        "unclassifiable": int(expected == "unclassifiable"),
        "pending": int(expected == "pending"),
        "details_command": _DETAILS_991 if unexplained else None,
    }


def test_deleted_twice_first_observation_and_latest_known_side(plugin):
    # Message 1: first deleted before --since, redelivered inside the range -> not on the line.
    early_sent = _utc(2026, 10, 1, 9, 0)
    early_first = _tomb(plugin, 1, _utc(2026, 10, 2, 9, 0), "inbound", message_at=early_sent)
    early_again = _tomb(plugin, 1, _utc(2026, 10, 12, 9, 0), "inbound", message_at=early_sent)
    # Message 2: first deleted inside the range, redelivered after --until -> on the line.
    in_sent = _utc(2026, 10, 11, 9, 0)
    in_first = _tomb(plugin, 2, _utc(2026, 10, 12, 9, 0), "inbound", message_at=in_sent)
    in_again = _tomb(plugin, 2, _utc(2026, 10, 16, 9, 0), "inbound", message_at=in_sent)
    # Message 3: unknown created direction; the latest tombstone with a known side decides.
    side_sent = _utc(2026, 10, 11, 10, 0)
    side_out = _tomb(plugin, 3, _utc(2026, 10, 12, 9, 0), "outbound", message_at=side_sent)
    side_in = _tomb(plugin, 3, _utc(2026, 10, 13, 9, 0), "inbound", message_at=side_sent)
    side_unknown = _tomb(plugin, 3, _utc(2026, 10, 13, 10, 0), "unknown", message_at=side_sent)
    _seed(
        plugin,
        _msg(plugin, 1, early_sent, "inbound"),
        early_first,
        early_again,
        _msg(plugin, 2, in_sent, "inbound"),
        in_first,
        _msg(plugin, 3, side_sent, "unknown"),
        side_out,
        side_in,
        side_unknown,
        _cls(plugin, side_in, "unexplained", _utc(2026, 10, 13, 9, 5), "no_strong_match"),
        in_again,
        _cls(plugin, in_again, "unexplained", _utc(2026, 10, 16, 9, 5), "no_strong_match"),
    )
    window = ("--contact", "@alice", "--since", "2026-10-10", "--until", "2026-10-14")

    contact = _contact_json(plugin, *window)

    assert contact["messages"] == {"total": 2, "received": 1, "sent": 0, "unknown_direction": 1}
    assert contact["deletions"]["total"] == 2
    assert contact["deletions"]["pending"] == 0
    assert contact["deletions"]["unexplained"] == {"total": 2, "received": 2, "sent": 0, "unknown_direction": 0}
    exit_code, output = _stats(plugin, *window)
    assert exit_code == 0
    assert "  deletions 2: unexplained 2 (received 2, sent 0)" in output.splitlines()


def test_unexplained_split_by_side(plugin):
    at = _utc(2026, 10, 10, 9, 0)
    out_tomb = _tomb(plugin, 2, at + timedelta(minutes=10), "outbound", message_at=at)
    # The tombstone copied the unknown direction of the latest edit.
    edited_tomb = _tomb(plugin, 3, at + timedelta(minutes=10), "unknown", message_at=at)
    unknown_tomb = _tomb(plugin, 4, at + timedelta(minutes=10), "unknown", message_at=at)
    # The original was pruned, but the tombstone still carries its send time and side.
    pruned_tomb = _tomb(plugin, 5, at + timedelta(minutes=10), "inbound", message_at=at - timedelta(days=60))
    _seed(
        plugin,
        _msg(plugin, 2, at, "outbound"),
        _msg(plugin, 3, at, "outbound"),
        _msg(plugin, 3, at, "unknown", edited=True, observed=at + timedelta(minutes=1)),
        _msg(plugin, 4, at, "unknown"),
        out_tomb,
        edited_tomb,
        unknown_tomb,
        pruned_tomb,
        *(
            _cls(plugin, tomb, "unexplained", at + timedelta(minutes=20), "no_strong_match")
            for tomb in (out_tomb, edited_tomb, unknown_tomb, pruned_tomb)
        ),
    )

    exit_code, output = _stats(plugin, "--contact", "@alice")

    assert exit_code == 0
    assert "  deletions 4: unexplained 4 (received 1, sent 2, unknown direction 1)" in output.splitlines()


# --- Missing history -------------------------------------------------------------


def test_partial_range(plugin):
    _seed(plugin, _msg(plugin, 1, _utc(2026, 10, 3, 9, 0), "inbound"))

    # first_seen_at is the first observation, 2026-10-03T09:00:01Z: --since exactly
    # there covers all retained history, one second earlier does not.
    at_first = _contact_json(plugin, "--contact", "@alice", "--since", "2026-10-03T09:00:01Z")
    assert at_first["missing_history"]["range_partial"] is False
    before_first = _contact_json(plugin, "--contact", "@alice", "--since", "2026-10-03T09:00:00Z")
    assert before_first["missing_history"]["range_partial"] is True


def test_overview_range_before_retained_history(plugin):
    _seed(
        plugin,
        # Alice is retained from 2026-10-03, Bob from 2026-10-12, Dave from 2026-08-20.
        _msg(plugin, 1, _utc(2026, 10, 3, 9, 0), "inbound"),
        _msg(plugin, 601, _utc(2026, 10, 12, 9, 0), "outbound", chat=992, profile=BOB),
        # Bob edited a message sent in September, before his history was captured.
        _msg(
            plugin,
            600,
            _utc(2026, 9, 15, 9, 0),
            "inbound",
            edited=True,
            observed=_utc(2026, 10, 12, 10, 0),
            chat=992,
            profile=BOB,
        ),
        _msg(plugin, 701, _utc(2026, 8, 20, 9, 0), "inbound", chat=995, profile=DAVE),
    )
    september = ("--since", "2026-09-01", "--until", "2026-09-30")

    # The whole range is before Alice's and Bob's retained history: their activity there
    # is unknown, not zero, and the overview says so instead of only "no activity".
    exit_code, output = _stats(plugin, *september)
    assert exit_code == 0
    assert output.splitlines() == [
        "range 2026-09-01..2026-09-30 UTC, by Telegram send time; text messages only",
        "No contacts with activity in range.",
        "shown 0 of 3 contacts; not shown: 3 with no activity in range "
        + "(range is partial for 2: retained history starts after --since)",
        "missing history in contacts with no activity in range: 1 message known only from an edit; "
        + "use --contact for details",
    ]
    contacts_table = _doc_section(STATS_DOC.read_text(encoding="utf-8"), "Contacts table")
    assert [body for language, body in _fenced_blocks(contacts_table) if language == "text"] == [output]
    document = _stats_json(plugin, *september)
    assert document["contacts"] == []
    assert document["overview"] == {
        "contacts_in_scope": 3,
        "active": 0,
        "shown": 0,
        "limit": 20,
        "not_shown_over_limit": 0,
        "not_shown_no_activity": 3,
        "not_shown_no_activity_partial": 2,
        "not_shown_no_activity_missing_history": {
            "deletions_of_messages_not_in_history": 0,
            "messages_known_only_from_edit": 1,
        },
    }
    # The contact report agrees, and missing history is never mixed into the counts.
    bob = _contact_json(plugin, "--contact", "@bob", *september)
    assert bob["messages"]["total"] == 0
    assert bob["missing_history"]["range_partial"] is True
    assert bob["missing_history"]["messages_known_only_from_edit"] == 1

    # A shown row keeps its own partial column; the footer covers only hidden contacts.
    exit_code, output = _stats(plugin, "--since", "2026-10-01", "--until", "2026-10-05")
    assert exit_code == 0
    lines = output.splitlines()
    assert lines[2].split()[-6:] == ["yes", "business-123", "991", "Alice", "Example", "(@alice)"]
    assert lines[3:] == [
        "shown 1 of 3 contacts; not shown: 2 with no activity in range "
        + "(range is partial for 1: retained history starts after --since)",
    ]

    # Without --since the range covers all retained history, so nothing is partial.
    exit_code, output = _stats(plugin, "--until", "2026-08-31")
    assert exit_code == 0
    assert output.splitlines()[-1] == "shown 1 of 3 contacts; not shown: 2 with no activity in range"


def test_real_prune(plugin, monkeypatch: pytest.MonkeyPatch):
    history = plugin._history_support
    july = _utc(2026, 7, 10, 9, 0)
    august = _utc(2026, 8, 10, 9, 0)
    _seed(
        plugin,
        _msg(plugin, 1, july, "inbound"),
        _msg(plugin, 2, august, "outbound"),
        _msg(plugin, 3, _utc(2026, 10, 5, 9, 0), "inbound", observed=_utc(2026, 10, 5, 9, 0, 3)),
        _msg(plugin, 1, july, "inbound", edited=True, observed=_utc(2026, 10, 6, 9, 0)),
        _tomb(plugin, 2, _utc(2026, 10, 7, 9, 0), "outbound", message_at=august),
        _tomb(plugin, 99, _utc(2026, 10, 8, 9, 0), "unknown"),
    )
    th.set_retention(plugin, 30)

    result = history.maintain_history(now=NOW)

    assert result.pruned_files == 2
    contact = _contact_json(plugin, "--contact", "@alice")
    assert contact["messages"] == {"total": 1, "received": 1, "sent": 0, "unknown_direction": 0}
    assert contact["missing_history"]["retained_from"] == "2026-10-05T09:00:03+00:00"
    assert contact["missing_history"]["range_partial"] is False
    assert contact["missing_history"]["messages_known_only_from_edit"] == 1
    assert contact["missing_history"]["deletions_of_messages_not_in_history"] == 1
    assert contact["deletions"]["total"] == 1
    assert contact["deletions"]["unexplained"] == {"total": 1, "received": 0, "sent": 1, "unknown_direction": 0}


def test_orphan_classification_ignored(plugin):
    sent = _utc(2026, 10, 10, 9, 0)
    absent_tomb = _tomb(plugin, 1, sent + timedelta(minutes=1), "inbound", message_at=sent)
    _seed(
        plugin,
        _msg(plugin, 1, sent, "inbound"),
        _cls(plugin, absent_tomb, "likely_duplicate", sent + timedelta(minutes=5), "normalized_exact_duplicate"),
    )

    contact = _contact_json(plugin, "--contact", "@alice")

    assert contact["messages"]["total"] == 1
    assert contact["deletions"]["total"] == 0
    assert contact["missing_history"]["deletions_of_messages_not_in_history"] == 0


# --- Range and time zone ---------------------------------------------------------


def test_moscow_plain_dates_inclusive(plugin):
    _seed(
        plugin,
        _msg(plugin, 1, _utc(2026, 9, 30, 20, 59, 59), "inbound"),
        _msg(plugin, 2, _utc(2026, 9, 30, 21, 0, 0), "inbound"),
        _msg(plugin, 3, _utc(2026, 10, 4, 21, 0, 0), "inbound"),
        _msg(plugin, 4, _utc(2026, 10, 5, 20, 59, 59), "inbound"),
        _msg(plugin, 5, _utc(2026, 10, 18, 20, 59, 59), "inbound"),
        _msg(plugin, 6, _utc(2026, 10, 18, 21, 0, 0), "inbound"),
    )

    assert _contact_json(plugin, "--contact", "@alice", *MOSCOW_RANGE)["messages"]["total"] == 4

    one_day = _contact_json(
        plugin, "--contact", "@alice", "--since", "2026-10-05", "--until", "2026-10-05", "--tz", "Europe/Moscow"
    )
    assert one_day["messages"]["total"] == 2


def test_exact_timestamp_bounds_inclusive(plugin):
    since = _utc(2026, 10, 5, 9, 0)
    until = _utc(2026, 10, 18, 18, 30)
    second = timedelta(seconds=1)
    early = _utc(2026, 10, 2, 9, 0)
    records = [
        _msg(plugin, 1, since, "inbound"),
        _msg(plugin, 2, since - second, "inbound"),
        _msg(plugin, 3, until, "outbound", observed=until + timedelta(minutes=5)),
        _msg(plugin, 4, until + second, "outbound"),
    ]
    # Deletions of messages sent before the range fall into it by observation time only.
    for mid, observed in ((10, since), (11, since - second), (12, until), (13, until + second)):
        sent = early + timedelta(minutes=mid)
        records += [_msg(plugin, mid, sent, "inbound"), _tomb(plugin, mid, observed, "inbound", message_at=sent)]
    _seed(plugin, *records)
    window = ("--contact", "@alice", "--since", "2026-10-05T09:00:00Z", "--until", "2026-10-18T18:30Z")

    contact = _contact_json(plugin, *window)

    assert contact["messages"] == {"total": 2, "received": 1, "sent": 1, "unknown_direction": 0}
    assert contact["latest"]["at"] == "2026-10-18T18:30:00+00:00"
    assert contact["deletions"]["total"] == 2
    assert contact["deletions"]["pending"] == 2
    exit_code, output = _stats(plugin, *window)
    assert exit_code == 0
    assert output.splitlines()[0] == (
        "range 2026-10-05 09:00..2026-10-18 18:30 UTC, by Telegram send time; text messages only"
    )
    assert "  messages 2: received 1, sent 1, unknown direction 0" in output.splitlines()
    assert "  deletions 2: pending 2" in output.splitlines()


def test_dst_days(plugin, cs):
    try:
        berlin = ZoneInfo("Europe/Berlin")
        santiago = ZoneInfo("America/Santiago")
    except ZoneInfoNotFoundError:
        pytest.skip("tz database unavailable")

    def bounds(day: str, tz) -> tuple[datetime, datetime]:
        return (
            cs.parse_bound(day, option_name="--since", tz=tz, now=NOW, end_of_day=False),
            cs.parse_bound(day, option_name="--until", tz=tz, now=NOW, end_of_day=True),
        )

    assert bounds("2026-10-25", berlin) == (_utc(2026, 10, 24, 22, 0), datetime(2026, 10, 25, 22, 59, 59, 999999, UTC))
    assert bounds("2026-03-29", berlin) == (_utc(2026, 3, 28, 23, 0), datetime(2026, 3, 29, 21, 59, 59, 999999, UTC))
    assert bounds("2026-09-06", santiago)[0] == _utc(2026, 9, 6, 4, 0)

    _seed(plugin, _msg(plugin, 1, _utc(2026, 9, 6, 12, 0), "inbound"))
    exit_code, output = _stats(
        plugin, "--contact", "@alice", "--since", "2026-09-06", "--until", "2026-09-06", "--tz", "America/Santiago"
    )
    assert exit_code == 0
    assert output.splitlines()[0].startswith("range 2026-09-06..2026-09-06 America/Santiago,")
    assert "  messages 1: received 1, sent 0, unknown direction 0" in output.splitlines()


def test_naive_and_offset_timestamps(cs):
    moscow = ZoneInfo("Europe/Moscow")

    def since(value: str) -> datetime:
        return cs.parse_bound(value, option_name="--since", tz=moscow, now=NOW, end_of_day=False)

    assert since("2026-10-01T09:00") == _utc(2026, 10, 1, 6, 0)
    assert since("2026-10-01 09:00") == _utc(2026, 10, 1, 6, 0)
    assert since("2026-10-01T09:00Z") == _utc(2026, 10, 1, 9, 0)
    assert since("2026-10-01T09:00+00:00") == _utc(2026, 10, 1, 9, 0)
    assert since("20261001") == since("2026-W40-4") == since("2026-10-01") == _utc(2026, 9, 30, 21, 0)
    assert cs.parse_bound("20261001", option_name="--until", tz=moscow, now=NOW, end_of_day=True) == datetime(
        2026, 10, 1, 20, 59, 59, 999999, UTC
    )


def test_relative_bounds(plugin):
    _seed(plugin, _msg(plugin, 1, _utc(2026, 10, 18, 9, 0), "inbound"))

    exit_code, output = _stats(plugin, "--contact", "@alice", "--since", "2d", "--until", "0d", "--tz", "Europe/Moscow")
    assert exit_code == 0
    assert output.splitlines()[0] == (
        "range 2026-10-17 14:32..2026-10-19 14:32 Europe/Moscow, by Telegram send time; text messages only"
    )

    exit_code, output = _stats(plugin, "--contact", "@alice", "--since", "2d", "--tz", "Europe/Moscow")
    assert exit_code == 0
    assert output.splitlines()[0].startswith("range 2026-10-17 14:32..now Europe/Moscow,")

    exit_code, output = _stats(plugin, "--contact", "@alice")
    assert exit_code == 0
    assert output.splitlines()[0] == "range start of history..now UTC, by Telegram send time; text messages only"


def test_hermes_tz_applies_to_plain_and_naive_bounds(plugin, monkeypatch: pytest.MonkeyPatch):
    _seed(
        plugin,
        # 2026-10-01 00:30 in Moscow, still 2026-09-30 in UTC.
        _msg(plugin, 1, _utc(2026, 9, 30, 21, 30), "inbound"),
        _msg(plugin, 2, _utc(2026, 10, 5, 9, 0), "outbound"),
        # 2026-10-18 in UTC, already 2026-10-19 01:00 in Moscow.
        _msg(plugin, 3, _utc(2026, 10, 18, 22, 0), "inbound"),
    )
    _hermes_tz(monkeypatch, "Europe/Moscow")
    plain = ("--contact", "@alice", "--since", "2026-10-01", "--until", "2026-10-18")

    exit_code, output = _stats(plugin, *plain)
    assert exit_code == 0
    lines = output.splitlines()
    assert lines[0] == "range 2026-10-01..2026-10-18 Europe/Moscow, by Telegram send time; text messages only"
    assert "  messages 2: received 1, sent 1, unknown direction 0" in lines
    assert "  latest 2026-10-05 12:00 sent; last received 2026-10-01 00:30" in lines

    # Naive timestamps are read in the Hermes zone too.
    naive = ("--contact", "@alice", "--since", "2026-10-01T00:00", "--until", "2026-10-18T23:59:59")
    exit_code, output = _stats(plugin, *naive)
    assert exit_code == 0
    lines = output.splitlines()
    assert lines[0] == "range 2026-10-01..2026-10-18 23:59 Europe/Moscow, by Telegram send time; text messages only"
    assert "  messages 2: received 1, sent 1, unknown direction 0" in lines

    # An explicit --tz overrides Hermes for the bounds, not only for the label.
    exit_code, output = _stats(plugin, *plain, "--tz", "UTC")
    assert exit_code == 0
    lines = output.splitlines()
    assert lines[0] == "range 2026-10-01..2026-10-18 UTC, by Telegram send time; text messages only"
    assert "  latest 2026-10-18 22:00 received; last sent 2026-10-05 09:00" in lines


@pytest.mark.parametrize("value", ["Mars/Base", "../etc", ""])
def test_invalid_tz(plugin, value: str):
    _seed(plugin, _msg(plugin, 1, _utc(2026, 10, 18, 9, 0), "inbound"))

    exit_code, output = _stats(plugin, "--contact", "@alice", "--tz", value)

    assert exit_code == 1
    assert output == f"error: unknown timezone {value!r} in --tz; use an IANA name such as Europe/Moscow"


def test_invalid_bounds_and_gaps(cs):
    for value in ("yesterday", "2026-10", "2026-02-30", "99999999999d"):
        with pytest.raises(ValueError, match=f"^unsupported --since value: {value}$"):
            _window(cs, since=value)
    with pytest.raises(ValueError, match="^unsupported --until value: 9999-12-31$"):
        _window(cs, until="9999-12-31")
    # A relative bound that lands on 0001-01-01T01:00Z cannot be shown west of UTC.
    with pytest.raises(ValueError, match="^unsupported --since value: 63928002720s$"):
        _window(cs, since="63928002720s", tz="America/New_York")
    with pytest.raises(ValueError, match="^--until must be greater than or equal to --since$"):
        _window(cs, since="2026-10-10", until="2026-10-01")
    hint = "; use a positive duration such as 90m, 6h, or 1d"
    for value in ("0h", "6", "1.5h", "-1h", "6H", ""):
        with pytest.raises(ValueError, match=re.escape(f"unsupported --session-gap value: {value}{hint}")):
            _window(cs, gap=value)


def test_active_days_follow_tz(plugin):
    _seed(
        plugin,
        # Same-side pairs that straddle Moscow midnight pin the per-side day counts.
        _msg(plugin, 11, _utc(2026, 10, 5, 22, 30), "inbound", chat=992, profile=BOB),
        _msg(plugin, 12, _utc(2026, 10, 6, 1, 0), "inbound", chat=992, profile=BOB),
        _msg(plugin, 13, _utc(2026, 10, 7, 22, 30), "outbound", chat=992, profile=BOB),
        _msg(plugin, 14, _utc(2026, 10, 8, 1, 0), "outbound", chat=992, profile=BOB),
    )

    assert _contact_json(plugin, "--contact", "@bob")["active_days"] == {"total": 4, "received": 2, "sent": 2}
    assert _contact_json(plugin, "--contact", "@bob", "--tz", "Europe/Moscow")["active_days"] == {
        "total": 2,
        "received": 1,
        "sent": 1,
    }


# --- Sessions --------------------------------------------------------------------


def test_session_gap_strict(plugin):
    start = _utc(2026, 10, 10, 0, 0)
    _seed(
        plugin,
        _msg(plugin, 1, start, "inbound"),
        _msg(plugin, 2, start + timedelta(hours=6), "inbound"),
        _msg(plugin, 3, start + timedelta(hours=12, seconds=1), "inbound"),
    )

    for gap, label, sessions in ((None, "6h", 2), ("90m", "90m", 3), ("1d", "1d", 1), ("1w", "7d", 1), ("150s", "150s", 3)):
        exit_code, output = _stats(plugin, "--contact", "@alice", *(["--session-gap", gap] if gap else []))
        assert exit_code == 0
        assert any(line.startswith(f"  sessions {sessions} (gap {label})") for line in output.splitlines()), output


def test_unknown_bridges_sessions(plugin):
    start = _utc(2026, 10, 10, 0, 0)
    _seed(
        plugin,
        _msg(plugin, 1, start, "inbound"),
        _msg(plugin, 3, start + timedelta(hours=10), "outbound"),
        _msg(plugin, 2, start + timedelta(hours=5), "unknown"),
    )

    assert _contact_json(plugin, "--contact", "@alice")["sessions"]["total"] == 1


def test_session_cut_at_range_start(plugin):
    since = _utc(2026, 10, 10, 0, 0)
    _seed(
        plugin,
        _msg(plugin, 1, since - timedelta(hours=1), "outbound"),
        _msg(plugin, 2, since + timedelta(hours=1), "inbound"),
    )

    cut = _contact_json(plugin, "--contact", "@alice", "--since", "2026-10-10")["sessions"]
    assert (cut["total"], cut["started_by_contact"], cut["started_by_owner"]) == (1, 1, 0)
    whole = _contact_json(plugin, "--contact", "@alice")["sessions"]
    assert (whole["total"], whole["started_by_contact"], whole["started_by_owner"]) == (1, 0, 1)


# --- Latest and last message -----------------------------------------------------


def test_reply_states(plugin):
    _seed(
        plugin,
        _msg(plugin, 1, _utc(2026, 10, 12, 9, 0), "outbound"),
        _msg(plugin, 2, _utc(2026, 10, 18, 9, 0), "inbound"),
    )

    exit_code, output = _stats(plugin, "--contact", "@alice")
    assert exit_code == 0
    assert "  last message: received 2026-10-18 09:00, no observed reply after it" in output.splitlines()
    assert _contact_json(plugin, "--contact", "@alice")["latest"]["observed_reply"] == "none"

    # An unknown-direction message after the range is not an observed reply.
    _seed(plugin, _msg(plugin, 4, _utc(2026, 10, 19, 6, 0), "unknown"))
    exit_code, output = _stats(plugin, "--contact", "@alice", "--until", "2026-10-18")
    assert exit_code == 0
    assert "  last message: received 2026-10-18 09:00, no observed reply after it" in output.splitlines()
    assert _contact_json(plugin, "--contact", "@alice", "--until", "2026-10-18")["latest"]["observed_reply"] == "none"
    # Inside the range it is the latest message, so there is no reply state at all.
    exit_code, output = _stats(plugin, "--contact", "@alice")
    assert exit_code == 0
    assert (
        "  latest 2026-10-19 06:00 unknown direction; last received 2026-10-18 09:00; last sent 2026-10-12 09:00"
        in output.splitlines()
    )
    assert "last message:" not in output
    assert _contact_json(plugin, "--contact", "@alice")["latest"]["observed_reply"] is None

    _seed(plugin, _msg(plugin, 3, _utc(2026, 10, 19, 7, 0), "outbound"))
    exit_code, output = _stats(plugin, "--contact", "@alice", "--until", "2026-10-18")
    assert exit_code == 0
    assert "  last message: received 2026-10-18 09:00, reply observed after the range" in output.splitlines()
    assert (
        _contact_json(plugin, "--contact", "@alice", "--until", "2026-10-18")["latest"]["observed_reply"]
        == "after_range"
    )

    exit_code, output = _stats(plugin, "--contact", "@alice")
    assert exit_code == 0
    assert "  latest 2026-10-19 07:00 sent; last received 2026-10-18 09:00" in output.splitlines()
    assert "last message:" not in output
    assert _contact_json(plugin, "--contact", "@alice")["latest"]["observed_reply"] is None

    exit_code, output = _stats(plugin, "--contact", "@alice", "--since", "2026-10-19T08:00Z")
    assert exit_code == 0
    lines = output.splitlines()
    assert "  latest none" in lines
    assert "  sessions 0 (gap 6h)" in lines
    assert "  messages 0: received 0, sent 0, unknown direction 0" in lines
    assert _contact_json(plugin, "--contact", "@alice", "--since", "2026-10-19T08:00Z")["latest"] == {
        "at": None,
        "side": None,
        "received_at": None,
        "sent_at": None,
        "observed_reply": None,
    }


def test_reply_tie_broken_by_message_id(plugin):
    same = _utc(2026, 10, 18, 9, 0)
    first, later = same + timedelta(seconds=1), same + timedelta(seconds=2)
    # The higher id is written and observed first, so neither file order, observation
    # order, nor string order of ids gives the expected (send time, numeric id) order.
    _seed(
        plugin,
        _msg(plugin, 10, same, "outbound", observed=first),
        _msg(plugin, 9, same, "inbound", observed=later),
        _msg(plugin, 100, same, "inbound", observed=first, chat=992, profile=BOB),
        _msg(plugin, 21, same, "outbound", observed=later, chat=992, profile=BOB),
    )

    alice = _contact_json(plugin, "--contact", "@alice")["latest"]
    assert alice["side"] == "sent"
    assert alice["observed_reply"] is None
    bob = _contact_json(plugin, "--contact", "@bob")["latest"]
    assert bob["side"] == "received"
    assert bob["observed_reply"] == "none"


def test_unknown_latest_line(plugin):
    _seed(plugin, _msg(plugin, 1, _utc(2026, 10, 18, 9, 0), "unknown"))

    exit_code, output = _stats(plugin, "--contact", "@alice")

    assert exit_code == 0
    assert "  latest 2026-10-18 09:00 unknown direction; no received message; no sent message" in output.splitlines()
    assert (
        "  sessions 1 (gap 6h): started by contact 0, by owner 0, unknown 1; ended on received 0" in output.splitlines()
    )
    # Machine-readable values stay the bare "unknown", not the text label.
    assert _contact_json(plugin, "--contact", "@alice")["latest"]["side"] == "unknown"

    exit_code, output = _stats(plugin)
    assert exit_code == 0
    header, row = output.splitlines()[1:3]
    assert header == (
        "latest            last     msgs  recv  sent  unk  days  sess  del  unexpl  partial  connection    chat  contact"
    )
    assert row == (
        "2026-10-18 09:00  unknown     1     0     0    1     1     1    0       0  no       business-123  991   "
        "Alice Example (@alice)"
    )


# --- Conversation metrics --------------------------------------------------------


def _texted(record: dict[str, Any], text: str | None) -> dict[str, Any]:
    if text is None:
        record.pop("text", None)
    else:
        record["text"] = text
    return record


def test_reply_times_session_gap_and_unknown_breaks(plugin, cs):
    start = _utc(2026, 10, 10, 9, 0)

    def at(minutes: int) -> datetime:
        return start + timedelta(minutes=minutes)

    records = [
        _msg(plugin, 1, at(0), "inbound"),
        _msg(plugin, 2, at(5), "inbound"),  # the same turn
        _msg(plugin, 3, at(15), "outbound"),  # owner reply: 10m after the turn's last message
        _msg(plugin, 4, at(375), "inbound"),  # exactly the 6h gap: still a reply
        _msg(plugin, 5, at(736), "outbound"),  # 6h01m later: a new conversation, not a reply
        _msg(plugin, 6, at(800), "unknown"),
        _msg(plugin, 7, at(801), "inbound"),  # right after an unknown message: not a reply
        _msg(plugin, 8, at(802), "unknown"),
        _msg(plugin, 9, at(803), "inbound"),  # the unknown message split the received run
        _msg(plugin, 10, at(805), "outbound"),  # owner reply: 2m
    ]

    stats = cs.compute_stats(_entry(), records, _window(cs))

    assert stats.turns == cs.Turns(received=4, sent=3, per_turn_received=1.3, per_turn_sent=1.0)
    assert stats.replies.received == cs.ReplyTimes(count=1, median_seconds=21600, p90_seconds=21600)
    assert stats.replies.sent == cs.ReplyTimes(count=2, median_seconds=360, p90_seconds=600)
    # A longer session gap turns the 6h01m gap into a reply.
    longer = cs.compute_stats(_entry(), records, _window(cs, gap="1d"))
    assert longer.replies.sent.count == 3
    assert longer.replies.received.count == 1


def test_reply_median_and_p90_nearest_rank(cs):
    assert cs._reply_times([]) == cs.ReplyTimes(count=0, median_seconds=None, p90_seconds=None)
    assert cs._reply_times([5.0]) == cs.ReplyTimes(count=1, median_seconds=5, p90_seconds=5)
    # Unsorted input; nearest rank ceil(0.9 * 3) = 3.
    assert cs._reply_times([10.0, 1.0, 7.0]) == cs.ReplyTimes(count=3, median_seconds=7, p90_seconds=10)
    # Even counts take the mean of the middle pair, rounded half up; ceil(0.9 * 10) = 9.
    assert cs._reply_times([float(n) for n in range(1, 11)]) == cs.ReplyTimes(10, 6, 9)
    assert cs._reply_times([float(n) for n in range(1, 12)]) == cs.ReplyTimes(11, 6, 10)
    assert cs._reply_times([float(n) for n in range(1, 21)]) == cs.ReplyTimes(20, 11, 18)
    assert cs._reply_times([0.4, 0.6]) == cs.ReplyTimes(2, 1, 1)


def test_share_needs_a_known_side(plugin, cs):
    unknown = _msg(plugin, 9, _utc(2026, 10, 10, 9, 0), "unknown")

    only_unknown = cs.compute_stats(_entry(), [unknown], _window(cs))
    assert only_unknown.share == cs.Share(received=None, sent=None)
    assert only_unknown.turns == cs.Turns()
    assert only_unknown.waiting is None
    _seed(plugin, unknown)
    exit_code, output = _stats(plugin, "--contact", "@alice")
    assert exit_code == 0
    assert not any(line.startswith(("  share", "  turns", "  replies", "  waiting")) for line in output.splitlines())
    contact = _contact_json(plugin, "--contact", "@alice")
    assert contact["share"] == {"received": None, "sent": None}
    assert contact["turns"]["messages_per_turn"] == {"received": None, "sent": None}


def test_waiting_only_when_open_ended(plugin, cs):
    records = [
        _msg(plugin, 1, _utc(2026, 10, 18, 9, 0), "outbound"),
        _msg(plugin, 2, _utc(2026, 10, 18, 10, 0), "inbound"),
        _msg(plugin, 3, _utc(2026, 10, 18, 10, 5), "inbound"),
        # An unknown-direction message after the last known turn does not end the wait.
        _msg(plugin, 4, _utc(2026, 10, 18, 11, 0), "unknown"),
    ]

    waiting = cs.compute_stats(_entry(), records, _window(cs)).waiting
    assert waiting == cs.Waiting(side="received", since=_utc(2026, 10, 18, 10, 0), seconds=25 * 3600 + 32 * 60)
    assert cs.compute_stats(_entry(), records, _window(cs, since="2026-10-18")).waiting == waiting
    # Any --until means the range does not run to now, even when it ends after the last message.
    for until in ("2026-10-18", "2026-10-25", "0d"):
        assert cs.compute_stats(_entry(), records, _window(cs, until=until)).waiting is None

    owner_waits = [*records, _msg(plugin, 5, _utc(2026, 10, 18, 12, 0), "outbound")]
    assert cs.compute_stats(_entry(), owner_waits, _window(cs)).waiting == cs.Waiting(
        side="sent", since=_utc(2026, 10, 18, 12, 0), seconds=23 * 3600 + 32 * 60
    )

    _seed(plugin, *records)
    assert _contact_json(plugin, "--contact", "@alice")["waiting"] == {
        "side": "received",
        "since": "2026-10-18T10:00:00+00:00",
        "seconds": 91920,
    }


def test_text_length_medians(plugin, cs):
    at = _utc(2026, 10, 10, 9, 0)
    minute = timedelta(minutes=1)
    records = [
        _texted(_msg(plugin, 1, at, "inbound"), "  qz  \n"),  # stripped: 2
        _texted(_msg(plugin, 2, at + minute, "inbound"), "привет"),  # 6 code points
        _texted(_msg(plugin, 3, at + 2 * minute, "inbound"), "\U0001f44d"),  # 1 code point
        _texted(_msg(plugin, 4, at + 3 * minute, "inbound"), None),  # no text: not measured
        _texted(_msg(plugin, 5, at + 4 * minute, "outbound"), "xy"),
        # The latest text in file order wins: the edit makes it 5.
        _texted(_msg(plugin, 5, at + 4 * minute, "outbound", edited=True, observed=at + 9 * minute), "xyzzy"),
        _texted(_msg(plugin, 6, at + 5 * minute, "outbound"), "wvut"),
        _texted(_msg(plugin, 7, at + 6 * minute, "unknown"), "SECRET-unknown side"),
    ]

    stats = cs.compute_stats(_entry(), records, _window(cs))

    # Received [1, 2, 6] has median 2; sent [4, 5] has median 4.5, rounded half up.
    assert stats.text_length == cs.TextLength(received=2, sent=5)
    assert cs.compute_stats(_entry(), records[4:6], _window(cs)).text_length == cs.TextLength(received=None, sent=5)
    _seed(plugin, *records)
    exit_code, output = _stats(plugin, "--contact", "@alice")
    assert exit_code == 0
    assert "  text length median received 2, sent 5; edited 1 (received 0, sent 1)" in output.splitlines()
    exit_code, raw = _stats(plugin, "--contact", "@alice", "--format", "json")
    assert exit_code == 0
    assert json.loads(raw)["contacts"][0]["text_length_median"] == {"received": 2, "sent": 5}
    for text in ("qz", "xyzzy", "wvut", "SECRET", "привет", "\U0001f44d", "\\u043f", "\\ud83d"):
        assert text not in output and text not in raw


def test_edited_counts(plugin, cs):
    at = _utc(2026, 10, 10, 9, 0)
    later = at + timedelta(hours=1)
    duplicate = _tomb(plugin, 6, later, "inbound", message_at=at + timedelta(minutes=6))
    records = [
        _msg(plugin, 1, at, "inbound"),
        _msg(plugin, 1, at, "inbound", edited=True, observed=later),
        _msg(plugin, 1, at, "inbound", edited=True, observed=later + timedelta(minutes=1)),
        _msg(plugin, 2, at + timedelta(minutes=2), "outbound"),
        _msg(plugin, 2, at + timedelta(minutes=2), "unknown", edited=True, observed=later),
        _msg(plugin, 3, at + timedelta(minutes=3), "unknown"),
        _msg(plugin, 3, at + timedelta(minutes=3), "unknown", edited=True, observed=later),
        _msg(plugin, 4, at + timedelta(minutes=4), "inbound"),  # never edited
        # Known only from an edit, so not counted at all.
        _msg(plugin, 5, at - timedelta(days=30), "inbound", edited=True, observed=later),
        # Replaced by another message, so not counted either.
        _msg(plugin, 6, at + timedelta(minutes=6), "inbound"),
        _msg(plugin, 6, at + timedelta(minutes=6), "inbound", edited=True, observed=later),
        duplicate,
        _cls(plugin, duplicate, "likely_duplicate", later, "normalized_exact_duplicate"),
        # Edited, but sent before the range below.
        _msg(plugin, 7, at - timedelta(days=2), "outbound"),
        _msg(plugin, 7, at - timedelta(days=2), "outbound", edited=True, observed=later),
    ]

    stats = cs.compute_stats(_entry(), records, _window(cs, since="2026-10-09"))

    assert stats.edited == cs.SideCounts(received=1, sent=1, unknown=1)
    assert cs.compute_stats(_entry(), records, _window(cs)).edited.sent == 2


def test_hours_and_weekdays_follow_tz_and_dst(plugin, cs):
    try:
        ZoneInfo("Europe/Berlin")
    except ZoneInfoNotFoundError:
        pytest.skip("tz database unavailable")
    records = [
        # Berlin leaves summer time on Sunday 2026-10-25: 02:30 happens twice.
        _msg(plugin, 1, _utc(2026, 10, 25, 0, 30), "inbound"),  # 02:30 CEST
        _msg(plugin, 2, _utc(2026, 10, 25, 1, 30), "outbound"),  # 02:30 CET
        _msg(plugin, 3, _utc(2026, 10, 25, 22, 30), "unknown"),  # 23:30, unknown direction counts too
        _msg(plugin, 4, _utc(2026, 10, 25, 23, 30), "inbound"),  # 00:30 on Monday in Berlin
    ]

    berlin = cs.compute_stats(_entry(), records, _window(cs, until="2026-10-31", tz="Europe/Berlin"))
    utc = cs.compute_stats(_entry(), records, _window(cs, until="2026-10-31"))

    assert berlin.hours == tuple(2 if hour == 2 else 1 if hour in (0, 23) else 0 for hour in range(24))
    assert berlin.weekdays == (1, 0, 0, 0, 0, 0, 3)
    assert (berlin.peak_hour, berlin.peak_weekday) == (2, 6)
    assert utc.hours == tuple(1 if hour in (0, 1, 22, 23) else 0 for hour in range(24))
    assert utc.weekdays == (0, 0, 0, 0, 0, 0, 4)
    # Ties go to the earliest hour.
    assert (utc.peak_hour, utc.peak_weekday) == (0, 6)
    empty = cs.compute_stats(_entry(), [], _window(cs))
    assert empty.hours == (0,) * 24 and empty.weekdays == (0,) * 7
    assert (empty.peak_hour, empty.peak_weekday) == (None, None)


def test_streaks_and_current_streak_alive_yesterday(plugin, cs):
    days = (1, 2, 3, 7, 8, 9, 17, 18)  # two 3-day runs (the earlier wins the tie), then 17..18
    records = [_msg(plugin, day, _utc(2026, 10, day, 12, 0), "inbound") for day in days]

    def streaks(**window: str):
        return cs.compute_stats(_entry(), records, _window(cs, **window)).streaks

    # Today (2026-10-19) has no message yet, so the run that ended yesterday is still alive.
    assert streaks() == cs.Streaks(
        current_days=2, longest_days=3, longest_start=date(2026, 10, 1), longest_end=date(2026, 10, 3)
    )
    # The last day of the range decides: on 2026-10-17 only that day counts, 10-18 is after the range.
    assert streaks(until="2026-10-17").current_days == 1
    assert streaks(until="2026-10-10").current_days == 3  # alive: the run ended the day before
    assert streaks(until="2026-10-11").current_days == 0
    assert streaks(since="2026-10-08").longest_start == date(2026, 10, 8)
    assert cs.compute_stats(_entry(), [], _window(cs)).streaks == cs.Streaks()
    # Moscow is ahead: 2026-10-18 22:30 UTC is already today there.
    late = [*records, _msg(plugin, 99, _utc(2026, 10, 18, 22, 30), "inbound")]
    moscow = cs.compute_stats(_entry(), late, _window(cs, tz="Europe/Moscow")).streaks
    assert (moscow.current_days, moscow.longest_days) == (3, 3)


def test_longest_silence(plugin, cs):
    start = _utc(2026, 10, 10, 9, 0)
    records = [
        _msg(plugin, 1, start, "inbound"),
        _msg(plugin, 2, start + timedelta(hours=3), "unknown"),
        _msg(plugin, 3, start + timedelta(hours=6), "outbound"),  # a second 3h gap: the first one wins
        _msg(plugin, 4, start + timedelta(hours=7), "inbound"),
    ]

    silence = cs.compute_stats(_entry(), records, _window(cs)).silence

    assert silence == cs.Silence(seconds=3 * 3600, start=start, end=start + timedelta(hours=3))
    longer = [*records, _msg(plugin, 5, start + timedelta(days=4, hours=9), "outbound")]
    assert cs.compute_stats(_entry(), longer, _window(cs)).silence == cs.Silence(
        seconds=(4 * 24 + 2) * 3600, start=start + timedelta(hours=7), end=start + timedelta(days=4, hours=9)
    )
    assert cs.compute_stats(_entry(), records[:1], _window(cs)).silence is None
    assert cs.compute_stats(_entry(), [], _window(cs)).silence is None


@pytest.mark.parametrize(
    ("since", "until", "bucket", "start", "counts"),
    [
        ("2026-10-01", "2026-10-31", "day", "2026-10-01", [0, 0, 1, *([0] * 14), 2, *([0] * 13)]),
        # 32 days: Monday weeks from the week of Thursday 2026-10-01.
        ("2026-10-01", "2026-11-01", "week", "2026-09-28", [1, 0, 2, 0, 0]),
        ("2026-01-01", "2026-10-18", "month", "2026-01-01", [0, 0, 0, 0, 0, 0, 0, 0, 0, 3]),
        ("2020-01-01", "2026-10-18", "quarter", "2020-01-01", [*([0] * 27), 3]),
        ("2010-01-01", "2026-10-18", "year", "2010-01-01", [*([0] * 16), 3]),
        # Without --since the series starts at the first counted message and runs to now.
        (None, None, "day", "2026-10-03", [1, *([0] * 14), 2, 0]),
    ],
)
def test_activity_buckets(plugin, cs, since, until, bucket, start, counts):
    records = [
        _msg(plugin, 1, _utc(2026, 10, 3, 12, 0), "inbound"),
        _msg(plugin, 2, _utc(2026, 10, 18, 9, 0), "outbound"),
        _msg(plugin, 3, _utc(2026, 10, 18, 9, 5), "unknown"),
    ]

    activity = cs.compute_stats(_entry(), records, _window(cs, since=since, until=until)).activity

    assert (activity.bucket, activity.start.isoformat(), list(activity.counts)) == (bucket, start, counts)


def test_activity_without_start_or_messages(plugin, cs):
    assert cs.compute_stats(_entry(), [], _window(cs)).activity is None
    # A range with a start still has a (zero) series, so a chart can show the empty period.
    empty = cs.compute_stats(_entry(), [], _window(cs, since="2026-10-17"))
    assert (empty.activity.bucket, empty.activity.counts) == ("day", (0, 0, 0))
    assert (empty.activity.first_day, empty.activity.last_day) == (date(2026, 10, 17), date(2026, 10, 19))


def _previous_records(plugin) -> list[dict[str, Any]]:
    return [
        _msg(plugin, 1, _utc(2026, 9, 1, 9, 0), "inbound"),  # long before both periods
        # The previous period: 2026-10-05..2026-10-09.
        _msg(plugin, 2, _utc(2026, 10, 6, 9, 0), "inbound"),
        _msg(plugin, 3, _utc(2026, 10, 6, 9, 20), "outbound"),
        _msg(plugin, 4, _utc(2026, 10, 7, 9, 0), "inbound"),  # a day later: not a reply
        _msg(plugin, 5, _utc(2026, 10, 9, 23, 59, 59), "inbound"),
        # The range: 2026-10-10..2026-10-14.
        _msg(plugin, 6, _utc(2026, 10, 10, 0, 0), "outbound"),
        _msg(plugin, 7, _utc(2026, 10, 11, 9, 0), "inbound"),
        _msg(plugin, 8, _utc(2026, 10, 11, 9, 5), "outbound"),
        _msg(plugin, 9, _utc(2026, 10, 11, 9, 35), "inbound"),
    ]


def test_previous_period_present(plugin, cs):
    records = _previous_records(plugin)
    window = _window(cs, since="2026-10-10", until="2026-10-14")

    stats = cs.compute_stats(_entry(first_seen_at="2026-09-01T09:00:01Z"), records, window)

    previous = stats.previous
    assert (previous.since, previous.until) == (_utc(2026, 10, 5), datetime(2026, 10, 9, 23, 59, 59, 999999, UTC))
    assert previous.messages == cs.SideCounts(received=3, sent=1, unknown=0)
    assert previous.active_days.total == 3
    assert previous.replies.sent.median_seconds == 1200
    assert previous.replies.received.count == 0  # 4 comes a day after 3
    assert stats.messages.total == 4
    # 6 comes 1s after 5, but 5 is in the previous period, so 6 is not a reply.
    assert stats.replies.sent == cs.ReplyTimes(count=1, median_seconds=300, p90_seconds=300)
    assert stats.replies.received.median_seconds == 1800
    assert cs.change_percent(4, 4) == 0 and cs.change_percent(6, 4) == 50 and cs.change_percent(1, 3) == -67
    assert cs.change_percent(5, 0) is None
    # Covered exactly from its first instant is enough.
    assert cs.compute_stats(_entry(first_seen_at="2026-10-05T00:00:00Z"), records, window).previous is not None
    # A relative range runs to now, so the previous period is as long and ends at --since.
    week = cs.compute_stats(_entry(first_seen_at="2026-09-01T00:00:00Z"), records, _window(cs, since="7d")).previous
    assert (week.since, week.until) == (NOW - timedelta(days=14), NOW - timedelta(days=7, microseconds=1))

    _seed(plugin, *records)
    argv = ("--contact", "@alice", "--since", "2026-10-10", "--until", "2026-10-14")
    exit_code, output = _stats(plugin, *argv)
    assert exit_code == 0
    assert (
        "  previous 2026-10-05..2026-10-09: messages 4 -> 4 (+0%); active days 3 -> 2; "
        "reply median received - -> 30m, sent 20m -> 5m"
    ) in output.splitlines()
    assert _contact_json(plugin, *argv)["previous"] == {
        "since": "2026-10-05T00:00:00+00:00",
        "until": "2026-10-09T23:59:59.999999+00:00",
        "messages": {"total": 4},
        "active_days": {"total": 3},
        "replies": {"received": {"median_seconds": None}, "sent": {"median_seconds": 1200}},
    }


def test_previous_period_of_whole_days(cs):
    # Clocks go back in Berlin on 2026-10-25, so these ranges are not a whole number of 24 hours.
    berlin = ZoneInfo("Europe/Berlin")
    now = _utc(2026, 10, 28, 12, 0)
    retained = _utc(2026, 1, 1)

    def previous(since: str, until: str | None = None):
        args = argparse.Namespace(since=since, until=until, tz="Europe/Berlin", session_gap=None)
        window = cs.build_window(args, now=now)
        found = cs.previous_window(window, retained)
        assert found.until == window.since - timedelta(microseconds=1)
        return found.since

    # A date range that runs to now (7 dates, today partial) compares with the 7 whole days before it.
    assert previous("2026-10-22") == datetime(2026, 10, 15, tzinfo=berlin)
    assert previous("2026-10-22", "2026-10-28") == datetime(2026, 10, 15, tzinfo=berlin)
    assert previous("2026-10-28") == datetime(2026, 10, 27, tzinfo=berlin)
    # Other ranges keep their exact length: 6d2h back from 10:00 UTC, and 6d13h (and 1us) back from 22:00 UTC.
    assert previous("2026-10-22T12:00") == _utc(2026, 10, 16, 8, 0)
    assert previous("2026-10-22", "2026-10-28T12:00") == _utc(2026, 10, 15, 9, 0) - timedelta(microseconds=1)


def test_previous_period_partial_or_absent(plugin, cs):
    records = _previous_records(plugin)
    window = _window(cs, since="2026-10-10", until="2026-10-14")

    # Retained history starts inside the previous period, so the comparison would be wrong.
    assert cs.compute_stats(_entry(first_seen_at="2026-10-05T00:00:01Z"), records, window).previous is None
    assert cs.compute_stats(_entry(first_seen_at=None), records, window).previous is None
    # Without --since there is no period to compare with.
    assert cs.compute_stats(_entry(first_seen_at="2026-09-01T00:00:00Z"), records, _window(cs)).previous is None
    # A start after now has no length.
    future = _window(cs, since="2026-10-25")
    assert cs.compute_stats(_entry(first_seen_at="2026-09-01T00:00:00Z"), records, future).previous is None
    assert cs.previous_window(future, _utc(2026, 9, 1)) is None

    # Only the previous line shows when the range itself has no messages.
    _seed(plugin, *records)
    exit_code, output = _stats(plugin, "--contact", "@alice", "--since", "2026-10-15", "--until", "2026-10-16")
    assert exit_code == 0
    lines = output.splitlines()
    assert "  messages 0: received 0, sent 0, unknown direction 0" in lines
    assert (
        "  previous 2026-10-13..2026-10-14: messages 0 -> 0; active days 0 -> 0; reply median received -, sent -"
        in lines
    )
    assert not any(line.startswith(("  share", "  turns", "  peak", "  streak", "  activity")) for line in lines)


def test_text_metric_formats(cs):
    spans = {0: "0s", 40: "40s", 60: "1m", 720: "12m", 3600: "1h00m", 6000: "1h40m", 11100: "3h05m"}
    spans |= {86399: "23h59m", 86400: "1d00h", 352800: "4d02h", 30 * 86400 + 3600: "30d01h", None: "-"}
    for seconds, text in spans.items():
        assert cs._format_span(seconds) == text


def test_tz_falls_back_to_hermes(cs, monkeypatch: pytest.MonkeyPatch):
    _hermes_tz(monkeypatch, "Asia/Tokyo")
    assert cs.resolve_timezone() == (ZoneInfo("Asia/Tokyo"), "Asia/Tokyo")
    # --tz wins over Hermes.
    assert cs.resolve_timezone("UTC") == (UTC, "UTC")
    assert cs.resolve_timezone(" Europe/Moscow ")[1] == "Europe/Moscow"

    # A bad Hermes value falls back to UTC quietly.
    for broken in ("Mars/Base", "../etc", "", "  ", None, 7, RuntimeError("config.yaml is broken")):
        _hermes_tz(monkeypatch, broken)
        assert cs.resolve_timezone() == (UTC, "UTC"), broken
    _hermes_tz(monkeypatch, "Asia/Tokyo")
    # HERMES_TIMEZONE wins over config.yaml, as in Hermes; a blank one is skipped.
    monkeypatch.setenv("HERMES_TIMEZONE", " Europe/Moscow ")
    assert cs.resolve_timezone()[1] == "Europe/Moscow"
    monkeypatch.setenv("HERMES_TIMEZONE", "  ")
    assert cs.resolve_timezone()[1] == "Asia/Tokyo"
    # Not running inside Hermes at all.
    monkeypatch.delenv("HERMES_TIMEZONE", raising=False)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", None)
    assert cs.resolve_timezone() == (UTC, "UTC")

    def _no_tz_database(name: str):
        raise ZoneInfoNotFoundError(name)

    monkeypatch.setattr(cs, "ZoneInfo", _no_tz_database)
    assert cs.resolve_timezone() == cs.resolve_timezone("UTC") == (UTC, "UTC")


# --- Overview --------------------------------------------------------------------


def test_overview_sort_and_activity(plugin):
    records = [
        _msg(plugin, 1, _utc(2026, 10, 10, 9, 0), "inbound", chat=991, profile=_profile(991, "aaa")),
        _msg(plugin, 1, _utc(2026, 10, 12, 9, 0), "inbound", chat=992, profile=_profile(992, "bbb")),
        _msg(plugin, 1, _utc(2026, 10, 1, 9, 0), "inbound", chat=993, profile=_profile(993, "ccc")),
        _tomb(plugin, 1, _utc(2026, 10, 14, 9, 0), "inbound", message_at=_utc(2026, 10, 1, 9, 0), chat=993),
        _msg(plugin, 1, _utc(2026, 10, 1, 9, 0), "inbound", chat=994, profile=_profile(994, "ddd")),
        _tomb(plugin, 2, _utc(2026, 10, 14, 9, 0), "unknown", chat=994),
        _msg(
            plugin, 1, _utc(2026, 10, 11, 9, 0), "inbound", conn="business-456", chat=990, profile=_profile(990, "eee")
        ),
        _msg(plugin, 1, _utc(2026, 10, 11, 9, 0), "inbound", chat=996, profile=_profile(996, "fff")),
        # More deletion-only rows. ggg's latest deletion (10-15) is newer than ccc's (10-14),
        # but its earliest (10-06) is older than hhh's (10-13), so max vs min matters.
        _msg(plugin, 1, _utc(2026, 10, 1, 9, 0), "inbound", chat=997, profile=_profile(997, "ggg")),
        _msg(plugin, 2, _utc(2026, 10, 1, 9, 1), "inbound", chat=997, profile=_profile(997, "ggg")),
        _tomb(plugin, 1, _utc(2026, 10, 6, 9, 0), "inbound", message_at=_utc(2026, 10, 1, 9, 0), chat=997),
        _tomb(plugin, 2, _utc(2026, 10, 15, 9, 0), "inbound", message_at=_utc(2026, 10, 1, 9, 1), chat=997),
        _msg(plugin, 1, _utc(2026, 10, 1, 9, 0), "inbound", chat=998, profile=_profile(998, "hhh")),
        _tomb(plugin, 1, _utc(2026, 10, 13, 9, 0), "inbound", message_at=_utc(2026, 10, 1, 9, 0), chat=998),
    ]
    _seed(plugin, *records)
    expected = [
        ("business-123", 992, "Bbb (@bbb)"),
        ("business-123", 996, "Fff (@fff)"),
        ("business-456", 990, "Eee (@eee)"),
        ("business-123", 991, "Aaa (@aaa)"),
        ("business-123", 997, "Ggg (@ggg)"),
        ("business-123", 993, "Ccc (@ccc)"),
        ("business-123", 998, "Hhh (@hhh)"),
    ]

    document = _stats_json(plugin, "--since", "2026-10-05")
    assert [
        (contact["business_connection_id"], contact["chat_id"], contact["display_name"])
        for contact in document["contacts"]
    ] == expected
    assert [contact["deletions"]["total"] for contact in document["contacts"]] == [0, 0, 0, 0, 2, 1, 1]

    exit_code, output = _stats(plugin, "--since", "2026-10-05")
    assert exit_code == 0
    lines = output.splitlines()
    assert len(lines) == 1 + 1 + 7 + 2
    assert [line.rsplit("  ", 1)[-1] for line in lines[2:9]] == [name for _, _, name in expected]
    assert "ddd" not in output
    assert lines[-2:] == [
        "shown 7 of 8 contacts; not shown: 1 with no activity in range",
        # ddd's only deletion is of a message not in history: not a row, but still reported.
        "missing history in contacts with no activity in range: 1 deletion of a message not in history; "
        + "use --contact for details",
    ]

    # --limit cuts the oldest deletion-only row, not the most recently active one.
    limited = _stats_json(plugin, "--since", "2026-10-05", "--limit", "6")
    assert [contact["chat_id"] for contact in limited["contacts"]] == [chat for _, chat, _ in expected[:6]]
    exit_code, output = _stats(plugin, "--since", "2026-10-05", "--limit", "6")
    assert exit_code == 0
    assert output.splitlines()[-2] == "shown 6 of 8 contacts; not shown: 1 over --limit 6, 1 with no activity in range"
    # --limit is clamped to 1..5000.
    assert _stats_json(plugin, "--since", "2026-10-05", "--limit", "0")["overview"]["limit"] == 1


def test_same_chat_two_connections(plugin):
    alice_two = dict(ALICE, username="alice2")
    second_tomb = _tomb(
        plugin, 2, _utc(2026, 10, 10, 9, 5), "inbound", message_at=_utc(2026, 10, 10, 9, 0), conn="business-456"
    )
    _seed(
        plugin,
        _msg(plugin, 1, _utc(2026, 10, 10, 9, 0), "inbound"),
        _msg(plugin, 2, _utc(2026, 10, 10, 9, 0), "inbound", conn="business-456", profile=alice_two),
        second_tomb,
        _cls(plugin, second_tomb, "unexplained", _utc(2026, 10, 10, 9, 10), "no_strong_match"),
    )

    document = _stats_json(plugin)
    assert sorted(contact["business_connection_id"] for contact in document["contacts"]) == [
        "business-123",
        "business-456",
    ]
    assert _stats(plugin, "--chat", "991") == (
        1,
        "error: multiple history chats match chat=991; pass --connection explicitly",
    )
    exit_code, output = _stats(plugin, "--connection", "business-456", "--chat", "991")
    assert exit_code == 0
    assert output.splitlines()[1] == "Alice Example (@alice2) connection=business-456 chat=991"

    exit_code, output = _stats(plugin, "--connection", "business-456")
    assert exit_code == 0
    lines = output.splitlines()
    assert len(lines) == 5
    assert lines[2].endswith("Alice Example (@alice2)")
    assert lines[3] == "shown 1 of 1 contacts"
    assert (
        lines[4] == "deletion details: hermes telesec history deletions --connection business-456 --status unexplained"
    )


def test_group_chat_named_by_title(plugin):
    team = {"type": "group", "title": "Team"}
    ops = {"type": "supergroup", "title": "Ops Room", "username": "opsroom"}
    _seed(
        plugin,
        _msg(plugin, 1, _utc(2026, 10, 10, 9, 0), "inbound", chat=-1001234, profile=team),
        _msg(plugin, 1, _utc(2026, 10, 9, 9, 0), "inbound", chat=-1005678, profile=ops),
    )

    contact = _contact_json(plugin, "--connection", "business-123", "--chat=-1001234")
    assert (contact["chat_id"], contact["chat_type"]) == (-1001234, "group")
    assert (contact["display_name"], contact["name"], contact["username"]) == ("Team", "Team", None)
    exit_code, output = _stats(plugin, "--contact", "Team")
    assert exit_code == 0
    assert output.splitlines()[1] == "Team connection=business-123 chat=-1001234"

    contact = _contact_json(plugin, "--contact", "@opsroom")
    assert (contact["chat_type"], contact["display_name"], contact["name"], contact["username"]) == (
        "supergroup",
        "Ops Room (@opsroom)",
        "Ops Room",
        "opsroom",
    )

    exit_code, output = _stats(plugin)
    assert exit_code == 0
    rows = output.splitlines()[2:4]
    assert rows[0].endswith("business-123  -1001234  Team")
    assert rows[1].endswith("business-123  -1005678  Ops Room (@opsroom)")


def test_details_command_runs_for_dash_leading_connection(plugin, cs):
    conn = "-Qx3_Ab-9"
    sent = _utc(2026, 10, 10, 9, 0)
    tomb = _tomb(plugin, 1, sent + timedelta(minutes=1), "inbound", message_at=sent, conn=conn, chat=992)
    _seed(
        plugin,
        _msg(plugin, 1, sent, "inbound", conn=conn, chat=992, profile=BOB),
        tomb,
        _cls(plugin, tomb, "unexplained", sent + timedelta(minutes=5), "no_strong_match"),
    )
    contact_command = f"hermes telesec history deletions --connection={conn} --chat 992 --status unexplained"
    overview_command = f"hermes telesec history deletions --connection={conn} --status unexplained"

    exit_code, output = _stats(plugin, "--contact", "@bob")
    assert exit_code == 0
    assert f"  deletion details: {contact_command}" in output.splitlines()
    assert _contact_json(plugin, "--contact", "@bob")["deletions"]["details_command"] == contact_command
    exit_code, output = _stats(plugin, f"--connection={conn}")
    assert exit_code == 0
    assert output.splitlines()[-1] == f"deletion details: {overview_command}"

    for command in (contact_command, overview_command):
        argv = shlex.split(command)
        assert argv[:2] == ["hermes", "telesec"]
        exit_code, listed = _run_history_cli(plugin, *argv[2:])
        assert exit_code == 0, listed
        assert listed != "No matching deletions found."
        assert len(listed.splitlines()) == 1

    # Quoting still applies, and a negative chat id is attached the same way.
    odd = cs._details_command("-a b", -100)
    assert odd == "hermes telesec history deletions --connection='-a b' --chat=-100 --status unexplained"
    parsed = _build_history_parser(plugin).parse_args(shlex.split(odd)[2:])
    assert (parsed.connection, parsed.chat, parsed.status) == ("-a b", "-100", "unexplained")


def test_overview_no_activity_and_empty(plugin):
    history = plugin._history_support

    exit_code, output = _stats(plugin)
    assert (exit_code, output) == (0, "No Telegram Business history found.")
    document = _stats_json(plugin)
    assert document["contacts"] == []
    assert document["overview"] == {
        "contacts_in_scope": 0,
        "active": 0,
        "shown": 0,
        "limit": 20,
        "not_shown_over_limit": 0,
        "not_shown_no_activity": 0,
        "not_shown_no_activity_partial": 0,
        "not_shown_no_activity_missing_history": {
            "deletions_of_messages_not_in_history": 0,
            "messages_known_only_from_edit": 0,
        },
    }
    assert not history.history_root().exists()


# --- Resolver, safety, read-only, fail-closed ------------------------------------


def test_resolver(plugin):
    _seed(plugin, _msg(plugin, 1, _utc(2026, 10, 10, 9, 0), "inbound"))

    assert _stats(plugin, "--contact", "@x") == (1, "error: no matching contact found for '@x'")
    assert _stats(plugin, "--chat", "991", "--contact", "@alice") == (
        1,
        "error: pass either --chat or --contact, not both",
    )
    assert _stats(plugin, "--contact", "") == (1, "error: pass --chat or --contact")


def test_message_text_never_printed(plugin):
    _overview_fixture(plugin)

    for argv in (("--contact", "@alice"), ()):
        exit_code, output = _stats(plugin, *argv, *MOSCOW_RANGE)
        assert exit_code == 0
        assert "SECRET-" not in output
        exit_code, output = _stats(plugin, *argv, *MOSCOW_RANGE, "--format", "json")
        assert exit_code == 0
        assert "SECRET-" not in output
        assert "text" not in _keys_everywhere(json.loads(output))


def test_untrusted_names_escaped(plugin):
    hostile = "Ev\x1b[31mil\u202e"
    profile = {"id": 991, "type": "private", "first_name": hostile, "last_name": "Twin\x9b"}
    raw_name = f"{hostile} Twin\x9b"
    escaped = "Ev\\x1b[31mil\\u202e Twin\\x9b"
    sent = _utc(2026, 10, 10, 9, 0)
    hostile_tomb = _tomb(plugin, 1, sent + timedelta(minutes=1), "inbound", message_at=sent)
    _seed(
        plugin,
        _msg(plugin, 1, sent, "inbound", profile=profile),
        hostile_tomb,
        _cls(plugin, hostile_tomb, "unexplained", sent + timedelta(minutes=5), "no_strong_match"),
        _msg(plugin, 2, sent, "inbound", chat=992, profile=profile),
    )

    outputs = [
        _stats(plugin, "--connection", "business-123", "--chat", "991"),
        _stats(plugin),
        _stats(plugin, "--contact", raw_name),
    ]

    assert outputs[0][0] == 0
    assert outputs[0][1].splitlines()[1] == f"{escaped} connection=business-123 chat=991"
    assert outputs[1][0] == 0
    assert any(line.endswith(f"  {escaped}") for line in outputs[1][1].splitlines())
    assert outputs[2][0] == 1
    assert "is ambiguous" in outputs[2][1]
    assert f"business-123:991={escaped}" in outputs[2][1]
    for _, output in outputs:
        assert "\x1b" not in output
        assert "\u202e" not in output
        assert "\x9b" not in output

    exit_code, raw_json = _stats(plugin, "--chat", "991", "--connection", "business-123", "--format", "json")
    assert exit_code == 0
    assert raw_json.isascii()
    assert json.loads(raw_json)["contacts"][0]["display_name"] == raw_name


def test_read_only(plugin, monkeypatch: pytest.MonkeyPatch):
    history = plugin._history_support
    _overview_fixture(plugin)
    before = snapshot_history_state(plugin)
    for name in (
        "_root_writer_lock",
        "_chat_writer_lock",
        "_append_record",
        "_write_json_atomic",
        "_rebuild_catalog_locked",
        "rebuild_contact_catalog",
        "_repair_torn_tail",
        "maintain_history",
        "_classify_due_for_chat",
        "_load_records",
        "_load_chat_state",
        "_build_chat_state",
    ):
        monkeypatch.setattr(history, name, lambda *_args, _name=name, **_kwargs: pytest.fail(f"stats called {_name}"))

    for argv in (("--contact", "@alice"), ()):
        for output_format in ("text", "json"):
            exit_code, output = _stats(plugin, *argv, *MOSCOW_RANGE, "--format", output_format)
            assert exit_code == 0, output

    assert snapshot_history_state(plugin) == before


def _break_history(plugin, case: str) -> str:
    history = plugin._history_support
    stale = "error: history contact catalog is stale; run 'hermes telesec history catalog --rebuild'"
    if case == "stale":
        append_raw_history_records(plugin, _msg(plugin, 999, _utc(2026, 10, 18, 22, 0), "inbound"))
        return stale
    if case == "dirty":
        catalog_dirty_path(plugin).write_text("{}", encoding="utf-8")
        return "error: history contact catalog is dirty; run 'hermes telesec history catalog --rebuild'"
    if case == "root_lock":
        history._root_lock_path().unlink()
        return "error: history root lock is uninitialized; "
    if case == "chat_lock":
        history._chat_lock_path(history._history_chat_dir_path("business-123", 991)).unlink()
        return "error: history chat lock is uninitialized for "
    assert case == "torn_tail"
    with th.load_history_file(plugin, month="2026-10.jsonl").open("ab") as handle:
        handle.write(b'{"event_id":"broken"')
    return stale


@pytest.mark.parametrize("mode", ["contact", "overview"])
@pytest.mark.parametrize("case", ["stale", "dirty", "root_lock", "chat_lock"])
def test_fail_closed(plugin, mode: str, case: str):
    _alice_fixture(plugin)
    expected = _break_history(plugin, case)
    before = snapshot_history_state(plugin)
    argv = ("--contact", "@alice") if mode == "contact" else ()

    exit_code, output = _stats(plugin, *argv)

    assert exit_code == 1
    assert output.startswith(expected), output
    assert len(output.splitlines()) == 1
    assert snapshot_history_state(plugin) == before


@pytest.mark.parametrize(
    ("tail", "message"),
    [
        (b'{"event_id":"broken"', "torn or invalid tail"),
        (b"not json\n", "invalid JSON"),
        (b"\xff\xfe not utf-8\n", "can't decode"),
    ],
)
def test_corrupt_history_file_fails_closed(plugin, cs, monkeypatch: pytest.MonkeyPatch, tail: bytes, message: str):
    history = plugin._history_support
    _alice_fixture(plugin)
    entry = history._resolve_history_entry(business_connection_id=None, chat_id=None, contact="@alice")
    with th.load_history_file(plugin, month="2026-10.jsonl").open("ab") as handle:
        handle.write(tail)
    before = snapshot_history_state(plugin)

    with pytest.raises(ValueError, match=message):
        cs.collect_contact_stats(entry, _window(cs))
    # Past the (now stale) catalog check, the CLI still prints one error line and changes nothing.
    monkeypatch.setattr(history, "_resolve_history_entry", lambda **_kwargs: entry)
    exit_code, output = _stats(plugin, "--contact", "@alice")
    assert exit_code == 1
    assert output.startswith("error: ") and len(output.splitlines()) == 1, output
    assert snapshot_history_state(plugin) == before


# --- Partitions ------------------------------------------------------------------


def test_partition_selection_slack(plugin, cs, monkeypatch: pytest.MonkeyPatch):
    history = plugin._history_support
    _seed(
        plugin,
        _msg(plugin, 1, _utc(2026, 7, 10, 9, 0), "inbound"),
        _msg(plugin, 2, _utc(2026, 8, 10, 9, 0), "inbound"),
        _msg(plugin, 3, _utc(2026, 9, 10, 9, 0), "inbound"),
        _msg(plugin, 4, _utc(2026, 10, 1, 0, 30), "inbound", observed=_utc(2026, 9, 30, 23, 50)),
        _msg(plugin, 5, _utc(2026, 10, 10, 9, 0), "inbound"),
    )
    calls: list[tuple[datetime | None, datetime | None, list[str]]] = []
    original = history._iter_selected_history_files

    def _spy(chat_dir: Path, *, since: datetime | None, until: datetime | None):
        paths = list(original(chat_dir, since=since, until=until))
        calls.append((since, until, [path.stem for path in paths]))
        return iter(paths)

    monkeypatch.setattr(history, "_iter_selected_history_files", _spy)

    _contact_json(plugin, "--contact", "@alice", "--since", "2026-10-01", "--tz", "Europe/Moscow")
    assert calls[-1] == (_utc(2026, 9, 1), None, ["2026-09", "2026-10"])

    # One read also covers the previous period (from 2026-09-18), so there is no second pass.
    calls.clear()
    _contact_json(plugin, "--contact", "@alice", "--since", "2026-10-04", "--tz", "Europe/Moscow")
    assert calls == [(_utc(2026, 9, 1), None, ["2026-09", "2026-10"])]

    skewed = _contact_json(plugin, "--contact", "@alice", "--since", "2026-10-01", "--tz", "UTC")
    assert skewed["messages"]["total"] == 2

    _contact_json(plugin, "--contact", "@alice")
    assert calls[-1] == (None, None, ["2026-07", "2026-08", "2026-09", "2026-10"])

    # When retained history does not cover the previous period, only the range is read.
    calls.clear()
    stats = cs.collect_contact_stats(
        _entry(first_seen_at="2026-10-01T00:00:00Z"), _window(cs, since="2026-10-04", tz="Europe/Moscow")
    )
    assert stats.previous is None
    assert calls == [(_utc(2026, 10, 1), None, ["2026-10"])]

    # The floor is the UTC month start even when a caller passes a non-UTC bound.
    window = cs.StatsWindow(
        since=datetime(2026, 10, 3, 22, 0, tzinfo=ZoneInfo("America/New_York")),
        until=None,
        tz=UTC,
        tz_label="UTC",
        session_gap=timedelta(hours=6),
        now=NOW,
    )
    assert window.read_since == _utc(2026, 10, 1)


def test_late_records_apply(plugin):
    second_tomb = _tomb(plugin, 2, _utc(2026, 10, 12, 9, 0), "inbound", message_at=_utc(2026, 10, 6, 9, 0))
    third_tomb = _tomb(plugin, 3, _utc(2026, 10, 12, 9, 0), "inbound", message_at=_utc(2026, 10, 7, 9, 0))
    _seed(
        plugin,
        _msg(plugin, 1, _utc(2026, 10, 5, 9, 0), "unknown"),
        _msg(plugin, 1, _utc(2026, 10, 5, 9, 0), "outbound", edited=True, observed=_utc(2026, 10, 15, 9, 0)),
        _msg(plugin, 2, _utc(2026, 10, 6, 9, 0), "inbound"),
        second_tomb,
        _cls(plugin, second_tomb, "likely_duplicate", _utc(2026, 10, 13, 9, 0), "normalized_exact_duplicate"),
        _msg(plugin, 3, _utc(2026, 10, 7, 9, 0), "inbound"),
        third_tomb,
        _cls(plugin, third_tomb, "unexplained", _utc(2026, 10, 13, 9, 0), "no_strong_match"),
    )

    contact = _contact_json(plugin, "--contact", "@alice", "--until", "2026-10-10")

    assert contact["messages"] == {"total": 2, "received": 1, "sent": 1, "unknown_direction": 0}
    assert contact["deletions"]["total"] == 0


# --- Wiring ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_end_to_end(enabled_history):
    plugin = enabled_history
    history = plugin._history_support
    base = _utc(2026, 10, 18, 10, 0)
    second = timedelta(seconds=1)
    # Captured before any FakeBot owner lookup for business-123: once an owner is
    # known for the connection (in memory or persisted), direction may be resolved
    # without a bot, and this message would no longer be unknown.
    await history.observe_ptb_update(
        make_business_text_update(text="no owner lookup", message_id=3, update_id=3, date=base),
        bot=None,
        now=base,
    )
    await history.observe_ptb_update(
        make_business_text_update(text="hello there", message_id=1, update_id=1, date=base + second),
        bot=FakeBot(),
        now=base + second,
    )
    await history.observe_ptb_update(
        make_business_text_update(
            text="owner reply", message_id=2, update_id=2, date=base + 2 * second, from_user_id=1000
        ),
        bot=FakeBot(),
        now=base + 2 * second,
    )
    await history.observe_ptb_update(
        make_deleted_update(message_ids=(1,), update_id=4),
        bot=FakeBot(),
        now=base + 3 * second,
    )
    history.maintain_history(now=base + timedelta(seconds=20))
    history.rebuild_contact_catalog()

    exit_code, output = _stats(plugin, "--contact", "@customer991")

    assert exit_code == 0
    lines = output.splitlines()
    assert "  messages 3: received 1, sent 1, unknown direction 1" in lines
    assert "  deletions 1: unexplained 1 (received 1, sent 0)" in lines
    assert (
        "  deletion details: hermes telesec history deletions --connection business-123 --chat 991 --status unexplained"
    ) in lines
    for text in ("hello there", "owner reply", "no owner lookup"):
        assert text not in output


def test_skill_has_stats_recipe(plugin):
    _overview_fixture(plugin)
    section = _doc_section(SKILL_DOC.read_text(encoding="utf-8"), "Stats Recipes")

    # Every recipe runs, and every JSON field the recipe names exists in real output.
    documents = []
    commands = _documented_stats_commands(section)
    assert len(commands) >= 4
    for argv in commands:
        exit_code, output = _run_history_cli(plugin, *argv)
        assert exit_code == 0, (argv, output)
        if argv[-2:] == ["--format", "json"]:
            documents.append(json.loads(output))
    by_mode = {document["mode"]: document for document in documents}
    assert set(by_mode) == {"contact", "overview"}
    references = set(re.findall(r"`((?:messages|latest|deletions|missing_history|overview)\.[a-z_]+)", section))
    assert references
    for reference in references:
        block, key = reference.split(".")
        container = by_mode["overview"]["overview"] if block == "overview" else by_mode["contact"]["contacts"][0][block]
        assert key in container, reference
