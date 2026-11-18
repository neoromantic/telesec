"""The v2 dossier writer (``dossier_writer``): inputs, output checks, the writer call, prices, Jev and Clairic.

Only ``dossier_writer`` and what it imports are loaded; the plugin entry point never runs. The host
facade is ``FakeLlm``, and ``model_client.jev_evaluate`` / ``clairic_extract`` are replaced by fakes,
so nothing here touches the network. All data is made up.
"""

from __future__ import annotations

import json
import math
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from dossier_helpers import FakeLlm, core_module, doc

TODAY = "2026-10-20, Tuesday"
CURRENT = "**Mira is your design lead at Northwind Demo.**\n\n## Work\n- Owns the Orbit redesign; review on **28 October** [m90]"
SHORT = "**Mira is your design lead.**\n\n## Work\n- Review on **28 October** [m90]"


pytestmark = pytest.mark.usefixtures("no_network")


@pytest.fixture(scope="module")
def dw(tmp_path_factory: pytest.TempPathFactory) -> Any:
    with pytest.MonkeyPatch.context() as mp:
        yield core_module(mp, tmp_path_factory.mktemp("dossier-writer"), "dossier_writer")


def msg(dw: ModuleType, ref: str, text: str | None, **kwargs: Any) -> Any:
    kwargs.setdefault("at", "2026-10-19 09:02")
    kwargs.setdefault("sender", "contact")
    return dw.WMsg(ref=ref, text=text, **kwargs)


