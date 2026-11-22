## What the numbers mean

The admin API exposes exactly **two** handoff fields per conversation —
`handoff_requested_at` and `handoff_resolution_type`. There is no `claimed_at`, no
`resolved_at`, no per-conversation duration (verified against the live openapi.json,
2026-09-13). So three of the four metrics are **derived**, and each derivation has a
known bias:

**Wait = time to first human reply, not claim latency.** Handoff transcripts carry a third
role besides `user` and `assistant`: **`agent`** — a human ops message. The first `agent`
message at or after the request is the earliest *observable* moment a human engaged. An
agent who claims a ticket and reads for 30s before typing appears here as a 30s-longer
wait, so this proxy runs **longer** than a true claim latency. (Observed: derived mean
201s vs the platform's own `avg_wait_seconds` 147.9s over the same days — the gap is the
bias, in the expected direction.) Never call it "claim time".

**Duration = request → last ops message.** An agent who resolves without a parting message
shortens it. It is a floor, not exact handling time.

**Expiry is NOT derived — the platform states it.** `handoff_resolution_type == "timeout"`
IS the request that aged out. The observed vocabulary on restricted:

| value | meaning |
|---|---|
| `close` | ops resolved it |
| `timeout` | **EXPIRED — nobody came** |
| `return_to_ai` | handed back to the bot |
| `abandoned` | the user left |
| `null` | still open at audit time |

The tool checks that `timeout` conversations really have no ops reply and **warns** if
that ever stops holding. If you see that warning, re-read the threads before quoting any
expiry number — the label's meaning has changed under you.

**"Never answered" is a wider set than "expired."** A request can go unanswered and end up
labelled `abandoned` or left `null`. Report both; they are different failures.

**There is no honest "how long did they wait before giving up".** For an unanswered
request the transcript just ends — nothing records how long the citizen sat with the
widget open. The gap to the last message is reported only where it is *positive*, which
it often is not: the handoff request is frequently written after the final bot message,
which yields a small negative number. Those are counted, not timed, and the row's `n` is
smaller than the never-answered count for exactly this reason. Do not clamp a negative to
zero and present it as a measurement.

## Step 3 — The platform's own analytics, and why it will not match

`GET /analytics/handoff/stats` returns `total_handoffs`, `avg_wait_seconds`,
`avg_resolution_seconds`, `resolution_rate`, `return_to_ai_rate` — the vendor's own
figures, from a claim event this tool cannot see.

It takes a rolling **`days=N` counted back from now**, so it **cannot be scoped to a named
day**. Asking it about last Thursday is not possible; `--cross-check` is only meaningful
when the window is the last N days.

Expect divergence, and present it as two different measurements rather than reconciling
it. Observed over the same four days: platform `total_handoffs` 35 vs 30 counted here —
different window edges, since the rolling window starts mid-day.

## What this cannot tell you — say so rather than improvising

- **Which ops agent handled what.** `agent_name` is populated only while a handoff is
  still ACTIVE and is null on every resolved conversation, so a per-agent breakdown or
  leaderboard cannot be reconstructed historically. The tool does not offer one; do not
  build one from the active-queue endpoint and present it as history.
- **A second handoff in the same conversation.** `handoff_requested_at` is one timestamp
  and keeps only the latest. Conversations whose only ops messages predate it are counted
  as never-answered and tagged `earlier-cycle`.
- **Who was waiting at a past moment.** `/handoff/queue` and `/handoff/active` are live
  state only.
