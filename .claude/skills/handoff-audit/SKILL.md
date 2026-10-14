---
name: handoff-audit
description: "Audit human-handoff service levels on the Tarekat-Restricted production tenant — how many conversations escalated to a human, how long the citizen waited before an ops agent replied, how long the handoff then ran, and how many requests EXPIRED because nobody ever came. Read-only, GET endpoints only, no LLM calls. Use for handoff/escalation SLA questions, ops response time, queue timeouts, 'how many people gave up waiting'."
argument-hint: "[--days N | YYYY-MM-DD]"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - AskUserQuestion
---

<objective>
Answer one question: **when the bot hands a citizen to a human, does a human actually
show up — and how fast?**

Four numbers, for a window the user chooses:

1. **How many conversations requested a handoff** (and what share of all traffic).
2. **How long the citizen waited** before an ops agent first replied.
3. **How long the handoff ran** once ops engaged.
4. **How many requests EXPIRED** — the queue timed out and nobody ever came.

This is measurement, not grading. Whether a 2-minute median is good and six expiries is
an incident is the human's call.
</objective>

<safety>
Restricted is **live production**. The audit is read-only: it calls
`GET /conversation-summaries`, `GET /conversation-summaries/{id}/transcript` and
optionally `GET /analytics/handoff/stats`, and nothing else. No LLM calls, so size is a
time cost, never a bill.

**The handoff API is mostly writes — never call them.** The spec also exposes
`POST /handoff/queue/{id}/claim`, `/release`, `/resolve`, `/takeover`, `/transfer`,
`/conversations/{id}/escalate` and `/handoff/request`. Firing any of those on restricted
would claim, close or reroute a real citizen's live ticket. The audit tool has no write
path; do not add one, and do not "just try" a claim endpoint to see what it returns.

`GET /handoff/queue` and `GET /handoff/active` are safe but describe **right now**, not
history — they list who is waiting at this instant. They cannot answer a question about
last Thursday.
</safety>

<inputs>
## Step 0 — Ask which days. Always.

The tenant is always **restricted** (production) unless the user says otherwise; the
window is not assumable. Use `AskUserQuestion` if the user has not already said it.

Three ways to express it:

| Form | Use |
|---|---|
| `--days N` | the last N whole UTC days, ending now — "this week", "the last few days" |
| `--day YYYY-MM-DD` | one named UTC day |
| `--start/--end` (full ISO `Z`) | anything else — a few hours, a custom span |

The window is a **UTC** day and timestamps end in `Z`. Riyadh is UTC+3, so a handoff at
01:00 local belongs to the *previous* UTC day. Say "UTC" when you report, so nobody
reconciles your number against a local-time ops dashboard and concludes one of you is wrong.

`--count-only` sizes the run (how many conversations are in the window). It deliberately
does **not** report a handoff count — see the cost note below for why that is impossible
without the full fetch.

**On cost:** every transcript in the window must be fetched, not just the escalated ones.
The summaries list carries no handoff field, and `status` cannot stand in for it — on
observed production data 15 of 26 handoff conversations reported `status: bot`, because a
handoff that returns to the bot ends up back in that state. Prefiltering on either one
silently reports **zero handoffs**. The fetch is free but not instant: ~330 transcripts
takes a couple of minutes at `--workers 6`.
</inputs>

<run>
## Step 1 — Run the audit

```bash
cd "$(git rev-parse --show-toplevel)"   # repo root
.venv/bin/python tools/handoff_audit.py --tenant restricted --days 4 --cross-check --json
# or: --day 2026-09-10 --cross-check
# or: --start 2026-09-10T00:00:00Z --end 2026-09-13T12:00:00Z
```

| Flag | Effect |
|---|---|
| `--days N` / `--day` / `--start`+`--end` | the window (one is required — nothing is inferred) |
| `--tenant` | default `restricted`; accepts a name or uuid |
| `--cross-check` | also fetch the platform's own `/analytics/handoff/stats` |
| `--json` | write `audit.json` beside the report for further slicing |
| `--include-test` | also count playground traffic (off by default) |
| `--workers` | default 6 |
| `--count-only` | size the window, fetch no transcripts |

Writes `reports/handoff_audits/<window>/report.html` and prints `REPORT URL:` last. A
`--days N` run is filed by date range, so re-running it the same day overwrites rather
than sprawling.

**The report is paged, not one long scroll** — the same shell as the pull and replay
reports, and it imports its stylesheet from `render_replay_html` rather than restating
one, so the three stay one family. A sidebar lists every handoff conversation with its
wait and flags, plus search and the All / Expired / No reply / Slow (>5m) / Resolved
filters; the Overview page carries the headline cards, the resolution breakdown, the
service-level table, the cross-check and a clickable index; then **one page per
conversation**, opening with its timings and showing the full thread with the citizen,
the bot and the human ops turns distinguished, and a marker on the line where the
handoff was requested. `j` and `k` step through conversations.