def contact_payload(dw: ModuleType, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("today", TODAY)
    kwargs.setdefault("mode", "update")
    kwargs.setdefault("current", CURRENT)
    kwargs.setdefault("note", "")
    kwargs.setdefault("context", [])
    kwargs.setdefault("messages", [msg(dw, "m101", "My new number is +1 555 555-0111.")])
    return json.loads(dw.contact_input(**kwargs))


def long_document(units: int = 3300) -> str:
    bullets = [f"- Fact number {n} about the Orbit project and its review on 28 October" for n in range(200)]
    text = "**Mira is your design lead.**\n\n## Work\n" + "\n".join(bullets)
    return text[:units]


def write(dw: ModuleType, llm: FakeLlm, **kwargs: Any) -> Any:
    kwargs.setdefault("kind", "contact")
    kwargs.setdefault(
        "payload",
        dw.contact_input(
            today=TODAY,
            mode="update",
            current=CURRENT,
            note="",
            context=[],
            messages=[msg(dw, "m101", "Review moved to Friday.")],
        ),
    )
    kwargs.setdefault("mode", "update")
    kwargs.setdefault("current", CURRENT)
    kwargs.setdefault("route", dw.Route(task="telesec_dossier", provider=None, model=None))
    kwargs.setdefault("timeout", 120.0)
    return dw.write(llm, **kwargs)


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def test_contact_input_keys_in_order(dw: ModuleType) -> None:
    payload = contact_payload(
        dw,
        note="We stop working on Orbit after December.",
        context=[msg(dw, "m88", "Can we move the review?", sender="owner", at="2026-10-12 09:58")],
        part=(2, 3),
        hints=[{"kind": "contact", "value": "+1 555 555-0111"}],
    )

    assert list(payload) == [
        "today",
        "mode",
        "part",
        "owner_note",
        "current_dossier",
        "context",
        "messages",
        "hints",
    ]
    assert payload["today"] == TODAY
    assert payload["part"] == {"index": 2, "total": 3}
    assert payload["owner_note"] == "We stop working on Orbit after December."
    assert payload["current_dossier"] == CURRENT
    assert payload["context"] == [
        {"ref": "m88", "at": "2026-10-12 09:58", "from": "owner", "text": "Can we move the review?"}
    ]
    assert payload["hints"] == [{"kind": "contact", "value": "+1 555 555-0111"}]


def test_contact_input_leaves_out_keys_that_do_not_apply(dw: ModuleType) -> None:
    first = contact_payload(
        dw, mode="first", current=None, context=[msg(dw, "m88", "Earlier message.", sender="owner")]
    )
    assert list(first) == ["today", "mode", "current_dossier", "messages"]
    assert first["current_dossier"] is None  # null in a first pass, never omitted

    update = contact_payload(dw, hints=[])
    assert list(update) == ["today", "mode", "current_dossier", "messages"]
    assert "hints" not in contact_payload(dw, hints=None)


def test_contact_input_is_compact_utf8_json(dw: ModuleType) -> None:
    raw = dw.contact_input(
        today=TODAY,
        mode="update",
        current=CURRENT,
        note="Не обсуждать зарплату.",
        context=[],
        messages=[msg(dw, "m7", "Привет! 👋")],
    )

    assert "Не обсуждать зарплату." in raw and "Привет! 👋" in raw
    assert "\\u" not in raw
    assert raw == json.dumps(json.loads(raw), ensure_ascii=False, separators=(",", ":"))


def test_message_text_is_clipped_at_4000(dw: ModuleType) -> None:
    payload = contact_payload(dw, messages=[msg(dw, "m1", "a" * 5000), msg(dw, "m2", "b" * 4000)])

    clipped, exact = payload["messages"]
    assert len(clipped["text"]) == 4000 and clipped["text"].endswith("a…")
    assert exact["text"] == "b" * 4000


def test_context_keeps_the_newest_ten(dw: ModuleType) -> None:
    context = [msg(dw, f"m{n}", f"Earlier message {n}", sender="owner") for n in range(1, 16)]

    payload = contact_payload(dw, context=context)

    assert [item["ref"] for item in payload["context"]] == [f"m{n}" for n in range(6, 16)]


def test_context_keeps_at_most_4000_characters(dw: ModuleType) -> None:
    context = [msg(dw, f"m{n}", str(n) * 1500) for n in range(1, 4)]

    assert [item["ref"] for item in contact_payload(dw, context=context)["context"]] == ["m2", "m3"]
    assert [item["ref"] for item in contact_payload(dw, context=[msg(dw, "m9", "x" * 4000)])["context"]] == ["m9"]
    # Each text is clipped first, so a single long context message still fits.
    (clipped,) = contact_payload(dw, context=[msg(dw, "m9", "x" * 9000)])["context"]
    assert len(clipped["text"]) == 4000


def test_message_flags_and_a_deleted_message_without_text(dw: ModuleType) -> None:
    messages = [
        msg(dw, "m77", "Between us, I'm thinking of leaving.", at="2026-10-10 11:00", voice=True, deleted=True),
        dw.WMsg(ref="m61", at=None, sender=None, text=None, deleted=True),
        msg(dw, "m90", "Let's do the review on Wednesday, 28 October.", edited=True),
    ]

    assert contact_payload(dw, messages=messages)["messages"] == [
        {
            "ref": "m77",
            "at": "2026-10-10 11:00",
            "from": "contact",
            "text": "Between us, I'm thinking of leaving.",
            "voice": True,
            "deleted": True,
        },
        {"ref": "m61", "deleted": True},
        {
            "ref": "m90",
            "at": "2026-10-19 09:02",
            "from": "contact",
            "text": "Let's do the review on Wednesday, 28 October.",
            "edited": True,
        },
    ]


def test_persona_input_aliases(dw: ModuleType) -> None:
    raw = dw.persona_input(
        today=TODAY,
        note="I never use emoji with clients.",
        messages=[
            ("c1", "2026-10-19 09:12", "Добрый день! Посмотрю и вернусь к вам до вечера."),
            ("c2", "2026-10-19 10:00", "Sure, Friday works."),
            ("c1", "2026-10-19 11:30", "Готово, отправил."),
        ],
    )
    payload = json.loads(raw)

    assert list(payload) == ["today", "owner_note", "messages"]
    assert [m["chat"] for m in payload["messages"]] == ["c1", "c2", "c1"]
    assert all(list(m) == ["chat", "at", "text"] for m in payload["messages"])
    assert "Добрый день" in raw
    assert "owner_note" not in json.loads(dw.persona_input(today=TODAY, note="", messages=[]))


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def test_prompts_ask_for_the_end_marker_the_note_rules_and_the_limits(dw: ModuleType) -> None:
    for prompt in (dw.WRITER_SYSTEM, dw.PERSONA_SYSTEM, dw.SHORTEN_SYSTEM):
        assert prompt.rstrip().endswith("<<END>> line.") or prompt.rstrip().endswith("exactly <<END>>.")
    assert "If it asks to leave something out, never write it" in dw.WRITER_SYSTEM
    assert f"At most {dw.DOC_TARGET_CHARS} characters" in dw.WRITER_SYSTEM
    assert f"At most {dw.PERSONA_TARGET_CHARS} characters" in dw.PERSONA_SYSTEM
    assert max(dw.DOC_TARGET_CHARS, dw.SHORTEN_TO) < dw._doc.DOC_MAX_UNITS


@pytest.mark.parametrize(("text", "expected"), [
    ("- Review on **Friday** [m90]", "- Review on **Friday**"),
    ("- Paid **900** [m901, m905]", "- Paid **900**"),
    ("- Agreed on the plan [m4000100–m4000107]", "- Agreed on the plan"),  # the live form
    ("- Agreed [m40-m47], then moved", "- Agreed, then moved"),
    ("- Agreed [m40 — m47; m52]", "- Agreed"),
    ("- Agreed [m40..47]", "- Agreed"),
    ("- Agreed [m40…m47] and [m50...52]", "- Agreed and"),
    ("- Agreed [M40] [м41] [М42, m43]", "- Agreed"),  # a capital or a Cyrillic m
    ("- Agreed [ m40 ] [m 41] [m40 , m 41]", "- Agreed"),
    ("- Agreed (m40) (m41, m42–m45)", "- Agreed"),
    ("- Paid **[m40]** and _[m41]_ `[m42]`, **900** [m43]", "- Paid and, **900**"),  # no empty emphasis left
    ("- Notes [m] [x12] [m12a] [Section m1] stay", "- Notes [m] [x12] [m12a] [Section m1] stay"),
    ("- Notes (m) (m12a) (see m12) (M) stay", "- Notes (m) (m12a) (see m12) (M) stay"),
    ("line one\n[m7] line two", "line one\n line two"),  # a ref never joins two lines
])
def test_strip_refs_removes_single_list_and_range_refs(dw: ModuleType, text: str, expected: str) -> None:
    assert dw.strip_refs(text) == expected


# ---------------------------------------------------------------------------
# parse_output
# ---------------------------------------------------------------------------


def parse_error(dw: ModuleType, text: str, **kwargs: Any) -> str:
    kwargs.setdefault("kind", "contact")
    with pytest.raises(dw.OutputError) as caught:
        dw.parse_output(text, **kwargs)
    assert isinstance(caught.value, ValueError) and str(caught.value) == caught.value.code
    return caught.value.code


@pytest.mark.parametrize(
    "text",
    [
        None,
        SHORT,
        f"{SHORT}\n<<END>>\nOne more line",
        f"{SHORT}\n<<END",
    ],
    ids=["none", "missing", "text-after", "cut"],
)
def test_no_end_marker(dw: ModuleType, text: str | None) -> None:
    assert parse_error(dw, text) == "no_end_marker"


@pytest.mark.parametrize(
    "text",
    [
        f"{SHORT}\n<<END>>",
        f"```markdown\n{SHORT}\n<<END>>\n```",
        f"```\n{SHORT}\n<<END>>\n```",
        f"```markdown\n{SHORT}\n```\n<<END>>",
        f"  \r\n{SHORT}\r\n<<END>>  \r\n\r\n",
    ],
    ids=["plain", "fence-markdown", "fence", "fence-then-marker", "crlf"],
)
def test_fences_and_line_ends_are_normalised(dw: ModuleType, text: str) -> None:
    assert dw.parse_output(text, kind="contact") == dw.Output("document", SHORT)


def test_no_change_keeps_what_the_contact_writer_was_sent(dw: ModuleType) -> None:
    reply = doc("NO_CHANGE")

    assert dw.parse_output(reply, kind="contact", mode="update", current=CURRENT) == dw.Output("no_change", "")
    # A first pass sent the older dossier (a migrated seed): keeping it makes it the document.
    assert dw.parse_output(reply, kind="contact", mode="first", current=CURRENT) == dw.Output("document", CURRENT)
    for mode in ("first", "update"):
        for current in ("", None):  # nothing was sent, so nothing is kept
            assert dw.parse_output(reply, kind="contact", mode=mode, current=current) == dw.Output("empty", "")
    for kind in ("persona", "shorten"):
        assert parse_error(dw, reply, kind=kind, mode="update", current=CURRENT) == "bad_no_change"


@pytest.mark.parametrize("tag", ["think", "reasoning", "THINK"])
def test_closed_reasoning_blocks_are_dropped(dw: ModuleType, tag: str) -> None:
    reply = f"<{tag}>The note says [m90] Mira moved the review.</{tag}>\n## Work\n- Review on **28 October**\n<<END>>"

    assert dw.parse_output(reply, kind="contact") == dw.Output("document", "## Work\n- Review on **28 October**")
    no_change = f"<{tag} type='plan'>\nNothing new here.\n</{tag}>\nNO_CHANGE\n<<END>>"
    assert dw.parse_output(no_change, kind="contact", mode="update", current=CURRENT).kind == "no_change"


@pytest.mark.parametrize(
    "text",
    [
        "<think>The note says Mira moved the review.\n## Work\n- Review on **28 October**",
        "## Work\n- Review on **28 October**\n<reasoning>Maybe add the budget",
        "Mira moved.</think>\nSo the review moved.</think>\n## Work\n- Review on **28 October**",
        "<thinking hidden='1'>\n## Work\n- Review on **28 October**",
    ],
    ids=["unclosed", "unclosed-at-end", "two-ends", "with-attributes"],
)
def test_unclosed_reasoning_is_refused(dw: ModuleType, text: str) -> None:
    assert parse_error(dw, doc(text)) == "unclosed_reasoning"


def test_reasoning_that_lost_its_opening_tag_is_dropped(dw: ModuleType) -> None:
    reply = "The note says Mira moved the review.</think>\n## Work\n- Review on **28 October**\n<<END>>"

    assert dw.parse_output(reply, kind="contact") == dw.Output("document", "## Work\n- Review on **28 October**")


def test_empty_reply(dw: ModuleType) -> None:
    assert dw.parse_output(doc("EMPTY"), kind="contact") == dw.Output("empty", "")


@pytest.mark.parametrize("text", ["<<END>>", "\u202e\x07\n<<END>>"])
def test_empty_body(dw: ModuleType, text: str) -> None:
    assert parse_error(dw, text) == "empty"


@pytest.mark.parametrize(
    "text",
    [
        "I can't help with that request.",
        "##Work\n-no space",
        "1. Review on Friday",
    ],
)
def test_unstructured(dw: ModuleType, text: str) -> None:
    assert parse_error(dw, doc(text)) == "unstructured"


def test_nested_bullets_count_as_structure(dw: ModuleType) -> None:
    assert dw.parse_output(doc("**Mira.**\n  - nested bullet"), kind="contact").kind == "document"


def test_refs_are_stripped_for_the_persona_only(dw: ModuleType) -> None:
    text = "## Work\n- Review on **28 October** [m90]\n- Budget **900** [m91, m92].\n- New phone[m101]"
    stripped = "## Work\n- Review on **28 October**\n- Budget **900**.\n- New phone"

    assert dw.parse_output(doc(text), kind="contact").text == text
    assert dw.parse_output(doc(text), kind="shorten").text == text
    assert dw.parse_output(doc(text), kind="persona").text == stripped


def test_stray_end_markers_are_dropped(dw: ModuleType) -> None:
    reply = "## Work\n<<END>>\n- Review on Friday\n  <<END>>\n<<END>>"

    assert dw.parse_output(reply, kind="contact").text == "## Work\n- Review on Friday"


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


def test_write_makes_one_call_with_the_task_route(dw: ModuleType) -> None:
    llm = FakeLlm(doc(SHORT))

    result = write(dw, llm)

    assert result.output == dw.Output("document", SHORT)
    assert result.calls == 1
    (call,) = llm.calls
    assert call["system"] == dw.WRITER_SYSTEM
    assert call["purpose"] == "telesec_dossier"
    assert call["timeout"] == 120.0
    assert call["task"] == "telesec_dossier"
    assert "provider" not in call and "model" not in call
    assert "max_tokens" not in call  # the task's extra_body caps the output
    assert call["user"]["messages"][0]["text"] == "Review moved to Friday."
    assert call["raw"] == json.dumps(call["user"], ensure_ascii=False, separators=(",", ":"))


def test_an_explicit_route_caps_every_call_including_the_shorten_call(dw: ModuleType) -> None:
    llm = FakeLlm(doc(long_document()), doc(SHORT))

    write(dw, llm, route=dw.Route(task=None, provider="gemini", model="gemini-3.8-flash"))

    assert [call["purpose"] for call in llm.calls] == ["telesec_dossier", "telesec_shorten"]
    for call in llm.calls:
        assert (call["provider"], call["model"], call["max_tokens"]) == ("gemini", "gemini-3.8-flash", 16_384)
        assert "task" not in call


def test_a_long_document_is_shortened_once(dw: ModuleType) -> None:
    long = long_document()
    llm = FakeLlm(doc(long), doc(SHORT))

    result = write(dw, llm)

    assert dw._doc.units(long) > dw._doc.DOC_MAX_UNITS
    assert result.output == dw.Output("document", SHORT)
    assert result.calls == 2
    shorten = llm.calls[1]
    assert shorten["system"] == dw.SHORTEN_SYSTEM
    assert shorten["purpose"] == "telesec_shorten"
    assert shorten["user"] == {"document": long, "max_chars": 2600}
    assert shorten["task"] == "telesec_dossier" and shorten["timeout"] == 120.0 and "max_tokens" not in shorten


def test_a_document_at_the_limit_is_not_shortened(dw: ModuleType) -> None:
    exact = long_document(3200)
    llm = FakeLlm(doc(exact))

    assert write(dw, llm).output.text == exact.strip()
    assert len(llm.calls) == 1


def test_shorten_reply_without_a_document_is_an_error(dw: ModuleType) -> None:
    llm = FakeLlm(doc(long_document()), doc("EMPTY"))

    with pytest.raises(dw.OutputError, match="bad_no_change"):
        write(dw, llm)


def test_a_shortened_persona_has_no_refs(dw: ModuleType) -> None:
    llm = FakeLlm(doc(long_document()), doc("## Plans\n- Review on **28 October** [m90]"))

    result = write(dw, llm, kind="persona", payload=json.dumps({"today": TODAY, "messages": []}), mode="first")

    assert result.output.text == "## Plans\n- Review on **28 October**"
    assert [(call["system"], call["purpose"]) for call in llm.calls] == [
        (dw.PERSONA_SYSTEM, "telesec_persona"),
        (dw.SHORTEN_SYSTEM, "telesec_shorten"),
    ]


def test_tokens_and_cost_are_summed_over_two_calls(dw: ModuleType) -> None:
    llm = FakeLlm(doc(long_document()), doc(SHORT))

    result = write(dw, llm)

    assert (result.input_tokens, result.output_tokens) == (200, 100)
    assert result.cost_usd == pytest.approx(2 * (100 * 0.75 + 50 * 3.75) / 1_000_000)
    assert (result.provider, result.model) == ("ai-gateway", "google/gemini-3.8-flash")
    assert result.ms >= 0


def test_cost_is_unknown_when_any_call_is_unpriced(dw: ModuleType) -> None:
    llm = FakeLlm(doc(long_document()), doc(SHORT))
    replies = iter([doc(long_document()), doc(SHORT)])
    reported = iter([("ai-gateway", "google/gemini-3.8-flash"), ("openrouter", "example/other-model")])

    def complete(messages: list[dict[str, Any]], **kwargs: Any) -> SimpleNamespace:
        llm.calls.append(kwargs)
        provider, model = next(reported)
        usage = SimpleNamespace(input_tokens=100, output_tokens=50)
        return SimpleNamespace(text=next(replies), provider=provider, model=model, usage=usage)

    llm.complete = complete

    result = write(dw, llm)

    assert result.cost_usd is None
    assert (result.provider, result.model) == ("openrouter", "example/other-model")  # the last call answers
    assert result.calls == 2


def test_missing_usage_counts_as_zero(dw: ModuleType) -> None:
    llm = SimpleNamespace(complete=lambda messages, **kw: SimpleNamespace(text=doc(SHORT), provider="", model=None))

    result = write(dw, llm)

    assert (result.input_tokens, result.output_tokens, result.cost_usd) == (0, 0, None)
    assert (result.provider, result.model) == (None, None)


def test_a_validation_error_carries_what_the_calls_cost(dw: ModuleType) -> None:
    with pytest.raises(dw.OutputError, match="too_long") as caught:
        write(dw, FakeLlm(doc(long_document()), doc(long_document(3400))))

    spent = caught.value.spent
    assert (spent.calls, spent.input_tokens, spent.output_tokens) == (2, 200, 100)
    assert spent.cost_usd == pytest.approx(2 * (100 * 0.75 + 50 * 3.75) / 1_000_000)


def test_a_host_error_after_a_call_still_carries_what_it_cost(dw: ModuleType) -> None:
    llm = FakeLlm(doc(long_document()), TimeoutError("shorten timed out"))

    with pytest.raises(TimeoutError) as caught:
        write(dw, llm)

    spent = caught.value.spent
    assert (spent.calls, spent.input_tokens, spent.output_tokens) == (1, 100, 50)


def test_timeout_none_leaves_the_timeout_to_the_host(dw: ModuleType) -> None:
    llm = FakeLlm(doc(SHORT))

    write(dw, llm, timeout=None)

    assert llm.calls[0]["timeout"] is None


def test_a_call_that_is_not_allowed_is_not_made(dw: ModuleType) -> None:
    llm = FakeLlm(doc(SHORT))

    with pytest.raises(dw.Stopped) as caught:
        write(dw, llm, allowed=lambda: False)

    assert llm.calls == [] and caught.value.spent.calls == 0
    assert not isinstance(caught.value, dw.OutputError)


def test_allowed_is_asked_again_before_the_shorten_call(dw: ModuleType) -> None:
    answers = iter([True, False])
    llm = FakeLlm(doc(long_document()), doc(SHORT))

    with pytest.raises(dw.Stopped) as caught:
        write(dw, llm, allowed=lambda: next(answers))

    assert len(llm.calls) == 1
    assert (caught.value.spent.calls, caught.value.spent.input_tokens) == (1, 100)


# ---------------------------------------------------------------------------
# price
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        ("ai-gateway", "google/gemini-3.8-flash", 0.75 + 3.75),
        ("vercel", "gemini-3.8-flash", 0.75 + 3.75),
        ("AI-Gateway", "Google/Gemini-3.8-Flash", 0.75 + 3.75),
        ("ai-gateway", "google/gemini-3.9-pro", None),
        ("openrouter", "google/gemini-3.8-flash", None),
        (None, "google/gemini-3.8-flash", None),
        ("ai-gateway", None, None),
    ],
)
def test_price(dw: ModuleType, provider: str | None, model: str | None, expected: float | None) -> None:
    result = dw.price(provider, model, 1_000_000, 1_000_000)

    assert result == (None if expected is None else pytest.approx(expected))


