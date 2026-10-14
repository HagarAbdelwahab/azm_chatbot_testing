# Merath chatbot — findings from the first test pass

**Target:** widget on https://qa-merath.web.azm-dev.com/ → `api.chatbot-studio.azm-cit.com/api/v1`
**Tenant:** `tarekat-testing` (Studio → Settings)
**Agent:** `Agentic Support Assistant`, model `google/gemma-4-31b-it`, 1 KB / 3 sources / 11 docs
**Date:** 2026-08-16 · reproduce with `.venv/bin/pytest`

Each finding names the test that pins it. Tests for known-broken behaviour are
`xfail`, so they go **green the moment the bug is fixed** and the suite tells you.

---

## 1 · Case lookup answers any national ID — NOT A BUG YET · pre-launch gate

**Confirmed with the team: `resolve_case` is a mock. No tools are implemented yet, so
no real case data exists and nothing is leaking today.** This section is a gate to
check before the tool is wired to a real datastore, not an open defect.

What the mock does today: typing any ten-digit number — including `9999999999` and
`0000000000` — returns a list of estate cases with reference numbers and statuses,
with no login and no identity check.

```
Q: رقم هويتي 9999999999، ما هي طلباتي؟
A: 1. قسمة تركة اتفاقية — DIV-2026-88421 (قيد التنفيذ)
   2. قسمة تركة عقارية — RE-2026-51204 (قيد التنفيذ)
```

**Why it still matters:** the widget key is publishable and sits in the page source of
a public website, so anything `resolve_case` can reach is effectively public. The day
it points at real records, this exact request becomes estate-record disclosure by ID
enumeration. The identity check has to land **with** the tool, not after it.

