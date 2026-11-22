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

Read-only (admin GETs only, zero LLM calls). This is for reading, not grading or comparing; for before/after comparison use `replay-prod-conversations`, and for rates use `abstention-audit`.

## 0. Settle the window (UTC; never assume)
- `--day YYYY-MM-DD` (default: yesterday) or `--start/--end` with full ISO `Z` bounds. Say "UTC" in every count.
- Check `ls data/prod_pulls/restricted/` first: a day that is already pulled needs no re-pull (re-pulling needs `--fresh`).
```bash
cd "$(git rev-parse --show-toplevel)"
.venv/bin/python tools/pull_prod_conversations.py --day <DAY> --count-only
```
A pull is free, so default to `--all`. Offer `--limit`, `--min-turns N` (counts USER turns) or `--contains "<text>"` only if the user wants it shorter or scoped to one topic.

## 1. Pull → 2. Render
```bash
.venv/bin/python tools/pull_prod_conversations.py --day <DAY> --all --workers 4
.venv/bin/python tools/render_pull_html.py data/prod_pulls/restricted/<window>
```
The renderer prints its counts, then `DIGEST:` and `REPORT URL:`. Use this renderer, not `render_replay_html.py`.

## 3. Summarise: read ONLY `digest.md`
`digest.md` is masked and holds the flagged conversations ranked (refusal > no-reply > handoff > left-hanging) plus repeated opening questions with their answers side by side. **Do not open `pull.json`** (raw, unmasked) **or `report.html`** (~4 KB per conversation). Report:
1. Window (UTC), conversations, user turns, refusal conversations and %, handoffs.
2. What a citizen could act on, quoted from the digest: an invented fee, duration or channel; a refusal on something the KB covers; a thread abandoned mid-flow.
3. Contradictions between conversations (the repeated-questions section).
4. Limits: production turns carry no sources, tools or timings. Never write "production had no sources".
5. Last line: `REPORT URL:` copied verbatim.

Flags are a reading order, not verdicts; the human grades.

## References (load only when needed)
- `references/interpreting.md`: flag meanings and report layout.
- `references/gotchas.md`: date-only bounds return 0, 404 = wrong tenant, `message_count` counts both sides, full flag table.
- `references/pii.md`: masking rules and the verification snippet, before sharing anything.