# ---------------------------------------------------------------------------
# gate_check (TypeSafe Jev)
# ---------------------------------------------------------------------------


class FakeJev:
    """Stands in for ``model_client.jev_evaluate``: records each call, answers or raises."""

    def __init__(self, dw: ModuleType, answers: Any, *, cost: float | None = 0.0002, error: Any = None) -> None:
        self.dw = dw
        self.answers = answers
        self.cost = cost
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def __call__(self, state: dict, questions: dict, *, timeout: float, opener: Any = None) -> Any:
        self.calls.append({"state": state, "questions": questions, "timeout": timeout, "opener": opener})
        if self.error is not None:
            raise self.error
        return self.dw.model_client.Evaluation(answers=self.answers, input_tokens=1500, cost_usd=self.cost, ms=240)


def fake_jev(monkeypatch: pytest.MonkeyPatch, dw: ModuleType, answers: Any, **kwargs: Any) -> FakeJev:
    fake = FakeJev(dw, answers, **kwargs)
    monkeypatch.setattr(dw.model_client, "jev_evaluate", fake)
    return fake


def test_gate_check_takes_the_higher_answer(monkeypatch: pytest.MonkeyPatch, dw: ModuleType) -> None:
    fake = fake_jev(monkeypatch, dw, {"update": 0.2, "promise": 0.81})
    opener = object()
    messages = [msg(dw, "m101", "My new number is +1 555 555-0111."), msg(dw, "m102", "Thanks!", sender="owner")]

    result = dw.gate_check(CURRENT, messages, opener=opener)

    assert result == dw.GateResult(p=0.81, input_tokens=1500, cost_usd=0.0002, ms=240)
    (call,) = fake.calls
    assert {name: question["type"] for name, question in call["questions"].items()} == {
        "update": "boolean",
        "promise": "boolean",
    }
    assert call["timeout"] == dw.model_client.GATE_TIMEOUT == 10.0
    assert call["opener"] is opener
    assert call["state"] == {
        "current_dossier": CURRENT,
        "new_messages": [
            {"ref": "m101", "at": "2026-10-19 09:02", "from": "contact", "text": "My new number is +1 555 555-0111."},
            {"ref": "m102", "at": "2026-10-19 09:02", "from": "owner", "text": "Thanks!"},
        ],
    }


