---
name: abstention-audit
description: Audit how often a tenant's chatbot punts with the canned "لا أملك معلومات كافية للإجابة على ذلك." instead of answering — pulls every conversation in a time window off any Agent Studio tenant, finds the ones holding at least one such bot reply, and reports the abstention rate, who was left with nothing, and which questions the knowledge base cannot answer. Read-only. Use for "is the bot actually effective", abstention/refusal rate, KB gap analysis.
---

<objective>
Answer one question: **is this bot actually useful to the people talking to it?**

The canned line `لا أملك معلومات كافية للإجابة على ذلك.` is the bot saying "I have
nothing for you". Every time it fires, a real person asked a real question and left
empty-handed. Aggregate dashboards hide this — a conversation that abstained on every
single turn still counts as a "conversation served".

So: take every conversation in a window off a named tenant, find the ones with at least
one abstaining **assistant** message, and report the rate, the severity, and the list of
questions the knowledge base cannot answer.

This is measurement, NOT grading. The tool reports the rate and shows the transcripts.
Whether 88% is an incident or a known corpus gap is the human's call.
</objective>

<safety>
## Read-only, and any tenant

`tools/abstention_audit.py` issues admin-API **GETs only**. It has no send path, no
widget key, and no write of any kind — unlike `replay-prod-conversations`, there is
nothing here that can create a conversation. It is therefore safe to point at
production (`restricted`) directly, which is usually the whole point.

`--tenant` takes a friendly name (`restricted`, `testing`, `insaf`, `test`) or a raw
uuid. Unlike the replay tooling, this flag genuinely selects the target — there is no
env-var key that could silently redirect it.
</safety>

<pii>
## Real citizens' text

`audit.json` holds unredacted transcripts by design — you cannot audit what the bot
was asked without the question. `report.html` masks national IDs, IBANs, phones and
emails at render time, and shortens `ED-x-…` case numbers to their last four digits.

Everything lands under `reports/`, which this repo gitignores wholesale. Do not move
either file out of it, do not paste raw `audit.json` text into chat or a ticket, and
quote from the **report** when writing findings.

**Bare numeric request numbers are NOT masked.** The mask patterns cover 10-digit
national IDs (`[12]\d{9}`), IBANs, phones and emails; a 9-digit request number like
`411082220` passes through, same as in the replay report. If a window is going to be
shared outside the QA group, skim it first. Verify masking on the rendered HTML rather
than trusting the regex:

```bash
.venv/bin/python -c "
import re,pathlib
h=pathlib.Path('<run-dir>/report.html').read_text(encoding='utf-8')
print('unmasked ids:', len(re.findall(r'(?<!\d)[12]\d{9}(?!\d)', h)))"
```

Never anchor a new masking pattern on `\b` — Arabic letters and digits are both word
characters, so there is no boundary in `الهوية1001520665`. Use digit lookarounds.
</pii>

<inputs>
## Step 0 — Settle the tenant and the window. Ask; don't assume.

1. **Which tenant?** There is no sensible default — auditing `testing` and auditing
   `restricted` answer completely different questions. Ask unless the user said.

2. **Which window?** Three forms, all UTC:
   - `--day 2026-09-03` — a whole UTC day
   - `--start … --end …` — explicit ISO bounds, for a sub-day window
   - `--last-hours 2` — a window ending now

   Say **"UTC"** whenever you report a count. Riyadh is UTC+3, so a conversation at
   01:00 local belongs to the *previous* UTC day, and a reviewer reconciling your
   number against a local-time dashboard will otherwise conclude one of you is wrong.

Run the count first so any scoping question carries a real number:

```bash
cd "$(git rev-parse --show-toplevel)"   # repo root
.venv/bin/python tools/abstention_audit.py --tenant restricted --day <DAY> --count-only
```

This is cheap — **transcript GETs only, no LLM calls at all** — so unlike a replay there
is no spend to negotiate. A full UTC day of ~130 conversations takes well under a minute.
Default to scanning the whole window; only reach for `--limit` or `--matched-only` if
the window is enormous.
</inputs>

<run>
## Step 1 — Run it

```bash
.venv/bin/python tools/abstention_audit.py --tenant restricted --day 2026-09-03
```

Writes `reports/abstention/<tenant>/<label>/` holding `audit.json` and `report.html`,
and prints `REPORT URL:` as its last line. Re-running the same window refuses to
clobber without `--fresh`.

**The report is paged, not one long scroll.** A sidebar indexes every abstaining
conversation with its punt ratio and flags, carrying search and the All / Fully
abstained / Ended on a punt / First contact / Repeat / Escalated filters; the Overview
page holds the headline rate, the severity tiles, the knowledge-base gap table, the
matched wordings, the agent config and a clickable index; then **one page per
conversation**, with each abstaining bot turn highlighted in place. `j` and `k` step
through conversations. Serve it before screenshotting — playwright-cli blocks `file:`.

Useful flags:

| flag | when |
|---|---|
| `--phrase "<text>"` | audit a different canned line, or pin an exact full sentence |
| `--matched-only` | very large window; skips non-matching transcripts (see gotchas — costs you the turn-level rate) |
| `--limit N` | cap transcripts fetched |
| `--include-test` | also count playground traffic (default: real users only) |
| `--last-hours N` | window ending now |
| `--count-only` | how many match, fetch nothing |

**The default match is the stem `لا أملك معلومات كافية`, not the full sentence.**
Trailing punctuation and follow-on clauses vary between agent versions, and matching
the whole sentence silently drops the variants. The report lists every distinct exact
wording that matched, with counts, so widening the net never hides what was said. Pass
`--phrase` with the full sentence if you specifically need an exact-string count.
</run>

