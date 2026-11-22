## Interpreting the numbers

**The denominator is the finding.** "14 conversations abstained" is meaningless;
"14 of 16" is an incident. The tool always fetches the unfiltered window count and
divides against it. Never report a numerator alone.

The severity ladder, worst first — these are the categories worth leading with:

| Metric | Why it matters |
|---|---|
| **Fully abstained** | Every bot turn was a punt. The citizen got *nothing at all* from the conversation. The most damning category; open these first. |
| **Ended on a punt** | Whatever else happened, the bot's last word was "I don't know" — the user left without an answer. |
| **Failed on first contact** | It gave up before the conversation had any chance to narrow, so clarification never even ran. |
| **Abstained ≥2 times** | The user re-asked, rephrased, and got the same wall. Reads as the worst experience of the lot. |
| **Escalated to a human** | Cross-tab against abstention: did the ones the bot failed actually reach a person, or just leave? |

**"Questions the bot could not answer"** is the actionable output — the user turn
immediately preceding each abstention, most frequent first. That table is the
knowledge-base gap list, and it is the thing to hand to whoever owns the corpus.

**An abstention is not automatically the model's fault.** The report includes the
agent config at audit time for exactly this reason. Read the rate against:
- `knowledge_base_ids` — is the topic even in this tenant's corpus?
- `min_score_to_answer`, `faithfulness_threshold`, `strict_grounding` — a high rate
  can be a strict gate on good retrieval rather than a missing document.

Both stories look identical in the rate alone. Say which one the config supports, or
say you can't tell from here.

**What this tool cannot see.** The transcript API returns role, content and timestamp
only — no retrieval scores, no `retrieved_chunks`, no tool trace, no
`abstention_reason`. So it can tell you *that* the bot punted and how often, never
*why*. For the mechanism you need the live SSE frames: run the question through
`agent_client.py`, or use `replay-prod-conversations`, which captures
`abstention_reason`, `retrieval_score` and `retrieved_chunks` on the replay side.

A worked example of that pairing: on 2026-09-03 every `below_faithfulness` abstention
on testing had `retrieved_chunks: 0` while `retrieval_score` read 0.77–1.00 — nothing
retrieved, despite a score that looked excellent. This audit would have shown the rate;
only the frame-level capture showed the cause.