def test_gate_check_clips_long_messages(monkeypatch: pytest.MonkeyPatch, dw: ModuleType) -> None:
    fake = fake_jev(monkeypatch, dw, {"update": 1.0, "promise": 1.0})

    dw.gate_check(CURRENT, [msg(dw, "m5", "x" * 6000)])

    assert len(fake.calls[0]["state"]["new_messages"][0]["text"]) == dw.MSG_MAX_CHARS


def test_gate_check_passes_model_errors_on(monkeypatch: pytest.MonkeyPatch, dw: ModuleType) -> None:
    fake_jev(monkeypatch, dw, {}, error=dw.model_client.ModelCallError("http", 429))

    with pytest.raises(dw.model_client.ModelCallError) as caught:
        dw.gate_check(CURRENT, [msg(dw, "m5", "Friday works.")])
    assert (caught.value.kind, caught.value.status) == ("http", 429)


def test_gate_check_prices_by_tokens_without_a_reported_cost(monkeypatch: pytest.MonkeyPatch, dw: ModuleType) -> None:
    fake_jev(monkeypatch, dw, {"update": 0.9, "promise": 0.1}, cost=None)

    result = dw.gate_check(CURRENT, [msg(dw, "m5", "Friday works.")])

    assert result.cost_usd == pytest.approx(1500 * 0.042e-6)


