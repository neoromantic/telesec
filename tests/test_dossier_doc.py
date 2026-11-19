"""The v2 dossier file (``dossier_doc``): shape, validation, atomic writes, the note hash and text helpers.

Only ``dossier_doc`` and ``history_support`` are imported; the plugin entry point never runs.
All data is made up.
"""

from __future__ import annotations

import hashlib
import json
import stat
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from dossier_helpers import core_module

NOW = datetime(2026, 10, 21, 9, 0, 5, 123456, tzinfo=UTC)
DOC_ERROR = "dossier file is corrupt or unsupported; reset it with 'hermes telesec dossier forget …'"


@pytest.fixture(scope="module")
def dd(tmp_path_factory: pytest.TempPathFactory) -> Any:
    with pytest.MonkeyPatch.context() as mp:
        yield core_module(mp, tmp_path_factory.mktemp("dossier-doc"), "dossier_doc")


def contact(dd: ModuleType, **changes: Any) -> dict[str, Any]:
    doc = dd.new_contact("biz-demo-1", 700001, NOW)
    doc.update(changes)
    return doc


def write_raw(target: Path, text: str | bytes) -> None:
    target.mkdir(parents=True, exist_ok=True)
    path = target / "document.json"
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# New documents
# ---------------------------------------------------------------------------


def test_new_contact_has_the_v2_shape(dd: ModuleType) -> None:
    doc = dd.new_contact("biz-demo-1", 700001, NOW)

    assert doc == {
        "format": 2,
        "kind": "contact",
        "id": doc["id"],
        "business_connection_id": "biz-demo-1",
        "chat_id": 700001,
        "created_at": "2026-10-21T09:00:05Z",
        "updated_at": None,
        "checked_at": None,
        "revision": 0,
        "document": "",
        "previous": None,
        "note": "",
        "note_updated_at": None,
        "note_sha": None,
        "window_start": None,
        "floor": None,
        "older_history_not_sent": False,
        "messages": 0,
        "seen": {},
        "last_call": None,
    }
    assert len(doc["id"]) == 12 and int(doc["id"], 16) >= 0
    assert dd.valid(doc)
    other = dd.new_contact(12345, "700001", NOW)
    assert (other["business_connection_id"], other["chat_id"]) == ("12345", "700001") and other["id"] != doc["id"]


def test_new_persona_has_the_v2_shape(dd: ModuleType) -> None:
    doc = dd.new_persona(NOW)

    assert doc["kind"] == "persona"
    assert doc["chats"] == [] and doc["newest_at"] is None and doc["messages"] == 0
    for contact_only in ("business_connection_id", "chat_id", "window_start", "floor", "seen"):
        assert contact_only not in doc
    assert dd.valid(doc)


# ---------------------------------------------------------------------------
# Read and write
# ---------------------------------------------------------------------------


def test_round_trip(dd: ModuleType, tmp_path: Path) -> None:
    target = tmp_path / "chats" / "biz-demo-1--0a1b2c3d" / "700001"
    doc = contact(
        dd,
        document="**Mira is your design lead at Northwind Demo.**\n\n## Work\n- Review on **28 October** [m90]",
        previous="**Mira is your design lead.**\n\n## Work\n- Review on **21 October** [m77]",
        revision=3,
        note="Мы заканчиваем проект Orbit в декабре.",
        seen={"90": ["9f1c2a7b3e4d5f60", "2026-10-19T09:02:00Z"]},
        last_call={"provider": "ai-gateway", "model": "google/gemini-3.8-flash", "at": "2026-10-21T09:00:05Z"},
        future_field={"x": 1},
    )
    dd.write_doc(target, doc)

    assert dd.read_doc(target) == doc
    assert "Мы заканчиваем" in (target / "document.json").read_text(encoding="utf-8")


