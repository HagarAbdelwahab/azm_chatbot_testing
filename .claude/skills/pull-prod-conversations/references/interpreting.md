## Reading a pull

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

## The HTML report
The report is **paged, not one long scroll**: a sidebar index of every conversation with
search and the All / Refusals / Handoff / No reply / Multi-turn filters, an overview page
with run metadata and the production config snapshot, then one page per conversation with
each turn as its own card. `j` and `k` step through conversations.

To screenshot it, serve it first — playwright-cli blocks `file:`:
`.venv/bin/python -m http.server 8793 --directory <run-dir>`, then kill the server.