# ---------------------------------------------------------------------------
# clairic_hints (Cloudflare Clairic)
# ---------------------------------------------------------------------------


class FakeClairic:
    """Stands in for ``model_client.clairic_extract``: records each call, answers or raises."""

    def __init__(self, dw: ModuleType, answers: dict[str, Any], *, error: Any = None) -> None:
        self.dw = dw
        self.answers = answers
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def __call__(self, state: str, fields: dict, *, reference_date: str | None, timeout: float, opener: Any) -> Any:
        self.calls.append(
            {"state": state, "fields": fields, "reference_date": reference_date, "timeout": timeout, "opener": opener}
        )
        if self.error is not None:
            raise self.error
        return self.dw.model_client.Extraction(answers=self.answers, input_tokens=655, ms=460)


def fake_clairic(monkeypatch: pytest.MonkeyPatch, dw: ModuleType, answers: dict[str, Any], **kwargs: Any) -> Any:
    fake = FakeClairic(dw, answers, **kwargs)
    monkeypatch.setattr(dw.model_client, "clairic_extract", fake)
    return fake


def answer(value: Any, *, confidence: float = 0.98, p_null: float = 0.01) -> dict[str, Any]:
    return {"type": "list", "value": value, "guess": value, "confidence": confidence, "p_null": p_null}


