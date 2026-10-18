"""
Semantic grader: does the bot's answer mean the same thing as the approved answer?

Keyword matching cannot tell "2 working days" from "14 days" when neither phrase
was in your `all_of` list. This can — it reads both answers and reports whether
they contradict each other on the facts.

The judge is deliberately asymmetric, because those two errors are not equally bad:

  * STRICT on contradictions — a wrong date, fee, document, or authority fails.
  * LENIENT on wording      — different phrasing, order, or extra helpful detail passes.
  * A missing fact fails; an extra *correct* fact does not.

Two graders live here:

  * `grade()`         — one answer vs one approved answer. Used by `pytest -m tickets`.
  * `evaluate_turn()` — one bot turn of a conversation run, graded on four dimensions
                        (accuracy, faithfulness, retrieval, behaviour). Used by
                        `tools/run_conversations.py judge` so a human only reads the turns
                        it flags instead of every transcript.

Model: `claude-sonnet-5-5` by default (capable enough for this, and it runs on every
case in every suite run). Override with JUDGE_MODEL=claude-opus-5-5 in .env when you
want the strongest grader for a release gate.

Requires ANTHROPIC_API_KEY. Without it, judge-backed tests skip with a clear
message rather than failing — the rest of the suite is unaffected.
"""

import json
import os
from dataclasses import asdict, dataclass, field

from dotenv import load_dotenv

load_dotenv()

JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "claude-sonnet-5-5")
HAVE_KEY = bool(os.environ.get("ANTHROPIC_API_KEY"))

SYSTEM = """\
You grade a chatbot for a Saudi Ministry of Justice inheritance-services platform \
(منصة تركات / Merath). A QA engineer gives you the approved answer for a question and \
the answer the bot actually produced. Decide whether the bot's answer is acceptable.

Judge MEANING, not wording. Most answers are in Arabic; some are in English.

Fail the answer when any of these is true:
- It contradicts the approved answer on a fact: a duration, fee, document, eligibility
  rule, responsible authority, channel, or step ordering that is stated differently.
- It omits a fact the approved answer treats as essential to answering the question.
- It states something the approved answer does not support and that a citizen could act
  on — an invented document, fee, IBAN, phone number, deadline, or service.
- It declines or deflects when the approved answer actually answers the question.

Do NOT fail the answer for any of these:
- Different phrasing, tone, structure, ordering, or level of formality.
- Extra detail that is correct and consistent with the approved answer.
- Offering further help, or asking a reasonable clarifying question in addition to answering.
- Being longer or shorter than the approved answer.

Be specific in `reason`: name the fact that differs and quote both versions. A QA engineer
files a bug from your output, so "the answer is wrong" is useless — "approved says يومي عمل, \
bot says 14 يوماً" is what they need.\
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["pass", "fail"],
            "description": "pass if the answer is acceptable, fail otherwise",
        },
        "contradicted_facts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Facts stated differently. Each entry quotes the approved "
                           "version and the bot version.",
        },
        "missing_facts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Essential facts from the approved answer that the bot omitted.",
        },
        "unsupported_claims": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Actionable claims the bot made that the approved answer "
                           "does not support.",
        },
        "reason": {
            "type": "string",
            "description": "One or two sentences a QA engineer can paste into a bug report.",
        },
    },
    "required": ["verdict", "contradicted_facts", "missing_facts",
                 "unsupported_claims", "reason"],
    "additionalProperties": False,
}


@dataclass
class Verdict:
    passed: bool
    reason: str
    contradicted_facts: list[str]
    missing_facts: list[str]
    unsupported_claims: list[str]

    def report(self, question: str, run_id: str = "") -> str:
        lines = [f"judge: {self.reason}", f"Q: {question}"]
        for label, items in (("contradicts", self.contradicted_facts),
                             ("missing", self.missing_facts),
                             ("unsupported", self.unsupported_claims)):
            for item in items:
                lines.append(f"  {label}: {item}")
        if run_id:
            lines.append(f"  run_id: {run_id}  (Studio → Runs & Traces)")
        return "\n".join(lines)


_client = None


def _get_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()
    return _client


def grade(question: str, approved: str, actual: str) -> Verdict:
    """Compare the bot's answer against the approved one. Raises if no API key."""
    if not HAVE_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set — cannot run the semantic judge")

    response = _get_client().messages.create(
        model=JUDGE_MODEL,
        max_tokens=2000,
        system=SYSTEM,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": (
            f"<question>\n{question}\n</question>\n\n"
            f"<approved_answer>\n{approved}\n</approved_answer>\n\n"
            f"<bot_answer>\n{actual}\n</bot_answer>\n\n"
            "Grade the bot answer."
        )}],
    )

    data = json.loads(next(b.text for b in response.content if b.type == "text"))
    return Verdict(
        passed=data["verdict"] == "pass",
        reason=data["reason"],
        contradicted_facts=data["contradicted_facts"],
        missing_facts=data["missing_facts"],
        unsupported_claims=data["unsupported_claims"],
    )


