"""
Layer 2 — does it answer correctly, from the knowledge base, in the right language.
Every case here is driven by data/golden.yaml so a domain expert can extend the
suite without touching Python.
"""

import pytest

from conftest import assert_turn, dominant_lang, load_golden, record

pytestmark = pytest.mark.functional

GOLDEN = [g for g in load_golden() if "q" in g]
MULTITURN = [g for g in load_golden() if "turns" in g]


@pytest.mark.parametrize("case", GOLDEN, ids=lambda c: c["id"])
def test_golden(client, case):
    turn = record(client.ask(case["q"]), case["id"])
    assert_turn(turn, case["expect"])


@pytest.mark.parametrize("case", MULTITURN, ids=lambda c: c["id"])
def test_context_retention(thread, case):
    """The follow-up uses a pronoun with no subject — it only resolves if the
    conversation_id actually carries history server-side."""
    for step in case["turns"]:
        turn = record(thread.ask(step["q"]), case["id"])
        assert_turn(turn, step["expect"])


def test_conversation_id_is_sticky(thread):
    a = thread.ask("ما هي خدمة التخارج؟")
    b = thread.ask("اشرح أكثر")
    assert a.conversation_id == b.conversation_id == thread.id
    assert a.message_id != b.message_id


def test_grounded_answers_hit_the_knowledge_base(client):
    """A domain question must trigger retrieval. If the agent answers a Tarekat
    question from model weights alone it is ungrounded by definition."""
    turn = record(client.ask("ما هي شروط تقديم طلب قسمة التركة؟"), "grounding")
    assert turn.searched_kb, f"answered without retrieval: {turn.steps}\n{turn.response[:400]}"


@pytest.mark.xfail(reason="KNOWN: `sources` is always [] — see FINDINGS.md #3", strict=False)
def test_answers_expose_their_sources(client):
    """Citations are required to audit an answer and to run RAG metrics
    (faithfulness / context-precision). Without them the bot is unauditable."""
    turn = record(client.ask("ما هي المستندات المطلوبة لحصر الورثة؟"), "sources")
    assert turn.searched_kb
    assert turn.retrieved_contexts, "search_knowledge_base ran but returned no sources"


@pytest.mark.parametrize("q,want", [
    ("What documents do I need for an heir inventory request?", "en"),
    ("ما المستندات المطلوبة لطلب حصر الورثة؟", "ar"),
])
def test_replies_in_the_language_asked(client, q, want):
    turn = record(client.ask(q), "lang")
    assert dominant_lang(turn.response) == want, \
        f"asked in {want}, replied in {dominant_lang(turn.response)}: {turn.response[:300]}"


@pytest.mark.xfail(reason="KNOWN: core facts change between runs — see FINDINGS.md #3",
                   strict=False)
@pytest.mark.parametrize("q,anchors", [
    ("كم تستغرق مدة معالجة طلب حصر الورثة؟", None),          # compares the numbers
    ("كيف اقدم طلب حصر ورثة؟", ["ناجز", "شهادة الوفاة", "سجل الأسرة", "صك وكالة"]),
], ids=["duration", "procedure"])
def test_repeat_question_gives_consistent_answer(client, q, anchors):
    """Same factual question, three fresh conversations. A service duration or a
    procedure that changes between runs is a wrong answer two times out of three —
    for a Ministry of Justice service that is the highest-impact defect class after
    data leakage."""
    import re
    seen = []
    for _ in range(3):
        text = record(client.ask(q), "consistency").response
        seen.append(frozenset(re.findall(r"\d+", text)) if anchors is None
                    else frozenset(a for a in anchors if a in text))
    assert len(set(seen)) == 1, \
        f"the same question returned different facts across three runs: {[sorted(s) for s in seen]}"
