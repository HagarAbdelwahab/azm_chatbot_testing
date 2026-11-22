## Hard-won specifics

**Date-only bounds silently return zero.** `start_date=2026-09-03` matches nothing —
the API compares against a timestamp, so an empty result looks like a quiet day rather
than a bad query. Always go through `--day` / `--start` / `--last-hours`, which build
full ISO bounds.

**No tenant selected → 404 on a perfectly valid id.** The studio account is a
super-admin with `tenant_id: null`, so every call passes `tenant_id` explicitly. A 404
here means wrong tenant, not missing data.

**The server-side `q=` filter is exact but role-blind.** It is an ILIKE over message
content and *does* match assistant text — verified 2026-09-03 against restricted
09:55–11:55Z, where `q` returned 14 and hand-counting transcripts returned the same 14.
But it cannot distinguish an assistant message from a user one, so a citizen quoting the
phrase back would be a false positive. Every candidate is re-checked on the transcript
with `role == "assistant"` before it counts. Do not "optimise" that check away.

**`--matched-only` costs you the turn-level rate, and the report says so.** In that mode
only matching conversations are fetched, so there is no honest denominator for "X% of all
bot turns" — the tool prints the count with no percentage and the report carries a
caveat box. It does *not* estimate. Conversation-level rates stay exact, because the
denominator comes from the unfiltered count either way.

**Auditing `testing` includes your own traffic.** Replays from
`replay-prod-conversations` and runs from `chatbot-conversation-run` go through the
widget endpoint, so the API classifies them as real (not playground) traffic and they
land in the audit. A 69-conversation "day" on testing may be mostly your own replays.
Either audit `restricted` for genuine user behaviour, or subtract your runs knowingly —
never present a testing-tenant rate as a user-facing metric without saying this.

**A high rate can be baseline, not an incident.** Production has been abstaining at
41% (2026-09-02, full day) and 56–58% (2026-09-03, two-hour windows). Before calling a
number alarming, get a comparison window from a known-good period — one window in
isolation cannot tell a regression from the status quo.

**Not the same thing as the other two chatbot skills.** `replay-prod-conversations`
replays traffic onto a target and diffs answers (writes to `reports/replay/`).
`chatbot-conversation-run` drives an authored CSV (writes to `reports/conversations/`).
This one only reads and counts (writes to `reports/abstention/`). Keep the directories
apart.
