## Interpreting a replay

**Similarity** is a character-diff ratio between the two answers. It is a reading order,
nothing more. A 90%-alike answer can be wrong in the one number that matters; a 20%-alike
answer can be a rewrite that is strictly better. Never report it as a quality score.

**Flags worth opening first**, roughly in order:

| Flag | Why it matters |
|---|---|
| `answered→refused` | The change made the bot stop helping with something it used to handle. Usually the most damaging regression, and invisible in aggregate metrics. |
| `refused→answered` | Could be the fix you wanted — or a guardrail that stopped holding. Read the answer, not the flag. |
| `ungrounded-answer` | A substantive answer with nothing retrieved. Any specific figure in it is invented. |
| `tool-failed` / `empty-reply` | Only visible in the paired trace; a bare tool list hides a failure. |
| `confirmation-gate` | A write tool wanted to run. Check the arguments table — fabricated identifiers have shown up there. |
| `prod-handoff` | Production escalated to a human. Did testing keep it in-bot, and should it have? |
| `weak-retrieval` | Platform retrieval score below 0.5. |

**The platform's own scores** (`answer_quality`, `retrieval`, `faithfulness`) arrive on the
replay side only. They are the vendor grading itself: a high score means the answer matched
what was retrieved, not that it is correct — the retrieved document can be wrong or
irrelevant. Under `scoring_version: v1-retrieval-only`, `faithfulness_score` is `null` and
`answer_quality_score` is just a copy of `retrieval_score`; do not present them as two
independent measurements.

**Production turns carry no sources, no tool trace and no timings.** The transcript API
does not expose them. That is a limitation of the comparison, not an absence of retrieval
in production — never write "production had no sources".

**Rank findings on what a citizen could act on**: an invented duration, fee or channel; a
contradiction between the two answers where one must be wrong; a refusal that stopped
holding; a fabricated identifier in a gate. Flag contradictions *between* conversations
too — those never show up row by row.

Do not fill in verdicts. Collection and triage is the job; the human grades.

## The HTML report
**The report is paged, not one long scroll.** One HTML file containing:

- a **sidebar index** of every conversation, each with its similarity and top flags, plus
  search and the Changed / Refusal flips / Context diverged / Problems filters;
- an **Overview page** holding the run metadata, the headline stats, the configuration
  diff (differing fields first, identical ones collapsed) and a clickable table of all
  conversations;
- **one page per conversation**, opening with a "Conversation N of M" header carrying the
  title, similarity, char delta, flags and ids, then each turn as its own bordered card
  labelled `Turn n / N` with its own alike badge, the user message full width, and
  production left / testing right.

`j` and `k` step through conversations; prev/next links sit at the foot of each page.

That structure is deliberate. A single scroll made consecutive turns bleed together and
buried where each conversation began — tolerable for three conversations, unusable for a
full day of 130.

To screenshot it, serve it first — playwright-cli blocks `file:`:
`.venv/bin/python -m http.server 8793 --directory <run-dir>`, then kill the server.
