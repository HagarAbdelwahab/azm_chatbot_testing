---
name: test-chatbot-studio
description: "Test an agent from inside the Agent Studio admin portal — log in, scope to the right tenant, and drive the agent's Test tab as isolated one-question-per-conversation runs, capturing each answer with its Sources, Quality and Faithfulness scores"
argument-hint: "<agent-id> [questions-file]"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
---

Drives the Studio playground (`/dashboard/agents/<id>?tab=test`), not the widget. It shows real source filenames and Quality/Faithfulness scores, but it **ignores the rollout %** and is not saved or billed. Always say so. This is collection, not grading.

## 1. Log in + scope the tenant (one command, from the repo root)
```bash
cd "$(git rev-parse --show-toplevel)"
.claude/skills/test-chatbot-studio/login.sh Tarekat-Testing    # or Tarekat-Restricted, etc.
```
It prints one line. **Never log in by hand with `playwright-cli fill`**: that echoes the email and password into the transcript. The tenant must own the agent; a blank page or a 404 means the wrong tenant (`references/gotchas.md` has the tenant ids).

## 2. Run (same directory: the browser session is cwd-bound)
```bash
.claude/skills/test-chatbot-studio/run_tests.sh <agent-id> questions.txt reports/studio/<agent-id>/results.json
```
One question per line. Each one gets a page reload, so it runs in its own conversation. It prints a total, `DIGEST:` and `REPORT URL:`. `STUDIO_WAIT` (default 30s) and `STUDIO_HYDRATE` (default 6s, never lower) are tunable. Re-render only: `python3 .claude/skills/test-chatbot-studio/render_report.py <results.json>`. Never write into `reports/conversations/`.

## 3. Summarise: read ONLY `digest.md`
`digest.md` is masked and holds the run header, the isolation check and each answer clipped, with its sources and scores. **Do not read the results JSON or `report.html`.** If the digest shows `NOT ISOLATED`, those answers carry earlier context: say so first. Report:
1. Agent, tenant, KB and model (from the digest header), plus "Studio playground, ignores rollout %".
2. Counts: questions, isolated, withheld, answered with 0 sources.
3. Findings ranked by what a citizen could act on, quoting the answer and naming the source filename. A confident answer with 0 sources is the red flag; high faithfulness does not mean the answer is correct.
4. Anything not captured (e.g. a model/KB strip or scores missing).
5. Last line: `REPORT URL:` copied verbatim.

## References (load only when needed)
- `references/interpreting.md`: playground vs widget, what Quality/Faithfulness/Withheld mean.
- `references/gotchas.md`: tenant ids and ownership, cwd-bound sessions, hydration, selectors, `--raw` quirks.
