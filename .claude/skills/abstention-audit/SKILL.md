---
name: abstention-audit
description: Audit how often a tenant's chatbot punts with the canned "لا أملك معلومات كافية للإجابة على ذلك." instead of answering — pulls every conversation in a time window off any Agent Studio tenant, finds the ones holding at least one such bot reply, and reports the abstention rate, who was left with nothing, and which questions the knowledge base cannot answer. Read-only. Use for "is the bot actually effective", abstention/refusal rate, KB gap analysis.
---

Read-only (admin-API GETs only; no LLM calls), safe on production. Measurement, not grading.

## 0. Ask tenant + window (no defaults)
- Tenant: `restricted` | `testing` | `insaf` | `test` | uuid.
- Window (UTC): `--day YYYY-MM-DD` | `--start ISO --end ISO` | `--last-hours N`. Never date-only `--start`.

```bash
cd "$(git rev-parse --show-toplevel)"
.venv/bin/python tools/abstention_audit.py --tenant <T> --day <DAY> --count-only
```

## 1. Run
```bash
.venv/bin/python tools/abstention_audit.py --tenant <T> --day <DAY>
```
Prints 3 lines: the headline rate, `SUMMARY: <path>` and `REPORT URL: <url>`. Add `--fresh` to overwrite. Flags: `--phrase`, `--matched-only` (no turn-level rate), `--limit N`, `--include-test`.

## 2. Report: read ONLY `summary.md`
`summary.md` is masked and complete. **Do not open `audit.json`** (raw, unmasked PII) **or `report.html`** (large). Summarise in this order:
1. Tenant, window **(UTC)**, conversation count, phrase.
2. Rate with its denominator ("68 of 128 (53%)"), plus the turn rate when present.
3. Fully abstained / ended on a punt / first contact / repeat.
4. The top unanswered questions, quoted from `summary.md`.
5. Config read: corpus gap vs a strict grounding gate (`min_score_to_answer`, `faithfulness_threshold`, `strict_grounding`), or "can't tell".
6. Limits: no scores, tool trace or `abstention_reason`. Point to `replay-prod-conversations` for the mechanism.
7. Last line: `REPORT URL:` copied verbatim.

## References (load only when needed)
- `references/interpreting.md` — severity ladder, config-vs-corpus reasoning, baseline rates.
- `references/gotchas.md` — role-blind `q=`, 404 = wrong tenant, testing includes own replays, matched-only caveat.
- `references/pii.md` — before sharing anything outside QA (request numbers are not masked).
