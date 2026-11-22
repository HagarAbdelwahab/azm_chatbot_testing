## Step 3 — Export the reviewable artefacts

```bash
.venv/bin/python tools/run_conversations.py export   # transcripts.md + results.csv
.venv/bin/python tools/render_html.py                # report.html
open reports/conversations/report.html
```

Re-run BOTH after every reply round and after `judge` — they read `state.json` and
`judgments.json`, so a stale export silently omits the newest turns and verdicts.
With judgments present the report opens with the review queue on top and adds
`judge: review queue / fail / critical / needs review` filters plus a Judge panel per
bot turn; `results.csv` gains `judge_verdict`, `judge_severity`, `judge_failed_turns`,
`judge_reason`.

**Archive the previous run before starting a new one** — `ask --fresh` overwrites
`state.json`, and the old transcripts are not recoverable:

```bash
.venv/bin/python tools/run_conversations.py archive          # or --label <name>
```

That moves every loose file into `reports/conversations/<YYYY-MM-DD_HHMM>-<label>/`,
stamped from `state.json`'s mtime — when the run finished, not when it was archived.
Date-time folders, never `run1`/`run2`: they sort chronologically and a run can be
matched to the day it was taken without opening it. The command refuses to overwrite
an existing folder.

**Do not name a folder (or the report) after one service.** These runs mix sources —
the `S` scenario scripts test repetition, false memory and language switching, which
belong to no service at all. Naming a whole run `investment-division` because the CSV
happened to be `golden_investment_division.csv` mislabels two thirds of it. With
`--label` omitted the label is derived from the row-id prefixes present
(`Q`→questions, `S`→scenarios, `T`→tickets, `P`→probes), e.g.
`2026-08-24_1436-questions+scenarios`. `render_html.py` derives its header line the
same way. Pass `--label` only when a run genuinely is about one thing.

Outputs in `reports/conversations/`:
- `report.html` — the one to review; chat bubbles, filters, search, tool chips, gates
- `transcripts.md` — same content, plain text, greppable
- `results.csv` — the original sheet with `response` filled; `verdict` left blank
- `state.json` — raw record with every `conversation_id` (needed to resume threads)
- `judgments.json` — the judge's per-turn verdicts, keyed `<row-id>/<bot-turn>`
- `replies_round*.json` — your side of the conversations

## Reading the report panels
The report header also links every other artefact of the run — `transcripts.md`,
`results.csv`, `state.json`, and each `replies_round*.json` — so the URL alone hands
over the whole run, not just the transcripts. `render_html.py` builds that bar by
globbing `reports/conversations/`, so a new round is picked up with no code change;
it prints how many files it linked. If that count looks short, an artefact is missing —
re-run `export` before handing over.

**Every bot turn carries its Sources and Tools-used panels**, mirroring Agent Studio's
Test Chat. Added 2026-08-24: `sources` (the KB filenames the answer was grounded in)
and the full `steps` trace are persisted by `_turn_record`, and `panels()` renders them.
Read them together with the answer — they are what separates a grounded answer from an
invented one:

- **Sources (0) on a factual claim is a finding**, not a formatting detail. A duration,
  fee, or deadline asserted with no retrieved document is invented. Cross-check this
  before ranking any hallucination finding.
- **`✗ failed` on a tool call** is visible only in the paired trace — a bare tool-name
  list makes a failed call look identical to a successful one.
- Turns captured before 2026-08-24 have neither field and render an explicit
  "not captured" note. That payload is NOT recoverable from `state.json`; only a
  re-run fills it. Never present an old turn's missing panel as "no sources retrieved".

Rank a finding on what a citizen could act on: a fabricated identifier, a contradiction
between two KB-grounded answers, an invented duration/fee/channel, or a refusal that
failed to hold. Flag contradictions BETWEEN rows — they do not show up row by row.

Do not fill `verdict` or `reviewer_notes`. Those are the reviewer's.
