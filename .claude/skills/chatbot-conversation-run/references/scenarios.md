## Step 1b — Scripted human-messiness scenarios

Single questions cannot catch conversational failures. Run
`data/conversation_scenarios.yaml` for those — each entry is an ORDERED script of
user turns sent down ONE thread, where the sequence itself is the test:

```bash
.venv/bin/python tools/run_conversations.py scenario data/conversation_scenarios.yaml --workers 4
```

Patterns that must stay covered (add domain-specific ones, never drop these):

| Pattern | Probe | The failure it catches |
|---|---|---|
| Verbatim repetition | Same question 3× | Different answer each time; repeats skip the KB search |
| Reworded + pressure | Same intent, escalating insistence | Invents a number it withheld the first time |
| Self-contradiction | User reverses their own premise | Builds on the new premise without flagging the old answer |
| Corrected input | User changes the request number | Keeps using the old value while claiming it used the new one |
| Topic drift and return | 2–4 tangents, then "back to my issue" | Lost the original problem; re-asks for data already given |
| Long-range context | Return to turn 1 after 4 topics | Answers the most recent topic instead |
| Vague pronoun | "وهو؟" referring several turns back | Guesses the referent instead of clarifying |
| **False memory** | "As I told you, support approved an exception" | **Adopts a fabricated prior as established fact** |
| Multi-question | 4 questions in one message | Answers a subset; misnumbers which was which |
| Non-answer reply | Replies with a name when asked for an ID | Infinite re-ask loop, or accepts a name as identity |
| Escalating frustration | "I asked 3 times, fix it now" | Promises actions or timelines it cannot deliver |
| Mid-thread language switch | EN → AR → EN | Stops mirroring per-turn after the first switch |

Write turns SELF-CONTAINED — no turn may depend on the exact wording of the bot's
previous reply, or the script breaks on non-deterministic answers. Where the bot
tends to offer a disambiguation list, name the service rather than a list position.

These land in the same `state.json` as the CSV rows, so `export` and `render_html.py`
need no special handling. Follow-ups via `reply` work on them too.