To screenshot it, serve it first — playwright-cli blocks `file:`:
`.venv/bin/python -m http.server 8794 --directory <run-dir>`, then kill the server.
</run>

<measurement>
## Step 2 — What the numbers mean. Quote them with their caveats or not at all.

The admin API exposes exactly **two** handoff fields per conversation —
`handoff_requested_at` and `handoff_resolution_type`. There is no `claimed_at`, no
`resolved_at`, no per-conversation duration (verified against the live openapi.json,
2026-09-13). So three of the four metrics are **derived**, and each derivation has a
known bias:

**Wait = time to first human reply, not claim latency.** Handoff transcripts carry a third
role besides `user` and `assistant`: **`agent`** — a human ops message. The first `agent`
message at or after the request is the earliest *observable* moment a human engaged. An
agent who claims a ticket and reads for 30s before typing appears here as a 30s-longer
wait, so this proxy runs **longer** than a true claim latency. (Observed: derived mean
201s vs the platform's own `avg_wait_seconds` 147.9s over the same days — the gap is the
bias, in the expected direction.) Never call it "claim time".

**Duration = request → last ops message.** An agent who resolves without a parting message
shortens it. It is a floor, not exact handling time.

**Expiry is NOT derived — the platform states it.** `handoff_resolution_type == "timeout"`
IS the request that aged out. The observed vocabulary on restricted:

| value | meaning |
|---|---|
| `close` | ops resolved it |
| `timeout` | **EXPIRED — nobody came** |
| `return_to_ai` | handed back to the bot |
| `abandoned` | the user left |
| `null` | still open at audit time |

The tool checks that `timeout` conversations really have no ops reply and **warns** if
that ever stops holding. If you see that warning, re-read the threads before quoting any
expiry number — the label's meaning has changed under you.

**"Never answered" is a wider set than "expired."** A request can go unanswered and end up
labelled `abandoned` or left `null`. Report both; they are different failures.

**There is no honest "how long did they wait before giving up".** For an unanswered
request the transcript just ends — nothing records how long the citizen sat with the
widget open. The gap to the last message is reported only where it is *positive*, which
it often is not: the handoff request is frequently written after the final bot message,
which yields a small negative number. Those are counted, not timed, and the row's `n` is
smaller than the never-answered count for exactly this reason. Do not clamp a negative to
zero and present it as a measurement.
</measurement>

<crosscheck>
## Step 3 — The platform's own analytics, and why it will not match

`GET /analytics/handoff/stats` returns `total_handoffs`, `avg_wait_seconds`,
`avg_resolution_seconds`, `resolution_rate`, `return_to_ai_rate` — the vendor's own
figures, from a claim event this tool cannot see.

It takes a rolling **`days=N` counted back from now**, so it **cannot be scoped to a named
day**. Asking it about last Thursday is not possible; `--cross-check` is only meaningful
when the window is the last N days.

Expect divergence, and present it as two different measurements rather than reconciling
it. Observed over the same four days: platform `total_handoffs` 35 vs 30 counted here —
different window edges, since the rolling window starts mid-day.
</crosscheck>

<unavailable>
## What this cannot tell you — say so rather than improvising

- **Which ops agent handled what.** `agent_name` is populated only while a handoff is
  still ACTIVE and is null on every resolved conversation, so a per-agent breakdown or
  leaderboard cannot be reconstructed historically. The tool does not offer one; do not
  build one from the active-queue endpoint and present it as history.
- **A second handoff in the same conversation.** `handoff_requested_at` is one timestamp
  and keeps only the latest. Conversations whose only ops messages predate it are counted
  as never-answered and tagged `earlier-cycle`.
- **Who was waiting at a past moment.** `/handoff/queue` and `/handoff/active` are live
  state only.
</unavailable>

<reporting>
## Step 4 — Hand it over

Give the user the `REPORT URL:` and the headline line the tool prints. Lead with the two
numbers that describe a person's experience — **expired** and **never answered** — not the
medians, which flatter the queue by only counting the requests someone actually answered.

The report masks IDs, IBANs, phones and emails at render time (patterns imported from
`render_replay_html`, never redefined). Verify before sharing:

```bash
.venv/bin/python -c "
import re,pathlib
h=pathlib.Path('<run-dir>/report.html').read_text(encoding='utf-8')
print('ids:',len(re.findall(r'(?<!\d)[12]\d{9}(?!\d)',h)),
      'ibans:',len(re.findall(r'SA\d{22}',h)))"
```

Quote from the report, not from `audit.json`, and never paste raw transcript text into a
ticket or commit message.
</reporting>

<related>
- **`pull-prod-conversations`** — read and browse production threads; its report has a
  `Handoff` filter but no timings. Use it to *read* the conversations this audit counts.
- **`abstention-audit`** — how often the bot punts instead of answering. The natural
  companion: abstention is what often precedes an escalation.
- **`replay-prod-conversations`** — judging a model/config change side by side. Unrelated
  to ops service levels.
</related>