<reading>
## Step 2 — What the numbers mean

**The denominator is the finding.** "14 conversations abstained" is meaningless;
"14 of 16" is an incident. The tool always fetches the unfiltered window count and
divides against it. Never report a numerator alone.

The severity ladder, worst first — these are the categories worth leading with:

| Metric | Why it matters |
|---|---|
| **Fully abstained** | Every bot turn was a punt. The citizen got *nothing at all* from the conversation. The most damning category; open these first. |
| **Ended on a punt** | Whatever else happened, the bot's last word was "I don't know" — the user left without an answer. |
| **Failed on first contact** | It gave up before the conversation had any chance to narrow, so clarification never even ran. |
| **Abstained ≥2 times** | The user re-asked, rephrased, and got the same wall. Reads as the worst experience of the lot. |
| **Escalated to a human** | Cross-tab against abstention: did the ones the bot failed actually reach a person, or just leave? |

**"Questions the bot could not answer"** is the actionable output — the user turn
immediately preceding each abstention, most frequent first. That table is the
knowledge-base gap list, and it is the thing to hand to whoever owns the corpus.

**An abstention is not automatically the model's fault.** The report includes the
agent config at audit time for exactly this reason. Read the rate against:
- `knowledge_base_ids` — is the topic even in this tenant's corpus?
- `min_score_to_answer`, `faithfulness_threshold`, `strict_grounding` — a high rate
  can be a strict gate on good retrieval rather than a missing document.

Both stories look identical in the rate alone. Say which one the config supports, or
say you can't tell from here.

**What this tool cannot see.** The transcript API returns role, content and timestamp
only — no retrieval scores, no `retrieved_chunks`, no tool trace, no
`abstention_reason`. So it can tell you *that* the bot punted and how often, never
*why*. For the mechanism you need the live SSE frames: run the question through
`agent_client.py`, or use `replay-prod-conversations`, which captures
`abstention_reason`, `retrieval_score` and `retrieved_chunks` on the replay side.

A worked example of that pairing: on 2026-09-03 every `below_faithfulness` abstention
on testing had `retrieved_chunks: 0` while `retrieval_score` read 0.77–1.00 — nothing
retrieved, despite a score that looked excellent. This audit would have shown the rate;
only the frame-level capture showed the cause.
</reading>

<gotchas>
## Hard-won specifics

**Date-only bounds silently return zero.** `start_date=2026-09-03` matches nothing —
the API compares against a timestamp, so an empty result looks like a quiet day rather
than a bad query. Always go through `--day` / `--start` / `--last-hours`, which build
full ISO bounds.

**No tenant selected → 404 on a perfectly valid id.** The studio account is a
super-admin with `tenant_id: null`, so every call passes `tenant_id` explicitly. A 404
here means wrong tenant, not missing data.

**The server-side `q=` filter is exact but role-blind.** It is an ILIKE over message
content and *does* match assistant text — verified 2026-09-03 against restricted
09:55–11:55Z, where `q` returned 14 and hand-counting transcripts returned the same 14.
But it cannot distinguish an assistant message from a user one, so a citizen quoting the
phrase back would be a false positive. Every candidate is re-checked on the transcript
with `role == "assistant"` before it counts. Do not "optimise" that check away.

**`--matched-only` costs you the turn-level rate, and the report says so.** In that mode
only matching conversations are fetched, so there is no honest denominator for "X% of all
bot turns" — the tool prints the count with no percentage and the report carries a
caveat box. It does *not* estimate. Conversation-level rates stay exact, because the
denominator comes from the unfiltered count either way.

**Auditing `testing` includes your own traffic.** Replays from
`replay-prod-conversations` and runs from `chatbot-conversation-run` go through the
widget endpoint, so the API classifies them as real (not playground) traffic and they
land in the audit. A 69-conversation "day" on testing may be mostly your own replays.
Either audit `restricted` for genuine user behaviour, or subtract your runs knowingly —
never present a testing-tenant rate as a user-facing metric without saying this.

**A high rate can be baseline, not an incident.** Production has been abstaining at
41% (2026-09-02, full day) and 56–58% (2026-09-03, two-hour windows). Before calling a
number alarming, get a comparison window from a known-good period — one window in
isolation cannot tell a regression from the status quo.

**Not the same thing as the other two chatbot skills.** `replay-prod-conversations`
replays traffic onto a target and diffs answers (writes to `reports/replay/`).
`chatbot-conversation-run` drives an authored CSV (writes to `reports/conversations/`).
This one only reads and counts (writes to `reports/abstention/`). Keep the directories
apart.
</gotchas>

<output>
## Step 3 — Summarise for the human

In this order:

1. **What was audited** — the tenant by name, the window (say **UTC**), how many
   conversations, and the phrase matched.
2. **The headline rate with its denominator** — "14 of 16 conversations (88%)", plus
   the turn-level rate if the scan was full.
3. **The severity ladder** — fully abstained / ended on a punt / failed on first
   contact / repeat, with the numbers.
4. **The top unanswered questions**, quoted from the report. This is what makes the
   finding actionable; don't summarise it away.
5. **The config read** — whether the rate is consistent with a corpus gap or a strict
   grounding gate, or that you can't tell from transcripts alone.
6. **What this cannot show** — no scores, no tool trace, no `abstention_reason`; point
   at the replay skill for mechanism.
7. **The report URL as the last line, always, unprompted**, copied verbatim from the
   tool's `REPORT URL:` line.

```
REPORT URL: file:///…/reports/abstention/<tenant>/<label>/report.html
```
</output>