def test_clairic_state_has_no_refs_ids_or_names(monkeypatch: pytest.MonkeyPatch, dw: ModuleType) -> None:
    fake = fake_clairic(monkeypatch, dw, {})
    messages = [
        msg(dw, "m90", "Давай в четверг,\n23 октября в 15:00.", at="2026-10-18 10:06"),
        msg(dw, "m91", "Ок. Пришлю предложение до пятницы.", sender="owner", at="2026-10-18 10:07"),
        msg(dw, "m92", "Deleted canary.", deleted=True),
        dw.WMsg(ref="m61", at=None, sender=None, text=None, deleted=True),
        msg(dw, "m120", "Голосовое про бюджет 900.", sender="unknown", voice=True, at="2026-10-18 10:09"),
    ]

    result = dw.clairic_hints(messages, today_iso="2026-10-20")

    (call,) = fake.calls
    assert call["state"] == (
        "2026-10-18 10:06 contact: Давай в четверг, 23 октября в 15:00.\n"
        "2026-10-18 10:07 owner: Ок. Пришлю предложение до пятницы.\n"
        "2026-10-18 10:09 unknown: Голосовое про бюджет 900."
    )
    assert "m9" not in call["state"] and "canary" not in call["state"]
    assert {name: (field["type"], field["items"]) for name, field in call["fields"].items()} == {
        "dates": ("list", "date"),
        "amounts": ("list", "number"),
        "contacts": ("list", "text"),
    }
    assert call["reference_date"] == "2026-10-20"
    assert call["timeout"] == dw.model_client.CLAIRIC_TIMEOUT
    assert result == dw.HintResult(hints=[], input_tokens=655, ms=460)


