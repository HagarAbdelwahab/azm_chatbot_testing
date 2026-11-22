## This is NOT the same system as `chatbot-conversation-run`

Do not compare results across the two without saying which is which:

| | Studio Test tab (this skill) | Widget API (`chatbot-conversation-run`) |
|---|---|---|
| Entry point | admin playground | real website widget endpoint |
| Rollout % | **ignored** — always runs the assistant | **applies** |
| Saved to history / billed | no | yes |
| Sources | real filenames (`heirs_listing.md`) | often `tmp*.txt` chunks |
| Scores | Quality + Faithfulness shown | none |

The banner says it outright: *"The playground ignores the rollout percentage and always
runs the assistant, unless the tenant kill switch is on."* So a behaviour difference
between the two may be the rollout percentage, not the model. Check the bot's rollout
setting before calling it a regression.

## Step 4 — What the extra signals mean

- **`Quality` / `Faithfulness`** are the platform's own numbers, not yours. High
  faithfulness means the answer matched its retrieved sources — it does **not** mean the
  answer is correct, because the source itself may be wrong or irrelevant. Treat a
  confident answer with `Sources: 0` as the real red flag.
- **`Withheld: Clarifying question asked`** replaces the answer when the agent asks
  instead of answering, and no scores are emitted. Several in a row on rows that should
  have been answerable is a finding — the same deflection-loop pattern the widget shows.
- **Sources are real filenames here** (`heirs_listing.md`, `cash_investment_division.md`),
  so an invented fact can be traced to a document. Quote the filename in a finding.

Rank findings on what a citizen could act on: an invented duration/fee/channel, a
contradiction between two rows, a refusal that failed to hold. Do not fill in verdicts —
collection is the job; the human grades.
