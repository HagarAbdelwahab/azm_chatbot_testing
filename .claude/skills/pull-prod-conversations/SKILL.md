---
name: pull-prod-conversations
description: "Pull real production conversations off a Tarekat Agent Studio tenant for a UTC day or an arbitrary time window, and render them as a readable, PII-masked HTML report — what citizens actually asked and what the live bot answered. Read-only: GETs only, nothing is sent to any agent and no LLM calls are spent. Use for reading production traffic, spot-checking live answers, or freezing an input that `replay-prod-conversations` will later replay."
argument-hint: "[YYYY-MM-DD]"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - AskUserQuestion
---

<objective>
Get real production traffic onto the screen so a human can read it.

Production holds what citizens actually typed — truncated, misspelled, mid-topic, in
dialect, with the context they assumed the bot had. This skill fetches a window of those
conversations off **Tarekat-Restricted** and lays them out for reading: each question,
each live answer, flagged by what is worth opening first.

This is collection and reading, NOT grading and NOT comparison. There is no second column
here — nothing is replayed, no config is being judged. If the question is *"would users
have got better answers after this change?"*, that is `replay-prod-conversations`, which
calls this pull as its Step 1.
</objective>

<safety>
## Read-only by construction

Restricted is **live production**. Real citizens are in there right now.

- The puller uses the admin API's **GET endpoints and nothing else**. It cannot create a
  conversation or send a message; there is no write path in it.
- No agent is invoked, so a pull costs **zero LLM calls** regardless of size. The spend
  warning in `replay-prod-conversations` belongs to the replay step, not to this one —
  do not carry it over and do not talk the user into sampling to "save money". Sample only
  to make the reading shorter.
- New conversations keep appearing in restricted while you work. That is production
  traffic, not your run — this skill never creates any.
</safety>

<pii>
## This is real citizens' data

Pulled text is unredacted **by design**: the pull is the faithful record, and a replay
run later needs the original wording. Masking happens at **render time** — national IDs,
IBANs, phone numbers and emails are replaced in the HTML, and case numbers keep only
their last four digits so a reviewer can still tell threads apart.

`data/prod_pulls/` is force-added to `.gitignore` by the puller on every run. Do not move
pulls out of it, do not paste raw pulled text into chat, a ticket, or a commit message,
and quote from the **report**, not from `pull.json`.

Verify the mask held before handing a report over:

```bash
.venv/bin/python -c "
import re,pathlib
h=pathlib.Path('<run-dir>/report.html').read_text(encoding='utf-8')
print('ids:', len(re.findall(r'(?<!\d)[12]\d{9}(?!\d)', h)))
print('ibans:', len(re.findall(r'SA\d{22}', h)))
print('emails:', len(re.findall(r'[\w.+-]+@[\w-]+\.[\w.]+', h)))"
```

**Never anchor a masking pattern on `\b`.** Arabic letters and digits are both word
characters, so there is no word boundary in `الهوية1001520665` — a `\b`-anchored rule
misses every identifier typed flush against Arabic text, which is exactly how users write
them. The patterns live in `render_replay_html.py` and are imported, not redefined; if you
add one, test it against an identifier with no space before it and check the rendered HTML.
</pii>

<inputs>
## Step 0 — Settle the window. Never assume it.

**Which window?** Either a single UTC day or an explicit range.

- `--day YYYY-MM-DD` for one day. Offer **yesterday** as the default — today is still
  filling up.