def test_clairic_hints_keep_confident_values(monkeypatch: pytest.MonkeyPatch, dw: ModuleType) -> None:
    fake_clairic(
        monkeypatch,
        dw,
        {
            "dates": answer(["2026-10-23", "2026-10-24"]),
            "amounts": answer([900.0, 12.5, 1500]),
            "contacts": answer(["+1 555 555-0111", "mira@example.com"]),
        },
    )

    result = dw.clairic_hints([msg(dw, "m1", "Call on 23 October.")], today_iso="2026-10-20")

    assert result.hints == [
        {"kind": "date", "value": "2026-10-23"},
        {"kind": "date", "value": "2026-10-24"},
        {"kind": "amount", "value": "900"},
        {"kind": "amount", "value": "12.5"},
        {"kind": "amount", "value": "1500"},
        {"kind": "contact", "value": "+1 555 555-0111"},
        {"kind": "contact", "value": "mira@example.com"},
    ]


@pytest.mark.parametrize(
    ("confidence", "p_null", "kept"),
    [
        (0.9, 0.09, True),
        (0.89, 0.0, False),
        (0.99, 0.1, False),
        (math.nan, 0.0, False),
        (0.99, None, False),
    ],
)
def test_clairic_hints_filter_by_confidence_and_p_null(
    monkeypatch: pytest.MonkeyPatch, dw: ModuleType, confidence: Any, p_null: Any, kept: bool
) -> None:
    fake_clairic(monkeypatch, dw, {"dates": answer(["2026-10-23"], confidence=confidence, p_null=p_null)})

    result = dw.clairic_hints([msg(dw, "m1", "Call on 23 October.")], today_iso="2026-10-20")

    assert result.hints == ([{"kind": "date", "value": "2026-10-23"}] if kept else [])


