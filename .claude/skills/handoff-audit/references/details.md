## Safety: read-only, and which endpoints never to call
Restricted is **live production**. The audit is read-only: it calls
`GET /conversation-summaries`, `GET /conversation-summaries/{id}/transcript` and
optionally `GET /analytics/handoff/stats`, and nothing else. No LLM calls, so size is a
time cost, never a bill.

**The handoff API is mostly writes — never call them.** The spec also exposes
`POST /handoff/queue/{id}/claim`, `/release`, `/resolve`, `/takeover`, `/transfer`,
`/conversations/{id}/escalate` and `/handoff/request`. Firing any of those on restricted
would claim, close or reroute a real citizen's live ticket. The audit tool has no write
path; do not add one, and do not "just try" a claim endpoint to see what it returns.

`GET /handoff/queue` and `GET /handoff/active` are safe but describe **right now**, not
history — they list who is waiting at this instant. They cannot answer a question about
last Thursday.

## Why every transcript is fetched
**On cost:** every transcript in the window must be fetched, not just the escalated ones.
The summaries list carries no handoff field, and `status` cannot stand in for it — on
observed production data 15 of 26 handoff conversations reported `status: bot`, because a
handoff that returns to the bot ends up back in that state. Prefiltering on either one
silently reports **zero handoffs**. The fetch is free but not instant: ~330 transcripts
takes a couple of minutes at `--workers 6`.

## The HTML report
**The report is paged, not one long scroll** — the same shell as the pull and replay
reports, and it imports its stylesheet from `render_replay_html` rather than restating
one, so the three stay one family. A sidebar lists every handoff conversation with its
wait and flags, plus search and the All / Expired / No reply / Slow (>5m) / Resolved
filters; the Overview page carries the headline cards, the resolution breakdown, the
service-level table, the cross-check and a clickable index; then **one page per
conversation**, opening with its timings and showing the full thread with the citizen,
the bot and the human ops turns distinguished, and a marker on the line where the
handoff was requested. `j` and `k` step through conversations.

To screenshot it, serve it first — playwright-cli blocks `file:`:
`.venv/bin/python -m http.server 8794 --directory <run-dir>`, then kill the server.

## Masking check
The report masks IDs, IBANs, phones and emails at render time (patterns imported from
`render_replay_html`, never redefined). Verify before sharing:

```bash
.venv/bin/python -c "
import re,pathlib
h=pathlib.Path('<run-dir>/report.html').read_text(encoding='utf-8')
print('ids:',len(re.findall(r'(?<!\d)[12]\d{9}(?!\d)',h)),
      'ibans:',len(re.findall(r'SA\d{22}',h)))"
```

Quote from the report, not from `audit.json`, and never paste raw transcript text into a
ticket or commit message.
