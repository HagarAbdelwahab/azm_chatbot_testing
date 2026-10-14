"""
Semantic grader: does the bot's answer mean the same thing as the approved answer?

Keyword matching cannot tell "2 working days" from "14 days" when neither phrase
was in your `all_of` list. This can — it reads both answers and reports whether
they contradict each other on the facts.

The judge is deliberately asymmetric, because those two errors are not equally bad:

  * STRICT on contradictions — a wrong date, fee, document, or authority fails.
  * LENIENT on wording      — different phrasing, order, or extra helpful detail passes.
  * A missing fact fails; an extra *correct* fact does not.

Model: `claude-sonnet-5` by default (capable enough for this, and it runs on every
case in every suite run). Override with JUDGE_MODEL=claude-opus-5 in .env when you
want the strongest grader for a release gate.

Requires ANTHROPIC_API_KEY. Without it, judge-backed tests skip with a clear
message rather than failing — the rest of the suite is unaffected.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "claude-sonnet-5")
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

    import json
    data = json.loads(next(b.text for b in response.content if b.type == "text"))
    return Verdict(
        passed=data["verdict"] == "pass",
        reason=data["reason"],
        contradicted_facts=data["contradicted_facts"],
        missing_facts=data["missing_facts"],
        unsupported_claims=data["unsupported_claims"],
    )


if __name__ == "__main__":
    # Sanity check the judge itself: it must fail a contradicting answer and pass a reworded one.
    q = "كم تستغرق مدة معالجة طلب حصر الورثة؟"
    approved = "تستغرق معالجة الطلب يومي عمل بعد اكتمال جميع المصادقات المطلوبة من الورثة."
    for label, answer in [
        ("reworded (expect pass)", "بعد أن يصادق جميع الورثة، يحتاج الطلب إلى يوم عمل ثانٍ لينتهي."),
        ("contradicting (expect fail)", "تستغرق معالجة الطلب 14 يوماً من تاريخ التقديم."),
        ("deflecting (expect fail)", "هل تود الاستفسار عن حصر الورثة أم قسمة التركة؟"),
    ]:
        v = grade(q, approved, answer)
        print(f"{label:32} → {'pass' if v.passed else 'FAIL'} | {v.reason}")