def test_clairic_hints_flatten_and_dedupe(monkeypatch: pytest.MonkeyPatch, dw: ModuleType) -> None:
    fake_clairic(
        monkeypatch,
        dw,
        {
            "dates": answer("2026-10-23"),  # a single value, not a list
            "amounts": answer([900, 900.0, "900", None, True, math.inf]),
            "contacts": answer(["  +1 555\n555-0111 ", "+1 555 555-0111", ""]),
            "unknown_field": answer(["ignored"]),
        },
    )

    result = dw.clairic_hints([msg(dw, "m1", "Call on 23 October.")], today_iso="2026-10-20")

    assert result.hints == [
        {"kind": "date", "value": "2026-10-23"},
        {"kind": "amount", "value": "900"},
        {"kind": "contact", "value": "+1 555 555-0111"},
    ]


def test_clairic_hints_ignore_malformed_answers(monkeypatch: pytest.MonkeyPatch, dw: ModuleType) -> None:
    fake_clairic(monkeypatch, dw, {"dates": ["2026-10-23"], "amounts": {"value": [900]}, "contacts": answer(None)})

    assert dw.clairic_hints([msg(dw, "m1", "Hi")], today_iso="2026-10-20").hints == []


def test_clairic_hints_are_capped_at_20(monkeypatch: pytest.MonkeyPatch, dw: ModuleType) -> None:
    fake_clairic(
        monkeypatch,
        dw,
        {"dates": answer([f"2026-11-{day:02d}" for day in range(1, 16)]), "amounts": answer(list(range(1, 16)))},
    )

    hints = dw.clairic_hints([msg(dw, "m1", "Plans for November.")], today_iso="2026-10-20").hints

    assert len(hints) == dw.HINT_MAX_ITEMS == 20
    assert [h["kind"] for h in hints] == ["date"] * 15 + ["amount"] * 5


def test_clairic_errors_propagate(monkeypatch: pytest.MonkeyPatch, dw: ModuleType) -> None:
    fake_clairic(monkeypatch, dw, {}, error=dw.model_client.ModelCallError("http", 401))

    with pytest.raises(dw.model_client.ModelCallError) as caught:
        dw.clairic_hints([msg(dw, "m1", "Call on 23 October.")], today_iso="2026-10-20")
    assert (caught.value.kind, caught.value.status) == ("http", 401)


def test_clairic_is_not_called_without_live_messages(monkeypatch: pytest.MonkeyPatch, dw: ModuleType) -> None:
    fake = fake_clairic(monkeypatch, dw, {})

    result = dw.clairic_hints(
        [msg(dw, "m1", "Deleted.", deleted=True), dw.WMsg(ref="m2", at=None, sender=None, text=None, deleted=True)],
        today_iso="2026-10-20",
    )

    assert result == dw.HintResult(hints=[], input_tokens=0, ms=0)
    assert fake.calls == []