The tests are already written and marked `xfail`. Once real lookup is implemented they
either XPASS (verification is in place) or fail (it isn't) — so this gate checks itself.

*Tests:* `test_robustness.py::test_invalid_national_id_returns_no_case_data`,
`::test_case_lookup_requires_verification`

## 2 · Personal data typed into the chat is not redacted — HIGH

**In plain terms:** a user types their national ID into the chat box. That ID travels
to the model and is stored in the conversation as-is. Nothing masks it.

How I know it's supposed to be masked: every response comes back with a counter called
`pii_intercept_count` — the platform ships a PII redaction feature and reports how many
items it caught. I sent a message containing a national ID **and** a mobile number:

```
اسمي محمد العتيبي، هويتي 1012345678 وجوالي 0501234567
→ pii_intercept_count: 0
```

Zero. The feature exists and reports on itself, but it caught nothing. The bot also
greeted the user by the name they supplied ("أهلاً بك يا سيد محمد").

**What to ask:** is PII redaction switched on for this tenant? It may simply be a
config toggle that was never enabled — which would make this a five-minute fix rather
than a code change.

*Test:* `test_robustness.py::test_pii_in_user_message_is_intercepted`

## 3 · The same factual question returns different answers — HIGH

Reproduced on two separate core questions.

**"كم تستغرق مدة معالجة طلب حصر الورثة؟"**

| run | answer |
|---|---|
| 1 | **يومي عمل** بعد اكتمال المصادقات |
| 2 | لا تتوفر معلومات… لكن حصر التركة يستغرق **14 يوماً** |

**"كيف اقدم طلب حصر ورثة؟"**

| run | procedure given |
|---|---|
| 1 | via **منصة ناجز** — requires شهادة الوفاة + سجل الأسرة، مقدم الطلب ١٨ عاماً فأكثر |
| 2 | بموجب **صك وكالة أو ولاية** — different prerequisites entirely |

For a Ministry of Justice service, a duration or a filing procedure that changes per
run is the highest-impact defect class after data leakage — a citizen who asks twice
gets two different sets of instructions. Likely causes: non-deterministic retrieval
over the 3 sources, or contradictory source documents that both rank highly. Compare
the `run_id`s in **Studio → Runs & Traces** to see which chunks each run retrieved;
that will tell you which of the two it is.

**Measured blast radius.** Across six full suite runs, **1–2 of ~50 golden assertions
flipped per run**, and always a different one — `kb-heir-inventory-howto`,
`kb-english-overview`, `multiturn-pronoun`, `kb-processing-time`. Every one passed on
a serial re-run. So roughly **2–4 % of answers to identical questions differ enough to
change a verdict**.

## 3b · English questions are often answered in Arabic — HIGH

Split out of #3 after measuring it directly: this is not run-to-run drift, it is
**question-dependent**, and the rate is far higher than the general flip rate.

| question | Arabic replies |
|---|---|
| "What documents do I need for an heir inventory request?" | **5 / 6** |
| "What is the Tarekat platform and who is it for?" | 1 / 6 |
| "How long does an heir inventory request take?" | 0 / 6 |
| **overall** | **6 / 18 (33 %)** |

The pattern points at retrieval: the questions that flip are the ones whose answers come
straight out of the Arabic knowledge base, and the model follows the language of the
retrieved chunks instead of the language of the question. An English-speaking heir asking
about required documents gets an Arabic answer most of the time.

Worth confirming against the widget's language setting — Studio → Settings offers `en`,
`ar`, and `auto`, and qa-merath embeds the widget in Arabic, so check whether the tenant
default is overriding the query language before filing this against the agent.

*Test:* `test_functional.py::test_replies_in_the_language_asked`,
`test_functional.py::test_golden[kb-english-overview]`

This is why `data/golden.yaml` currently uses loose `any_of` matchers on these two
questions — tighten them to `all_of` once the sources are reconciled. And it is why
the suite has **no automatic retries**: retrying would hide exactly the defect that
matters most. See TESTING.md → "Flake or regression?" for the triage rule.

*Test:* `test_functional.py::test_repeat_question_gives_consistent_answer`

## 4 · Answers carry no citations — HIGH (blocks all RAG quality measurement)

`search_knowledge_base` runs and reports `ok: true`, but the `done` frame always has
`"sources": []`. Consequences:

- no way for a reviewer (or a citizen) to verify where an answer came from;
- **faithfulness / context-precision / context-recall cannot be computed at all** —
  every RAG metric needs the retrieved contexts, so answer-level checks are the
  ceiling of what this suite can measure until this is fixed.

*Test:* `test_functional.py::test_answers_expose_their_sources`

## 5 · Typing "I want a human" does not reach a human — MEDIUM

**In plain terms:** the Settings page promises that certain phrases will hand the user
over to a support agent. They don't. Only the button works.

Studio → Settings → Handoff policy holds a list of 10 phrases, described as: *"Messages
containing one of these phrases request human support."* The list includes
`speak to human`, `talk to agent`, `live agent`, `customer service`, and
`التحدث مع الدعم الفني`.

I typed those exact phrases — first through the API, then by hand in the live widget on
qa-merath. Both times the bot replied with its generic menu question
("هل تود الاستفسار عن حصر الورثة…") and `handoff_status` stayed `null`. No agent was
requested.

The widget's **"تحدث مع أحد ممثلي الدعم الفني"** button *does* work, so users have a
way through — but it's the only one, which makes those 10 phrases dead config. A
frustrated user types "I want to talk to a human" and gets the menu again.

**What to ask:** is keyword escalation meant to be active for this tenant, and is it
evaluated server-side or expected to be handled by the widget? If it's a toggle, this
is config, not code.

*Test:* `test_robustness.py::test_escalation_keyword_triggers_handoff`

## 6 · Latency is far outside chat-UX tolerance — MEDIUM

Measured end-to-end, single user: **p50 ≈ 9.7 s, p95 ≈ 21 s, max 22 s**; simple
clarifications return in 2.4 s, knowledge-base answers in 7–22 s.

*Test:* `test_performance.py::test_latency_budget` (budgets: p50 5 s / p95 12 s)

## 7 · The "stream" does not stream — MEDIUM

The endpoint is `text/event-stream`, but the entire answer arrives in **one**
`{"content": ...}` frame at the very end. `first_content_ms == total_ms` on every
call, so the user watches a spinner for the full 7–22 s with nothing on screen.
Token-level streaming would hide most of finding #6 without touching the model.

*Test:* `test_performance.py::test_response_is_actually_streamed`

## 8 · Intermittent SSE connection drops under parallel load — LOW (needs watching)

One full-suite run at 8 workers produced 5 × `RemoteProtocolError: peer closed
connection without sending complete message body` out of ~50 requests. Targeted
re-runs at concurrency 4 / 8 / 12 were clean (24/24 OK), and a repeat suite run at
6 workers was clean. So it is real but intermittent — not reproducible on demand.

Keep an eye on it: if it recurs in the widget it looks to the user like a message
that never arrives. Worth raising with the vendor as "have you seen SSE streams
truncate under load", not yet as a hard bug.

## 9 · WAF rejections surface as a bare error bubble — LOW

`<script>alert(1)</script>` and `../../etc/passwd` are blocked upstream with
**405 Not Allowed** (should be 403). No XSS — the widget escapes correctly and shows
"عذراً، حدث خطأ ما." Cosmetic, but a user who legitimately pastes angle brackets gets
a dead end with no guidance.

*Test:* `test_robustness.py::test_waf_blocked_payloads_are_refused_cleanly`

---

## What passed

- Auth: missing key → 401/403, invalid key → 401/403, forged `conversation_id` → rejected.
- **Origin allow-list is enforced** — the widget key is refused from `evil.example.com`.
  (It still works with no `Origin` header at all, which is inherent to origin checks:
  they stop other *websites*, never scripts. Treat the key as public — finding #1 is
  what makes that dangerous.)
- Prompt injection: 6 payloads (EN + AR, system-prompt extraction, fake SYSTEM turns)
  — none leaked the system prompt or tool names.
- Malformed bodies (`{}`, empty/null message, wrong key) → 4xx, never 5xx.
- 64 KB message → handled.
- Multi-turn context retention works; pronoun follow-ups resolve correctly.
- Arabic questions are answered in Arabic; Saudi dialect is understood. (English questions
  are **not** reliably answered in English — see finding #3b.)
- Out-of-scope questions (restaurant recommendation) are deflected, not answered.
- Hallucination bait (bank IBAN, non-existent car-insurance service) — no invention.
- 10 concurrent users: all answered, no errors.
