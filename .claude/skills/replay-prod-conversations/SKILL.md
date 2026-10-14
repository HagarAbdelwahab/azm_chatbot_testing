---
name: replay-prod-conversations
description: "Pull a day of real production conversations off the Tarekat-Restricted tenant and replay the users' own messages against Tarekat-Testing, so a model or config change can be judged against traffic that actually happened — output is a side-by-side production-vs-testing HTML report"
argument-hint: "[YYYY-MM-DD]"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - AskUserQuestion
---

<objective>
Answer one question: **after this model/config change, would real users have got better
answers?**

Synthetic test sheets cannot answer it. They contain the questions we thought to ask, in
the phrasing we thought to use. Production contains what citizens actually typed —
truncated, misspelled, mid-topic, in dialect, with the context they assumed the bot had.

So: read a day of real conversations off **Tarekat-Restricted** (production), send the
*user's* turns verbatim to **Tarekat-Testing** (the tenant whose config changed), and put
the two answers side by side.

This is collection and diffing, NOT grading. Similarity percentages triage what to read
first; they are not verdicts. The human decides which answer is better.
</objective>

<safety>
## Read production, write only to testing — and prove it

Restricted is **live production**. Real citizens are in there right now.

- **Never send a message to restricted.** The pull is read-only: it uses the admin API's
  GET endpoints and nothing else.
- The replayer refuses `--tenant restricted` outright, and before sending anything it
  opens one throwaway conversation and asks the admin API which tenant can see it. If
  that is not the tenant you named, it aborts having sent nothing.
- **That check exists because the flag does not route the traffic.** `--tenant` selects
  which config to snapshot and how to label the report. Where messages actually land is
  decided solely by `AGENT_API_KEY` in `.env`. Repoint that key and, without the probe,
  a run would write synthetic conversations into production while the report claimed
  "testing".
- New conversations WILL keep appearing in restricted while you work. That is production
  traffic, not your run. To confirm one is yours, look it up by id — a conversation is
  visible only to its owning tenant, so a replay id 404s on restricted.

If the user reports seeing unexpected conversations on restricted, do not reassure them —
check, by resolving the specific ids against both tenants:

```bash
.venv/bin/python -c "
import sys; sys.path.insert(0,'.')
from tools.studio_admin import StudioAdmin, TENANTS
cid='<conversation-id>'
with StudioAdmin() as api:
    for n,t in TENANTS.items():
        try: api.get(f'/conversations/{cid}', tenant_id=t); print('lives in',n)
        except Exception: pass
"
```
</safety>

<pii>
## This is real citizens' data

Pulled text is unredacted **by design** — the replay must send exactly what the user typed
or it is not reproducing production behaviour. Masking happens at render time: national
IDs, IBANs, phone numbers and emails are replaced in the HTML, and case numbers keep only
their last four digits so a reviewer can still tell one thread from another.

`data/prod_pulls/` is force-added to `.gitignore` by the puller on every run. Do not move
pulls out of it, do not paste raw pulled text into chat, a ticket, or a commit message,
and quote from the **report**, not the pull, when writing findings.

**Never anchor a masking pattern on `\b`.** Arabic letters and digits are both word
characters, so there is no word boundary in `الهوية1001520665` — a `\b`-anchored rule
misses every identifier typed flush against Arabic text, which is exactly how users write
them. The patterns in `render_replay_html.py` use digit lookarounds for this reason. If
you add one, test it against an identifier with no space before it, and verify the
rendered HTML rather than trusting the regex:

```bash
.venv/bin/python -c "
import re,pathlib
h=pathlib.Path('<run-dir>/report.html').read_text(encoding='utf-8')
print('unmasked ids:', len(re.findall(r'(?<!\d)[12]\d{9}(?!\d)', h)))"
```
</pii>

<inputs>
## Step 0 — Ask what to pull. Always ask; never assume.

Two things must be settled before anything runs. Use `AskUserQuestion` for both unless the
user already said them.

1. **Which day?** A single UTC date, `YYYY-MM-DD`. If the user gave one in `$ARGUMENTS`,
   use it. Offer yesterday as the default — today is still filling up.

   The window is a **UTC** day and timestamps end in `Z`. Riyadh is UTC+3, so a
   conversation at 01:00 local belongs to the *previous* UTC day. Say "UTC day" when you
   report counts, so nobody reconciles your number against a local-time dashboard and
   concludes one of you is wrong.

2. **All of them, or a sample?** Ask explicitly — this is the cost decision. Run the
   count first so the question carries a real number rather than an abstraction:

   ```bash
   cd "$(git rev-parse --show-toplevel)"   # repo root
   .venv/bin/python tools/pull_prod_conversations.py --day <DAY> --count-only
   ```

   It prints the total plus a message-count distribution. A typical weekday is ~130
   conversations / ~400 user turns; every turn is one LLM call on testing, so a full day
   is a real spend and takes roughly half an hour at `--workers 4`.

   Offer, with the counted numbers filled in:
   - **All** — `--all`. Complete picture; use it when the change is being signed off.
   - **A sample of N** — `--limit N`. 20–30 is enough to see whether a change helped.
   - **Substantive only** — `--min-turns 2`, alone or with `--limit`. Drops the one-shot
     "شكرا" conversations, which are ~28% of a day and diff to nothing interesting.

   Do not silently pick one. A full-day replay the user did not ask for is a bill they
   did not agree to.