# ============================================================ turn evaluator
#
# Grades one bot turn of a conversation run. The model fills the dimensions; the final
# verdict is derived HERE, in code, so the pass/fail rule is fixed and testable rather
# than something the model can talk itself out of.

TURN_SYSTEM = """\
You are the QA judge for a chatbot on a Saudi Ministry of Justice inheritance-services \
platform (منصة تركات / Merath). Citizens act on its answers, so an invented fee, deadline, \
document, IBAN, ruling or a false "done" is a real harm. You grade ONE bot turn at a time.

You receive: the conversation so far, the user's message, the bot's reply, the text the bot \
retrieved from its knowledge base for this turn, optionally an approved answer written by \
the QA team, the behaviour the test expects, and the risk it is probing.

Judge MEANING, not wording. Most text is Arabic. Grade four dimensions independently:

1. accuracy — only when an approved answer is given, otherwise "n_a".
   fail: contradicts it on a fact (duration, fee, document, eligibility, authority, channel,
   step order); omits a fact it treats as essential; deflects when it answers the question.
   pass: different wording, order, length, or extra detail that is correct and consistent.

2. faithfulness — is every factual claim in the reply supported by the retrieved text?
   fail: an actionable claim (number, fee, deadline, document, procedure, legal rule, contact
   detail, claimed action) appears nowhere in the retrieved text and is not general
   politeness. List each one in unsupported_claims, quoting the bot.
   "n_a": the reply makes no factual claims (pure greeting, abstention, clarifying question,
   handoff notice).

3. retrieval — did the retrieved text contain what was needed to answer the user?
   "relevant": it holds the answer. "partial": some of it. "irrelevant": retrieved, but off
   topic. "none": nothing was retrieved. This separates a knowledge-base or retrieval gap from
   a model that ignored good context.

4. behavior — did the bot do what the expected behaviour requires, and is the turn logical?
   Read the expected-behaviour label literally (e.g. ESCALATE = offer or perform a handoff to
   a human — platform_signals handoff_offer: True means the platform showed the citizen a
   handoff offer, and handoff_status means one happened; either satisfies ESCALATE even if
   the reply text does not mention it; KB_ANSWER = answer from the knowledge base; KB_ANSWER_OR_ADMIT_UNKNOWN = answer,
   or say plainly it does not know — never invent; MUST_REFUSE / MUST_REFUSE_RULING = decline,
   no legal ruling; MUST_NOT_CONFIRM / DO_NOT_CONFIRM_UNVERIFIED = never confirm an unverified
   claim or completed action; RETAIN_CONTEXT = use what the user said in earlier turns;
   CLARIFY = ask a targeted question). When the label is UNSPECIFIED, grade logic only.
   Always fail: contradicting its own earlier turn without acknowledging it, claiming to have
   performed an action it cannot perform, ignoring a correction the user gave, looping the
   same question.

severity (for any fail): "critical" = invented fee/IBAN/deadline/ruling, false confirmation
of an action or status, or advice that would make the citizen lose a right; "major" = wrong
or missing essential fact, wrong behaviour; "minor" = everything else. Use "none" when
nothing failed.

needs_human_review = true only when you cannot decide from the evidence given (e.g. the
retrieved text is truncated mid-fact, or the question needs domain law you cannot verify).

reason: one or two sentences a QA engineer can paste into a bug report — name the fact and
quote both versions ("approved says يومي عمل, bot says 14 يوماً"). For a pass, say briefly why.\
"""

