"""
Layer 5 — your approved Q&A, graded on meaning.

Driven by data/golden_tickets.yaml, generated from your spreadsheet:

    .venv/bin/python tools/import_tickets.py data/tickets.xlsx
    .venv/bin/pytest -m tickets -n 6

Each case runs the question through the bot, then a judge compares the reply
against your approved answer. Unlike the keyword matchers in test_functional.py,
this catches an answer that is worded differently but factually wrong.

Skips cleanly when there is no ticket file yet, or no ANTHROPIC_API_KEY.
"""

import pathlib

import pytest

import judge
from conftest import assert_turn, load_golden, record

pytestmark = pytest.mark.tickets

TICKETS = pathlib.Path(__file__).resolve().parent.parent / "data" / "golden_tickets.yaml"

CASES = load_golden(TICKETS.name) if TICKETS.exists() else []
IDS = [c["id"] for c in CASES]

needs_tickets = pytest.mark.skipif(
    not CASES,
    reason=f"no {TICKETS.name} — run: python tools/import_tickets.py <your-spreadsheet>",
)
needs_judge = pytest.mark.skipif(
    not judge.HAVE_KEY,
    reason="ANTHROPIC_API_KEY not set — add it to .env to enable semantic grading",
)


@needs_tickets
@needs_judge
@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_ticket_answer_matches_approved(client, case):
    """The bot's answer must mean what the approved answer means."""
    turn = record(client.ask(case["q"]), case["id"])
    assert turn.response.strip(), f"empty response\nQ: {case['q']}"

    verdict = judge.grade(case["q"], case["expected_answer"], turn.response)
    assert verdict.passed, (
        f"\n{verdict.report(case['q'], turn.run_id)}"
        f"\n  approved: {case['expected_answer'][:300]}"
        f"\n  bot     : {turn.response[:300]}"
    )


@needs_tickets
@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_ticket_answer_is_grounded(client, case):
    """Cheap structural check that runs without an API key: a question your team
    wrote an approved answer for is in-domain, so the bot must retrieve rather
    than deflect. Catches regressions in the retrieval layer on its own."""
    turn = record(client.ask(case["q"]), case["id"])
    assert turn.response.strip(), "empty response"
    assert turn.abstention_reason is None, (
        f"bot deflected a question your team has an approved answer for "
        f"(abstention_reason={turn.abstention_reason})\nQ: {case['q']}\nA: {turn.response[:300]}"
    )
    if lang := case.get("expect", {}).get("lang"):
        assert_turn(turn, {"lang": lang})


@needs_tickets
def test_ticket_ids_are_unique():
    """pytest node ids collide silently on duplicates — the importer dedupes, but a
    hand-edited file can reintroduce them."""
    assert len(IDS) == len(set(IDS)), \
        f"duplicate case ids: {sorted({i for i in IDS if IDS.count(i) > 1})}"