</inputs>

<pull>
## Step 1 — Pull production

This step is the **`pull-prod-conversations`** skill. Everything about the pull — the
read-only guarantees, the window/UTC rules, the flags, the PII handling — lives there and
is not duplicated here. Load it if you need any of that detail; the commands below are the
short form for when you already know what you are pulling.

If the user only wants to *read* production and is not comparing anything, stop at that
skill: it has its own single-column renderer and costs no LLM calls. Come back here only
when the question is "would users have got better answers after this change?".

```bash
cd "$(git rev-parse --show-toplevel)"   # repo root
.venv/bin/python tools/pull_prod_conversations.py --day <DAY> --all
# or: --limit 25 --min-turns 2
```

Writes `data/prod_pulls/restricted/<DAY>/pull.json` — summaries, full transcripts, the
user turns isolated in order, and a snapshot of the production agent config. Re-pulling
the same day refuses to overwrite without `--fresh`, because a replay compared against a
*different* pull than it ran on is worse than no comparison.

`--min-turns` prefilters on `message_count` before fetching transcripts, so `--limit 25
--min-turns 2` really does give 25.

Useful extras: `--contains "<text>"` for a server-side content filter (scope a replay to
one topic), `--include-test` to also take playground traffic (off by default, so what you
get is genuine user traffic).
</pull>

<replay>
## Step 2 — Replay onto testing

```bash
.venv/bin/python tools/replay_conversations.py data/prod_pulls/restricted/<DAY>/pull.json --workers 4
```

Each production conversation gets ONE fresh conversation on testing, and its user turns go
out in their original order. Production *answers* are never sent — only what the human
typed — so the testing agent has to reach its own conclusions from the same input.

**The replay is faithful, not adaptive.** Turn 3 is sent even if testing answered turn 2
completely differently. Do not "fix up" a follow-up to suit the new bot: the moment you
rewrite the user's turns you are measuring a different conversation, and a better
transcript would only mean you improvised well. Divergence IS the signal — a follow-up
that has become a non-sequitur is a finding about the config change.

Keep `--workers` at 4; higher rate-limits the widget key. Runs are resumable — results are
keyed by production conversation id and an interrupted run picks up where it stopped. Use
`--fresh` only to deliberately discard.

Output: `reports/replay/restricted-to-testing/<DAY>/` (named for the direction, so nobody
reads it as a replay *on* restricted), holding `state.json` and `meta.json`. `meta.json`
records both configs at run time — without it the run cannot be interpreted later or
compared against the next one.
</replay>

<report>
## Step 3 — Render and hand over

```bash
.venv/bin/python tools/render_replay_html.py reports/replay/restricted-to-testing/<DAY>
```

Writes `report.html` beside the state and prints `REPORT URL:` as its last line. Re-run it
after any further replay — it reads `state.json`, so a stale render silently omits turns.

**The report is paged, not one long scroll.** One HTML file containing:

- a **sidebar index** of every conversation, each with its similarity and top flags, plus
  search and the Changed / Refusal flips / Context diverged / Problems filters;
- an **Overview page** holding the run metadata, the headline stats, the configuration
  diff (differing fields first, identical ones collapsed) and a clickable table of all
  conversations;
- **one page per conversation**, opening with a "Conversation N of M" header carrying the
  title, similarity, char delta, flags and ids, then each turn as its own bordered card
  labelled `Turn n / N` with its own alike badge, the user message full width, and
  production left / testing right.

`j` and `k` step through conversations; prev/next links sit at the foot of each page.

That structure is deliberate. A single scroll made consecutive turns bleed together and
buried where each conversation began — tolerable for three conversations, unusable for a
full day of 130.

To screenshot it, serve it first — playwright-cli blocks `file:`:
`.venv/bin/python -m http.server 8793 --directory <run-dir>`, then kill the server.
</report>

<reading>
## Step 4 — What the signals mean, and what they do not

**Similarity** is a character-diff ratio between the two answers. It is a reading order,
nothing more. A 90%-alike answer can be wrong in the one number that matters; a 20%-alike
answer can be a rewrite that is strictly better. Never report it as a quality score.

**Flags worth opening first**, roughly in order:

| Flag | Why it matters |
|---|---|
| `answered→refused` | The change made the bot stop helping with something it used to handle. Usually the most damaging regression, and invisible in aggregate metrics. |
| `refused→answered` | Could be the fix you wanted — or a guardrail that stopped holding. Read the answer, not the flag. |
| `ungrounded-answer` | A substantive answer with nothing retrieved. Any specific figure in it is invented. |
| `tool-failed` / `empty-reply` | Only visible in the paired trace; a bare tool list hides a failure. |
| `confirmation-gate` | A write tool wanted to run. Check the arguments table — fabricated identifiers have shown up there. |
| `prod-handoff` | Production escalated to a human. Did testing keep it in-bot, and should it have? |
| `weak-retrieval` | Platform retrieval score below 0.5. |

**The platform's own scores** (`answer_quality`, `retrieval`, `faithfulness`) arrive on the
replay side only. They are the vendor grading itself: a high score means the answer matched
what was retrieved, not that it is correct — the retrieved document can be wrong or
irrelevant. Under `scoring_version: v1-retrieval-only`, `faithfulness_score` is `null` and
`answer_quality_score` is just a copy of `retrieval_score`; do not present them as two
independent measurements.

**Production turns carry no sources, no tool trace and no timings.** The transcript API
does not expose them. That is a limitation of the comparison, not an absence of retrieval
in production — never write "production had no sources".

**Rank findings on what a citizen could act on**: an invented duration, fee or channel; a
contradiction between the two answers where one must be wrong; a refusal that stopped
holding; a fabricated identifier in a gate. Flag contradictions *between* conversations
too — those never show up row by row.

Do not fill in verdicts. Collection and triage is the job; the human grades.
</reading>

<gotchas>
## Hard-won specifics — read before interpreting anything

**Date-only bounds silently return zero.** `start_date=2026-08-31` matches nothing: the API
compares against a timestamp, so an empty result looks like a quiet day rather than a bad
query. Always go through `--day`, which builds full ISO bounds via `day_bounds()`.

**No tenant selected → 404 on a perfectly valid id.** The studio account is a super-admin
with `tenant_id: null`. Every admin call here passes `tenant_id` explicitly for that
reason; a 404 usually means wrong tenant, not missing data.

**`sources` is empty even when retrieval worked** (contract change 2026-09-01). The
retrieved text moved to `retrieved_chunks`. Any check reading "sources == [] means
ungrounded" is now wrong — `agent_client.py` folds chunks into `retrieved_contexts`, so
read that.

**`content` is token-streamed now**, many frames per answer. `content_frames > 1` is
normal and no longer signals anything.

**Contract drift aborts trust, not the run.** `unknown_keys` on any turn means the vendor
is sending data being dropped. The replayer prints `!! CONTRACT DRIFT` with the keys. Fix
`agent_client.py` (add to `KNOWN_FRAME_KEYS`, add a `Turn` field, handle in `_extract`,
document the shape and date in the module docstring) and re-run — turns captured before
the fix keep their gap, and it is not recoverable from `state.json`.

**A write tool never executes.** It streams a `confirmation_request` and the turn carries
no text — correct behaviour, not an empty answer. Replying "نعم" just re-issues the gate,
so the approve path is not testable here.

**Tools on testing are mocked.** `resolve_case` returns the same fixture regardless of
input. Heir names and failure reasons in a replayed answer are fixture data — do not file
them as a leak. Confirm whether a tool is wired to a real datastore before assigning
severity to anything depending on its output.

**Rollout percentage applies here.** This is the widget endpoint, the same one the real
site uses, so a behaviour difference may be the rollout setting rather than the model.
(The Studio playground ignores rollout — that is `test-chatbot-studio`, a different
surface. Never mix results from the two without saying which is which.)

**Not the same thing as `chatbot-conversation-run`.** That skill runs an authored CSV and
writes to `reports/conversations/`. This one replays real traffic and writes to
`reports/replay/`. Keep them apart: that skill's `archive` command sweeps loose files in
its own directory, and mixing the two makes both unreviewable.
</gotchas>

<output>
## Step 5 — Summarise for the human

In this order:

1. **What was compared** — the day (say "UTC"), how many production conversations of how
   many available, how many turns; and both configs, named: e.g. *production
   Claude-Sonnet-4.5 @ temp 0.1 → testing Gemini 3.1 Pro @ temp 0.2*. A replay reported
   without its config diff is uninterpretable.
2. **Confirm the direction explicitly** — restricted was read only; every replayed
   conversation was created on testing, verified by probe.
3. **Headline numbers** — mean similarity, count heavily diverged, and the flag tally.
4. **Findings ranked by severity**, each naming the conversation number and quoting the
   two answers. Lead with `answered→refused` and with anything a citizen could act on.
5. **What was not captured or not testable**, plainly: production has no sources/tools;
   `faithfulness_score` is null under v1 scoring; anything that errored or was skipped.
6. **The report URL as the last line, always, unprompted.** Copy the `REPORT URL:` line
   from the renderer verbatim — a path the user has to assemble by hand is a path they do
   not open.

```
REPORT URL: file:///…/reports/replay/restricted-to-testing/<DAY>/report.html
```
</output>
