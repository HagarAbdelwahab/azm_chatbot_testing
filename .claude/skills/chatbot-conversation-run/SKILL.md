---
name: chatbot-conversation-run
description: "Run a question CSV through the Merath/Tarekat chatbot as isolated multi-turn conversations — one thread per question, reply wherever the bot needs user input, then export transcripts and a reviewable HTML report"
argument-hint: "<path-to-questions.csv>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
---

Transcript collection, not grading. Never fill `verdict` / `reviewer_notes`; those are the reviewer's.

## 0. Setup
- CSV_PATH from $ARGUMENTS, or ask. Only `question` is required (`id`, `expected_behavior`, `approved_answer` recommended). If the CSV is outside the repo, `cp` it into `data/`.
- `cd "$(git rev-parse --show-toplevel)"`. If `reports/conversations/state.json` exists from an earlier run, run `.venv/bin/python tools/run_conversations.py archive` first.
- **Use `--ops` on every send command.** It runs through the Tarekat ops portal widget on testing. The `.env` `AGENT_API_KEY` bot is unconfigured.

## 1. Send
```bash
.venv/bin/python tools/run_conversations.py --ops ask <CSV_PATH> --workers 4            # one isolated conversation per row
.venv/bin/python tools/run_conversations.py --ops scenario data/conversation_scenarios.yaml --workers 4   # optional scripted multi-turn
```
Output is quiet: only failures and a total line. Keep `--workers` at 4.

## 2. Reply rounds: read ONLY `pending.md`
```bash
.venv/bin/python tools/run_conversations.py pending        # -> reports/conversations/pending.md: only the threads that need a reply
```
- Fix one fact sheet (request number, service, situation) and reuse it in every reply.
- Write `reports/conversations/replies_roundN.json` (`{"Q03": "text"}`, or a list for several turns) for the ids listed, matching each question's dialect.
- Then: `.venv/bin/python tools/run_conversations.py --ops reply reports/conversations/replies_roundN.json --workers 4` and `.venv/bin/python tools/run_conversations.py pending` again. Repeat until it reports 0.
- **Never invent a national ID, IBAN or authorization.** If a thread only advances that way, stop it and say so.
- Categories and the full reply table: `references/replies.md`.

## 3. Grade (optional), export, render, verify
```bash
.venv/bin/python tools/run_conversations.py judge          # needs ANTHROPIC_API_KEY; prints the review queue only
.venv/bin/python tools/run_conversations.py export         # transcripts.md, results.csv, turns_to_label.csv + digest.md
.venv/bin/python tools/render_html.py
.venv/bin/python tools/run_conversations.py verify         # prints holes only; report every one plainly
```
Re-run `export` and `render_html.py` after every reply round and after `judge`.

## 4. Summarise: read ONLY `digest.md` (+ `verify` output)
**Do not read `transcripts.md`, `state.json` or `report.html`.** Open a single conversation only to confirm a specific finding. Report:
1. Conversations, bot turns, reply rounds, and the judge split (or "judge not run").
2. Findings ranked by what a citizen could act on: a fabricated identifier in a write gate, an invented fee/duration/channel (substantive answer with nothing retrieved), a contradiction, a refusal that didn't hold. Quote from the digest.
3. Holes and anything not testable (write gates never execute; tools are mocked).
4. Last line: `REPORT URL:` copied verbatim from `render_html.py`.

## References (load only when needed)
- `references/replies.md`: fact sheet and reply table.
- `references/scenarios.md`: the scripted messiness patterns that must stay covered.
- `references/judge.md`: judge dimensions, cost, calibration.
- `references/report.md`: artefacts, archive naming, Sources/Tools panels.
- `references/gotchas.md`: mocked tools, write gates, contract drift (`unknown_keys`).
