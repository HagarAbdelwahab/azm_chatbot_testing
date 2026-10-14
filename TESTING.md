# How to test the Merath chatbot

## The system under test

```
qa-merath.web.azm-dev.com          ← widget in an iframe, data-token in page source
        │
        ▼
api.chatbot-studio.azm-cit.com/api/v1
   POST /conversations                              → {"id": uuid}
   POST /chat/completions/stream?conversation_id=…  → SSE
        │
        ▼
chatbot-studio.azm-cit.com/dashboard   ← tenant `tarekat-testing`
   Agents · Knowledge Bases · Settings (branding, handoff, API keys, origins)
   Runs & Traces ← look up any run_id from a failing test
```

Three surfaces, three kinds of bug, and it matters which one you file against:

| surface | what breaks here | how you test it |
|---|---|---|
| **Studio config** | wrong model, KB not attached, dead handoff keywords, over-broad origins | read the dashboard, then assert the behaviour it promises |
| **API / agent** | grounding, hallucination, authz, PII, latency, contract drift | `pytest` (this repo) |
| **Widget** | RTL, streaming, error states, mobile, a11y, session reset | Playwright against the iframe |

**The config surface is the one people forget.** Finding #5 exists because Settings
promises keyword escalation and the agent does not deliver it — you only catch that
class of bug by reading the config and testing the promise.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install httpx pytest pytest-xdist pyyaml python-dotenv
# .env is already populated (widget key from the qa-merath page source) and gitignored
```

## Run

```bash
.venv/bin/pytest                      # contract + functional + robustness (~5 min with -n 6)
.venv/bin/pytest -m contract          # ~70 s — run this on every vendor deploy
.venv/bin/pytest -m functional -n 6
.venv/bin/pytest -m robustness -n 6
.venv/bin/pytest -m perf              # slow, excluded by default
.venv/bin/python agent_client.py      # raw SSE dump — re-run after any deploy
```

`reports/latency.json` accumulates timings for every turn any test made.

## The four layers, and why each exists

**1 · Contract** (`test_contract.py`) — deterministic, no LLM judgement. UUIDs, paired
tool steps, auth rejections, malformed bodies, oversized input. `test_response_frame_schema_is_stable`
is the drift detector: any new SSE field the vendor ships fails this test instead of
silently vanishing from your metrics. When contract goes red, the client is stale — fix
`agent_client.py` first, then re-read the other failures.

**2 · Functional** (`test_functional.py` + `data/golden.yaml`) — the golden set is the
test oracle and **it is the one file a domain expert must own**. Everything is driven
from YAML so someone who knows حصر ورثة law can add cases without touching Python.

**3 · Robustness** (`test_robustness.py`) — injection, authz, PII, escalation, edge
inputs. This is where a public-facing government assistant actually gets hurt.

**4 · Performance** (`test_performance.py`) — latency percentiles, real streaming,
concurrency. Budgets are currently aspirational; see FINDINGS.md #6/#7.

## Writing assertions against a non-deterministic system

The output changes every run, so **never assert on exact strings**. Assert on invariants:

| invariant | assertion | catches |
|---|---|---|
| did it retrieve? | `searched_kb: true` | ungrounded answers from model weights |
| did it decline? | `abstains: true/false` | over-eager answering, over-eager deflection |
| are the facts there? | `all_of: ["شهادة الوفاة"]` | incomplete answers |
| did it invent? | `none_of: ["SA0", …]` | hallucinated IBANs, fake services |
| right language? | `lang: ar` \| `en` | language-mirroring regressions |

Matching is Arabic-normalised in `conftest.py::_norm` — diacritics stripped, `أإآ→ا`,
`ى→ي`, `ة→ه` — so `الورثه` matches `الورثة` and your golden set does not rot on
orthographic variation.

**Known bugs are `xfail`, never deleted or commented out.** Each one carries a
`reason` pointing at FINDINGS.md. When the vendor fixes it the test XPASSes and you
find out immediately — that is your regression signal, for free.

## Flake or regression?

Expect **1–2 red assertions per full run**, a different one each time. That is not the
harness being unreliable — it is FINDINGS.md #3, the bot answering the same question
differently across runs. Triage rule:

```bash
.venv/bin/pytest "tests/test_functional.py::test_golden[the-failing-id]"   # serial re-run
```

- **passes on re-run** → non-determinism. Log it against finding #3; do not "fix" the
  golden entry to make it green, that just lowers the bar.
- **fails again** → a real regression. Take the `run_id` from the assertion message
  into Studio → Runs & Traces and look at what was retrieved.

There are deliberately **no automatic retries**. A retry would turn the single most
important defect in this system into a green tick.

## Loading approved Q&A from a spreadsheet

This is the highest-value way to grow the suite: your team already knows the right
answers, so let them own a spreadsheet instead of learning YAML.

**1. Put the approved answers in a sheet.** `data/tickets_template.csv` shows the shape.
Columns are matched case-insensitively and Arabic headers work, so you can import an
existing sheet without renaming anything:

| what it is | accepted column names |
|---|---|
| the user's question (required) | `question`, `q`, `سؤال`, `السؤال` |
| the approved answer (required) | `answer`, `expected`, `الإجابة`, `الجواب` |
| a stable case id | `id`, `ticket`, `رقم` — auto-generated if absent |
| `ar` / `en` | `lang` — auto-detected from the question if absent |
| free text | `notes`, `ملاحظات` |

Column order doesn't matter. Blank rows are skipped, duplicate ids are de-duplicated,
`.csv` and `.xlsx` both work.

**2. Import it.**

```bash
.venv/bin/python tools/import_tickets.py data/tickets.xlsx
```

That writes `data/golden_tickets.yaml`. It is **generated — never hand-edit it**; rerun
the importer after every spreadsheet change. Hand-written cases belong in `golden.yaml`.

**3. Run them.**

```bash
.venv/bin/pytest -m tickets -n 6
```

### How approved answers are graded

Because you supply the full approved answer rather than keywords, grading is **semantic** —
an LLM judge (`judge.py`) reads both answers and decides whether they mean the same thing.
That is what catches the failure keywords cannot:

> Approved: *"تستغرق معالجة الطلب **يومي عمل**"* · Bot: *"تستغرق **14 يوماً**"*
>
> No keyword matched either way, so an `all_of` check passes it. The judge fails it and
> reports **which** fact differs — ready to paste into a bug report.

The judge is deliberately asymmetric, because the two error types are not equally bad:

| Fails | Passes |
|---|---|
| Contradicts a fact (duration, fee, document, authority, step order) | Different wording, tone, structure, or ordering |
| Omits an essential fact | Extra detail that is correct and consistent |
| Invents an actionable claim (IBAN, deadline, fake service) | Offering further help or a clarifying question |
| Deflects when the approved answer answers | Being longer or shorter than the approved answer |

**Setup.** Add `ANTHROPIC_API_KEY=...` to `.env`. Without it the graded tests **skip** with
that message rather than failing, and everything else still runs. Sanity-check the judge
itself with `.venv/bin/python judge.py` — it grades three known answers (a reworded one, a
contradicting one, a deflecting one) so you can confirm it is calibrated before trusting it.

**Model.** Defaults to `claude-sonnet-5` — capable enough for this comparison and it runs on
every case in every run. Set `JUDGE_MODEL=claude-opus-5` in `.env` for the strongest grader
on a release gate.

**One test per case runs without an API key**: `test_ticket_answer_is_grounded` asserts the
bot doesn't deflect a question your team wrote an approved answer for, and replies in the
right language. That alone catches retrieval regressions for free.

## Extending the golden set

Add to `data/golden.yaml`:

```yaml
- id: kb-my-new-case
  q: "سؤال المستخدم"
  expect:
    searched_kb: true
    abstains: false
    all_of: ["حقيقة يجب ذكرها"]
    none_of: ["شيء يجب ألا يُذكر"]
    lang: ar
