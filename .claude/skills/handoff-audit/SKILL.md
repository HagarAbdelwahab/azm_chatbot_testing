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

Read-only GETs on production, no LLM calls. This is measurement, not grading. **Never call any handoff POST endpoint** (claim, release, resolve, takeover, transfer, escalate): those act on real citizens' tickets.

## 0. Ask the window (UTC; never assume)
`--days N` (last N whole UTC days) | `--day YYYY-MM-DD` | `--start/--end` (full ISO `Z`). Tenant defaults to `restricted`. Every transcript is fetched (handoff state lives only there): about 2 min for a week.

## 1. Run
```bash
cd "$(git rev-parse --show-toplevel)"
.venv/bin/python tools/handoff_audit.py --tenant restricted --days 7 --cross-check
```
Prints a 1-line headline, then `SUMMARY:` and `REPORT URL:`. `--cross-check` is only meaningful with `--days` (the platform stats endpoint is a rolling window counted back from now). Use `--json` only if someone needs `audit.json`.

## 2. Report: read ONLY `summary.md`
`summary.md` is masked. **Do not open `report.html`** (MBs) **or `audit.json`** (raw transcripts). If it has a `WARNINGS` section, state it first. Then:
1. Window (UTC), handoffs of total conversations.
2. Lead with the experience numbers: **expired** and **never answered** (a wider set). Not the medians, which only count answered requests.
3. Wait = time to the first human reply (runs longer than claim latency; never call it "claim time"). Duration is a floor.
4. Platform cross-check as a separate measurement; do not reconcile it.
5. Cannot show: per-agent breakdown, a second handoff in one conversation, past queue state.
6. Last line: `REPORT URL:` copied verbatim.

## References (load only when needed)
- `references/measurement.md`: how each metric is derived and its bias, the resolution vocabulary, cross-check divergence, what is unavailable.
- `references/details.md`: endpoint safety, why every transcript is fetched, report layout, masking check.