- `--start`/`--end` with full ISO `Z` bounds for anything else: several days ("the last
  couple of days"), or a few hours inside one day.

The window is **UTC** and timestamps end in `Z`. Riyadh is UTC+3, so a conversation at
01:00 local belongs to the *previous* UTC day. Say "UTC day" when reporting counts, so
nobody reconciles your number against a local-time dashboard and concludes one of you is
wrong.

Check `ls data/prod_pulls/restricted/` first — a day already pulled does not need pulling
again, and re-pulling refuses to clobber without `--fresh`.

**Then count before fetching**, so any question you ask carries a real number:

```bash
cd "$(git rev-parse --show-toplevel)"   # repo root
.venv/bin/python tools/pull_prod_conversations.py --day <DAY> --count-only
# or a range:
.venv/bin/python tools/pull_prod_conversations.py --start <ISO_Z> --end <ISO_Z> --count-only
```

It prints the total plus a message-count distribution (`message_count` counts **both**
sides — `2:15` means 15 conversations of one user turn and one bot reply). A typical
weekday is ~60–130 conversations.

**Do not ask about sampling by default.** A pull is read-only and free; `--all` is the
right default and gives the user the complete picture. Only raise `--limit` / `--min-turns`
if the user says the report is too long to read, or asks to scope it to one topic.
</inputs>

<pull>
## Step 1 — Pull

```bash
cd "$(git rev-parse --show-toplevel)"   # repo root
.venv/bin/python tools/pull_prod_conversations.py --day <DAY> --all --workers 4
# range:
.venv/bin/python tools/pull_prod_conversations.py --start 2026-09-11T00:00:00Z --end 2026-09-12T23:59:59.999999Z --all --workers 4
```

Writes `data/prod_pulls/restricted/<window>/pull.json` — summaries, full transcripts, the
user turns isolated in order, and a snapshot of the production agent config at pull time.
A `--day` pull is filed under the date; a `--start/--end` pull under a window label like
`2026-09-11T0000_to_2026-09-12T2359Z`, so two different windows on the same date cannot
overwrite each other.

Flags that matter:

| Flag | Use |
|---|---|
| `--all` | every conversation in the window (the default choice) |
| `--limit N` | first N, most recent first |
| `--min-turns N` | skip conversations with fewer than N **user** messages — drops the one-shot "شكرا" threads |
| `--contains "<text>"` | server-side ILIKE over message content; scope the read to one topic |
| `--include-test` | also take playground traffic (off by default, so you get genuine user traffic) |
| `--tenant <name\|uuid>` | another tenant; default `restricted` |
| `--fresh` | overwrite an existing pull |

`--min-turns` prefilters on `message_count` before fetching transcripts, so `--limit 25
--min-turns 2` really does give 25.
</pull>

<report>
## Step 2 — Render and hand over

```bash
.venv/bin/python tools/render_pull_html.py data/prod_pulls/restricted/<window>
# optional: -o /some/other/path.html
```

Takes the pull directory or the `pull.json` itself. Writes `report.html` under
`reports/prod_pulls/<window>/` and prints `REPORT URL:` as its last line. Hand the user
that URL — and the headline counts it prints (conversations, user turns, refusal
conversations and %, handoffs).

Use **this** renderer, not `render_replay_html.py`. That one draws two columns and refuses
without a replay `state.json`; this one reads the pull alone. Masking, refusal detection
and styling are imported from it, so there is exactly one definition of each.

The report is **paged, not one long scroll**: a sidebar index of every conversation with
search and the All / Refusals / Handoff / No reply / Multi-turn filters, an overview page
with run metadata and the production config snapshot, then one page per conversation with
each turn as its own card. `j` and `k` step through conversations.

To screenshot it, serve it first — playwright-cli blocks `file:`:
`.venv/bin/python -m http.server 8793 --directory <run-dir>`, then kill the server.
</report>

<reading>
## Step 3 — What the flags mean, and what they do not

The flags are a **reading order**, not verdicts:

| Flag | Why it matters |
|---|---|
| `refusal` | The bot punted with the canned "لا أملك معلومات كافية…". Who was left with nothing, and could the KB have answered? |
| `handoff` | Escalated to a human. Should it have been? |
| `unanswered` | The user's last turn got no reply at all. |
| `multi` | Multi-turn; where the bot loses the thread is visible only here. |

For a rate across a whole window rather than a read-through, `abstention-audit` is the
purpose-built skill — do not recompute it by hand from this report.

**Production turns carry no sources, no tool trace and no timings.** The transcript API
does not expose them. That is a limitation of what can be shown here, not an absence of
retrieval in production — never write "production had no sources". If grounding matters,
the answer is a replay, where the testing side does return sources and scores.

Rank what you surface on **what a citizen could act on**: an invented duration, fee or
channel; a refusal on something the KB clearly covers; a thread abandoned mid-flow. Flag
contradictions *between* conversations too — the same question answered two ways never
shows up row by row. Then stop: collection and triage is the job, the human grades.
</reading>

<gotchas>
## Hard-won specifics

**Date-only bounds silently return zero.** `start_date=2026-08-31` matches nothing: the
API compares against a timestamp, so an empty result looks like a quiet day rather than a
bad query. Always go through `--day` or full ISO `Z` bounds — `--day` builds them via
`day_bounds()`.

**No tenant selected → 404 on a perfectly valid id.** The studio account is a super-admin
with `tenant_id: null`. Every admin call passes `tenant_id` explicitly for that reason; a
404 usually means wrong tenant, not missing data.

**`sources` is empty even when retrieval worked** (contract change 2026-09-01). The
retrieved text moved to `retrieved_chunks`; `agent_client.py` folds them into
`retrieved_contexts`. Any check reading "sources == [] means ungrounded" is wrong.

**`message_count` counts both sides.** `--min-turns` counts **user** turns. A
`message_count` of 2 is one exchange, which is `--min-turns 1`.
</gotchas>

<related>
- **`replay-prod-conversations`** — the full pipeline: calls this pull as its Step 1, then
  replays the user turns against Tarekat-Testing and renders a side-by-side prod-vs-testing
  diff. Use it when a model or config change needs judging, not when you just want to read.
  A pull made here can be handed straight to `tools/replay_conversations.py <pull.json>`.
- **`abstention-audit`** — read-only abstention/refusal **rate** for a tenant and window,
  plus the KB gaps behind it. Use it for "is the bot effective", not for reading threads.
</related>