```

Multi-turn cases use a `turns:` list instead of `q:` and run in one conversation.

**Source your expectations from the knowledge base, not from the bot's current
answers.** A golden set seeded from the bot's own output only tests that the bot
still agrees with itself. The seed entries in `golden.yaml` were written this way and
are marked for review — pull the 11 indexed documents from Studio → Knowledge Bases
and check every `all_of` against them.

## What this suite cannot measure yet

RAG quality metrics — **faithfulness**, **context precision**, **context recall** —
all require the retrieved chunks, and `sources` is always `[]` (FINDINGS.md #4).
`Turn.retrieved_contexts` already maps 1:1 onto a ragas `SingleTurnSample`, so the day
citations are returned you add ragas and the plumbing is done. Until then, answer-level
assertions in the golden set are the ceiling.

## Widget testing (not yet automated)

The widget lives in an **iframe**, so target it via the frame, not the top document.
The launcher is `button[aria-label="فتح المحادثة"]`; inside are the textbox
`اكتب رسالة...`, the send button, and the manual handoff button.

Worth covering: RTL layout at mobile widths, the error bubble from FINDINGS.md #8,
"بدء محادثة جديدة" actually resetting server-side context, thumbs up/down reaching
Studio → Feedback, keyboard-only operation and screen-reader labels (this is a
government service — accessibility is likely a compliance requirement), and the
widget's behaviour when the API is slow or down (`playwright-cli route`).

## Suggested cadence

| when | what |
|---|---|
| every vendor deploy | `-m contract` — 70 s, catches breaking changes |
| every KB or prompt change in Studio | full run — this is the regression gate |
| before a release | full run + `-m perf` + manual widget pass |
| weekly | re-run `python agent_client.py` and skim for new SSE fields |
