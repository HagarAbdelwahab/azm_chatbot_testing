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

Read production (GETs only), write only to testing. Collection and diffing, not grading: the human decides which answer is better.

## 0. Ask day + scope (AskUserQuestion; never assume)
Count first:
```bash
cd "$(git rev-parse --show-toplevel)"
.venv/bin/python tools/pull_prod_conversations.py --day <DAY> --count-only
```
- Day: a UTC `YYYY-MM-DD` (default: yesterday). Say "UTC day" in every count.
- Scope (this is the cost; every user turn is one LLM call, and a full day is ~400 turns / ~30 min): `--all` | `--limit N` (20–30 is enough) | `--min-turns 2` (drops one-shot "شكرا" conversations).

## 1. Pull → 2. Replay → 3. Render
```bash
.venv/bin/python tools/pull_prod_conversations.py --day <DAY> --limit 25 --min-turns 2
# Default target: the "Tarekat ops portal widget" bot, replayed as a signed-in heir (agentic tools run).
# It logs in with OPS_USERNAME/OPS_PASSWORD and reads the widget key from the portal page.
.venv/bin/python tools/replay_ops_user.py data/prod_pulls/restricted/<DAY>/pull.json --rules
.venv/bin/python tools/render_replay_html.py reports/replay/ops-user/<label>   # path from its NEXT: line
```
- `--rules` swaps identifiers by pattern without a Claude API key (names are NOT swapped). Drop it only if ANTHROPIC_API_KEY is set.
- Anonymous alternative: `tools/replay_conversations.py <pull.json> --workers 4`. It goes to whichever bot `AGENT_API_KEY` in `.env` belongs to, and its config snapshot is only the tenant's default bot, so it is unverified.
- Each script prints a short status block; follow its `NEXT:` line.
- Stop and report if you see `!! FAILED TURNS`, `!! CONTRACT DRIFT` or `ABORTED`. See `references/gotchas.md`.
- Replays are resumable. `--fresh` discards earlier results; `--workers` max is 4.
- Never edit the user's turns: divergence is the signal.

## 4. Summarise: read ONLY `digest.md`
The renderer prints `DIGEST:`. `digest.md` is masked and ranked by severity. **Do not open `pull.json`, `state.json`** (raw, unmasked PII) **or `report.html`** (large). Report in this order:
1. Day (UTC), conversations / turns, and the config diff (prod → testing) from the digest.
2. Direction: restricted was only read; the replay went to testing, verified by probe.
3. Mean similarity, count heavily diverged, flag tally. Similarity is a reading order, not a score.
4. Findings ranked by severity: conversation #, with both answers quoted from the digest. Lead with `answered→refused` and anything a citizen could act on (invented fee, duration or channel; a contradiction).
5. Not captured: production has no sources, tools or timings; `faithfulness_score` is null under v1 scoring; errors.
6. Last line: `REPORT URL:` copied verbatim.

## References (load only when needed)
- `references/interpreting.md`: flag meanings, platform scores, report layout.
- `references/gotchas.md`: empty `sources`, mocked tools, write-gates, rollout, contract drift.
- `references/safety.md`: why the probe exists; checking a stray conversation's tenant.
- `references/pii.md`: masking rules and the verification snippet.
- Pull details: the `pull-prod-conversations` skill.