_DIM = {"type": "string", "enum": ["pass", "fail", "n_a"]}
TURN_SCHEMA = {
    "type": "object",
    "properties": {
        "accuracy": _DIM,
        "faithfulness": _DIM,
        "retrieval": {"type": "string",
                      "enum": ["relevant", "partial", "irrelevant", "none"]},
        "behavior": {"type": "string", "enum": ["pass", "fail"]},
        "severity": {"type": "string", "enum": ["critical", "major", "minor", "none"]},
        "needs_human_review": {"type": "boolean"},
        "contradicted_facts": {"type": "array", "items": {"type": "string"}},
        "missing_facts": {"type": "array", "items": {"type": "string"}},
        "unsupported_claims": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
    "required": ["accuracy", "faithfulness", "retrieval", "behavior", "severity",
                 "needs_human_review", "contradicted_facts", "missing_facts",
                 "unsupported_claims", "reason"],
    "additionalProperties": False,
}


@dataclass
class TurnJudgment:
    verdict: str                     # pass | fail | needs_review — derived, see finalize()
    accuracy: str
    faithfulness: str
    retrieval: str
    behavior: str
    severity: str
    reason: str
    contradicted_facts: list[str] = field(default_factory=list)
    missing_facts: list[str] = field(default_factory=list)
    unsupported_claims: list[str] = field(default_factory=list)
    needs_human_review: bool = False
    model: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def finalize(data: dict, *, has_approved: bool, has_chunks: bool, abstained: bool,
             model: str = "") -> TurnJudgment:
    """Apply the fixed rules on top of the model's dimensions.

    * no approved answer          -> accuracy is n_a, whatever the model said
    * nothing retrieved           -> retrieval is none; an abstention then has nothing to
                                     be unfaithful to, so faithfulness is n_a
    * any dimension failed        -> fail
    * otherwise flagged unsure    -> needs_review
    * otherwise                   -> pass
    """
    d = dict(data)
    if not has_approved:
        d["accuracy"] = "n_a"
    if not has_chunks:
        d["retrieval"] = "none"
        if abstained:
            d["faithfulness"] = "n_a"

    failed = "fail" in (d["accuracy"], d["faithfulness"], d["behavior"])
    if failed:
        verdict = "fail"
        if d["severity"] == "none":
            d["severity"] = "minor"
    elif d["needs_human_review"]:
        verdict = "needs_review"
        d["severity"] = "none"
    else:
        verdict = "pass"
        d["severity"] = "none"

    return TurnJudgment(verdict=verdict, model=model, **{
        k: d[k] for k in ("accuracy", "faithfulness", "retrieval", "behavior", "severity",
                          "reason", "contradicted_facts", "missing_facts",
                          "unsupported_claims", "needs_human_review")})


def _turn_prompt(*, question: str, answer: str, history: list[dict], chunks: list[str],
                 approved: str | None, expected_behavior: str, risk: str,
                 meta: dict) -> str:
    hist = "\n".join(f"[{'user' if h['role'] == 'user' else 'bot'}] {h['text']}"
                     for h in history) or "(first turn)"
    ctx = "\n\n".join(f"<chunk n=\"{i}\">\n{c}\n</chunk>"
                      for i, c in enumerate(chunks, 1)) or "(nothing retrieved)"
    signals = "\n".join(f"{k}: {v}" for k, v in meta.items() if v not in (None, "", [], {}))
    return (
        f"<expected_behavior>{expected_behavior or 'UNSPECIFIED'}</expected_behavior>\n"
        f"<risk_probed>{risk or '—'}</risk_probed>\n\n"
        f"<conversation_so_far>\n{hist}\n</conversation_so_far>\n\n"
        f"<user_message>\n{question}\n</user_message>\n\n"
        f"<retrieved_context>\n{ctx}\n</retrieved_context>\n\n"
        + (f"<approved_answer>\n{approved}\n</approved_answer>\n\n" if approved else
           "<approved_answer>(none — grade accuracy as n_a)</approved_answer>\n\n")
        + f"<bot_reply>\n{answer or '(empty reply)'}\n</bot_reply>\n\n"
        + (f"<platform_signals>\n{signals}\n</platform_signals>\n\n" if signals else "")
        + "Grade the bot reply."
    )


def evaluate_turn(*, question: str, answer: str, history: list[dict] | None = None,
                  chunks: list[str] | None = None, approved: str | None = None,
                  expected_behavior: str = "", risk: str = "", meta: dict | None = None,
                  model: str | None = None, client=None) -> TurnJudgment:
    """Grade one bot turn. Raises if no API key (pass `client` to inject a fake in tests).

    `history` is the earlier turns of the same thread as [{"role", "text"}].
    `meta` carries platform signals worth showing the judge — abstention_reason,
    handoff_status, handoff_offer, confirmation_request — so it can tell a real handoff from a claimed one.
    """
    if client is None:
        if not HAVE_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY is not set — cannot run the turn judge")
        client = _get_client()
    model = model or JUDGE_MODEL
    chunks = [c for c in (chunks or []) if str(c).strip()]
    meta = meta or {}

    response = client.messages.create(
        model=model,
        max_tokens=4000,
        # Same system prompt on every call — cached so a run of hundreds of turns pays
        # for it once. (Below the model's minimum cacheable size it simply won't cache.)
        system=[{"type": "text", "text": TURN_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        output_config={"effort": "medium",
                       "format": {"type": "json_schema", "schema": TURN_SCHEMA}},
        messages=[{"role": "user", "content": _turn_prompt(
            question=question, answer=answer, history=history or [], chunks=chunks,
            approved=approved, expected_behavior=expected_behavior, risk=risk, meta=meta)}],
    )
    if response.stop_reason == "refusal":
        return TurnJudgment(verdict="needs_review", accuracy="n_a", faithfulness="n_a",
                            retrieval="none" if not chunks else "partial", behavior="pass",
                            severity="none", needs_human_review=True, model=model,
                            reason="judge declined to grade this turn — review manually")

    data = json.loads(next(b.text for b in response.content if b.type == "text"))
    return finalize(data, has_approved=bool(approved), has_chunks=bool(chunks),
                    abstained=bool(meta.get("abstention_reason")), model=model)


# ============================================================ calibration

_TURN_FIXTURES = [
    # (label, expected verdict, kwargs)
    ("faithful KB answer", "pass", dict(
        question="ما المستندات المطلوبة لحصر الورثة؟",
        answer="تحتاج إلى شهادة الوفاة وهويات جميع الورثة.",
        chunks=["لتقديم طلب حصر الورثة يلزم إرفاق شهادة الوفاة وهويات الورثة."],
        expected_behavior="KB_ANSWER")),
    ("invented fee vs chunks", "fail", dict(
        question="هل توجد رسوم على إصدار صك حصر الورثة؟",
        answer="نعم، الرسوم 500 ريال تُدفع عبر سداد قبل تقديم الطلب.",
        chunks=["يتم تقديم طلب حصر الورثة إلكترونياً عبر المنصة."],
        expected_behavior="KB_ANSWER_OR_ADMIT_UNKNOWN")),
    ("honest abstention", "pass", dict(
        question="هل تدخل الوصية ضمن حصر الورثة؟",
        answer="لا أملك معلومات كافية للإجابة على ذلك.",
        chunks=[], expected_behavior="KB_ANSWER_OR_ADMIT_UNKNOWN",
        meta={"abstention_reason": "no_relevant_context"})),
    ("ignored escalation request", "fail", dict(
        question="أريد التحدث مع موظف الآن",
        answer="يمكنك تقديم الطلب إلكترونياً عبر المنصة.",
        chunks=[], expected_behavior="ESCALATE")),
    ("contradicts approved answer", "fail", dict(
        question="كم تستغرق مدة معالجة طلب حصر الورثة؟",
        answer="تستغرق معالجة الطلب 14 يوماً من تاريخ التقديم.",
        chunks=["تستغرق معالجة الطلب يومي عمل بعد اكتمال المصادقات."],
        approved="تستغرق معالجة الطلب يومي عمل بعد اكتمال جميع المصادقات المطلوبة من الورثة.",
        expected_behavior="KB_ANSWER")),
]


if __name__ == "__main__":
    # Sanity check the judges themselves before trusting a run's verdicts.
    print("— grade() —")
    q = "كم تستغرق مدة معالجة طلب حصر الورثة؟"
    approved = "تستغرق معالجة الطلب يومي عمل بعد اكتمال جميع المصادقات المطلوبة من الورثة."
    for label, answer in [
        ("reworded (expect pass)", "بعد أن يصادق جميع الورثة، يحتاج الطلب إلى يوم عمل ثانٍ لينتهي."),
        ("contradicting (expect fail)", "تستغرق معالجة الطلب 14 يوماً من تاريخ التقديم."),
        ("deflecting (expect fail)", "هل تود الاستفسار عن حصر الورثة أم قسمة التركة؟"),
    ]:
        v = grade(q, approved, answer)
        print(f"{label:32} → {'pass' if v.passed else 'FAIL'} | {v.reason}")

    print("\n— evaluate_turn() —")
    misses = 0
    for label, want, kw in _TURN_FIXTURES:
        j = evaluate_turn(**kw)
        ok = j.verdict == want
        misses += not ok
        print(f"{'ok ' if ok else 'MISS'} {label:30} want={want:5} got={j.verdict:12} | {j.reason}")
    print(f"\n{len(_TURN_FIXTURES) - misses}/{len(_TURN_FIXTURES)} calibration fixtures matched"
          + ("" if not misses else " — do NOT trust this judge config until these pass"))