def test_persona_round_trip(dd: ModuleType, tmp_path: Path) -> None:
    chats = [{"business_connection_id": "biz-demo-1", "chat_id": 700001}]
    doc = {**dd.new_persona(NOW), "chats": chats, "messages": 12, "previous": "## Style\n- Short.", "previous_chats": chats}
    dd.write_doc(tmp_path / "persona", doc)

    assert dd.read_doc(tmp_path / "persona") == doc


def test_absent_document_reads_as_none(dd: ModuleType, tmp_path: Path) -> None:
    assert dd.read_doc(tmp_path / "missing") is None
    (tmp_path / "empty").mkdir()
    assert dd.read_doc(tmp_path / "empty") is None


def test_write_creates_private_dirs_and_file(dd: ModuleType, tmp_path: Path) -> None:
    root = tmp_path / "dossiers"
    target = root / "chats" / "conn" / "700001"
    dd.write_doc(target, contact(dd))
    dd.write_doc(target, contact(dd, revision=1))  # a replace keeps the modes

    path = target / "document.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for directory in (root, root / "chats", root / "chats" / "conn", target):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert [p.name for p in target.iterdir()] == ["document.json"]  # no temporary file left
    assert json.loads(path.read_text(encoding="utf-8"))["revision"] == 1


def test_write_refuses_an_invalid_document(dd: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        dd.write_doc(tmp_path / "c", contact(dd, revision="7"))

    assert not (tmp_path / "c").exists()


def test_migration_fields_are_accepted(dd: ModuleType, tmp_path: Path) -> None:
    doc = contact(dd, document="## Work\n- Owns the Orbit redesign", migrated_from="v1", note="Keep: likes tea")
    doc["note_from"] = "v1"
    dd.write_doc(tmp_path / "c", doc)

    assert dd.read_doc(tmp_path / "c") == doc
    assert dd.converted(doc)  # revision 0 text that no v2 pass has rewritten yet
    assert not dd.converted({**doc, "window_start": "2026-07-23T00:00:00Z"})
    assert not dd.converted({**doc, "document": ""})


def test_an_earlier_seed_field_reads_as_the_document(dd: ModuleType, tmp_path: Path) -> None:
    """Files an earlier build of this version wrote kept the v1 facts beside an empty document."""
    seed = "## Work\n- Owns the Orbit redesign"
    dd.write_doc(tmp_path / "c", contact(dd, seed=seed, migrated_from="v1"))
    dd.write_doc(tmp_path / "built", contact(dd, seed=seed, document="**Mira.**", window_start="2026-07-23T00:00:00Z"))

    doc = dd.read_doc(tmp_path / "c")
    assert doc["document"] == seed and "seed" not in doc and dd.converted(doc)
    built = dd.read_doc(tmp_path / "built")
    assert built["document"] == "**Mira.**" and "seed" not in built


@pytest.mark.parametrize(
    "raw",
    ['{"note": "Mira private canary"', '{"note": "Mira private canary"}', "[]", b"\xff\xfe\x00"],
    ids=["bad-json", "invalid-doc", "list", "bad-utf8"],
)
def test_unreadable_files_raise_a_fixed_doc_error(dd: ModuleType, tmp_path: Path, raw: str | bytes) -> None:
    write_raw(tmp_path / "secret-chat", raw)

    with pytest.raises(dd.DocError) as caught:
        dd.read_doc(tmp_path / "secret-chat")
    assert str(caught.value) == DOC_ERROR and isinstance(caught.value, ValueError)
    # The JSON error (which quotes the text) is not chained either.
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None or caught.value.__suppress_context__


BAD_FIELDS = [
    ("format", 1),
    ("format", 2.0),
    ("kind", "dossier"),
    ("kind", ["contact"]),
    ("id", ""),
    ("id", 42),
    ("revision", "3"),
    ("revision", -1),
    ("revision", True),
    ("document", None),
    ("previous", 5),
    ("note", None),
    ("note_sha", 12),
    ("checked_at", 1700000000),
    ("messages", 1.5),
    ("last_call", "ai-gateway"),
    ("business_connection_id", 12345),
    ("chat_id", None),
    ("chat_id", False),
    ("window_start", 0),
    ("older_history_not_sent", "no"),
    ("seen", []),
    ("seen", {"90": "9f1c2a7b3e4d5f60"}),
    ("seen", {"90": ["9f1c2a7b3e4d5f60"]}),
    ("seen", {"90": [None, "2026-10-19T09:02:00Z"]}),
    ("seed", 5),
    ("migrated_from", 1),
]


@pytest.mark.parametrize(("field", "value"), BAD_FIELDS, ids=[f"{f}={v!r}" for f, v in BAD_FIELDS])
def test_bad_field_types_raise_doc_error(dd: ModuleType, tmp_path: Path, field: str, value: Any) -> None:
    write_raw(tmp_path / "c", json.dumps(contact(dd, **{field: value})))

    with pytest.raises(dd.DocError, match="corrupt or unsupported"):
        dd.read_doc(tmp_path / "c")


@pytest.mark.parametrize("field", ["window_start", "last_call"])
def test_missing_fields_raise_doc_error(dd: ModuleType, tmp_path: Path, field: str) -> None:
    doc = contact(dd)
    del doc[field]
    write_raw(tmp_path / "c", json.dumps(doc))

    with pytest.raises(dd.DocError):
        dd.read_doc(tmp_path / "c")


@pytest.mark.parametrize(("field", "value"), [("chats", None), ("chats", ["c1"]), ("newest_at", 3),
                                              ("previous_chats", ["c1"])])
def test_bad_persona_fields_raise_doc_error(dd: ModuleType, tmp_path: Path, field: str, value: Any) -> None:
    doc = dd.new_persona(NOW)
    doc[field] = value
    write_raw(tmp_path / "persona", json.dumps(doc))

    with pytest.raises(dd.DocError):
        dd.read_doc(tmp_path / "persona")


def test_a_v1_file_is_never_read(dd: ModuleType, tmp_path: Path) -> None:
    target = tmp_path / "c"
    target.mkdir()
    (target / "dossier.json").write_text(json.dumps({"schema_version": 1, "kind": "contact"}), encoding="utf-8")

    assert dd.read_doc(target) is None
    write_raw(target, json.dumps({"schema_version": 1, "kind": "contact", "facts": []}))
    with pytest.raises(dd.DocError):
        dd.read_doc(target)


# ---------------------------------------------------------------------------
# Note hash, units and note cleaning
# ---------------------------------------------------------------------------


def test_note_sha_follows_the_note(dd: ModuleType) -> None:
    assert dd.note_sha(contact(dd)) is None
    assert dd.note_sha({}) is None

    first = dd.note_sha(contact(dd, note="We stop working on Orbit after December."))
    assert first == hashlib.sha256(b"We stop working on Orbit after December.").hexdigest()[:16]
    assert dd.note_sha(contact(dd, note="We stop working on Orbit after November.")) != first
    # Only the note counts: the document, cursor and other fields do not change it.
    assert dd.note_sha(contact(dd, note="We stop working on Orbit after December.", document="## A\n- b")) == first
    assert dd.note_sha(dd.new_persona(NOW) | {"note": "We stop working on Orbit after December."}) == first


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Mira", 4),
        ("привет", 6),
        ("👍", 2),
        ("👨\u200d👩\u200d👧", 8),
    ],
)
def test_units_count_utf16(dd: ModuleType, text: str, expected: int) -> None:
    assert dd.units(text) == expected


def test_clean_note_strips_controls_and_bidi(dd: ModuleType) -> None:
    raw = "  Keep\u202e it\x07 short.\x7f\r\nНе\tбольше 🙂\u2066 двух\x00 строк.\x9b\x85\n\n "

    assert dd.clean_note(raw) == "Keep it short.\nНе больше 🙂 двух строк."
