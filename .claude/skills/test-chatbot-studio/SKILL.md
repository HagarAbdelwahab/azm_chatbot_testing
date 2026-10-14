---
name: test-chatbot-studio
description: "Test an agent from inside the Agent Studio admin portal — log in, scope to the right tenant, and drive the agent's Test tab as isolated one-question-per-conversation runs, capturing each answer with its Sources, Quality and Faithfulness scores"
argument-hint: "<agent-id> [questions-file]"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
---

<objective>
Drive the Studio playground (`/dashboard/agents/<id>?tab=test`) instead of the public
widget endpoint, and collect answers a human can review.

Studio shows things the widget API does not: retrieved **Sources** as real filenames,
and per-answer **Quality** / **Faithfulness** scores. That is the reason to test here.

One question per conversation — the playground has no "new conversation" button, so a
page reload is the only way to clear the thread. Skip the reload and every question
inherits the previous answer's context.
</objective>

<not_the_widget>
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
</not_the_widget>

<credentials>
## Step 0 — Credentials

Read from `.env` in the harness repo (gitignored). **Never write them into a file,
a command line, or a report:**

```bash
set -a; . ./.env; set +a       # STUDIO_URL, STUDIO_EMAIL, STUDIO_PASSWORD
```

If they are absent, ask the user rather than guessing. Pass them via `"$STUDIO_EMAIL"`
so the value never appears in the transcript.
</credentials>

<login>
## Step 1 — Log in

`playwright-cli` keeps session state in `./.playwright-cli/`, so **every command must run
from the same working directory**. Run them all from the harness repo root. A command
issued from elsewhere reports `The browser 'studio' is not open` — and inside a script
that failure is silent, producing a full set of empty results.

```bash
cd "$(git rev-parse --show-toplevel)"   # repo root
playwright-cli -s=studio open "$STUDIO_URL"
playwright-cli -s=studio fill e54 "$STUDIO_EMAIL"
playwright-cli -s=studio fill e61 "$STUDIO_PASSWORD"
playwright-cli -s=studio click e66            # "Sign In"
```

Refs shift between builds — re-snapshot if a fill misses.
</login>

<tenant>
## Step 2 — Scope to the tenant that owns the agent

**This is the step that wastes the most time.** The `$STUDIO_EMAIL` account is a
super-admin (role `azm_admin`) with
`tenant_id: null`, so it lands with *no* tenant selected. Until one is chosen, the agent
page renders an empty `<main>` and the API returns **404 for a perfectly valid agent id**.
An empty page here means wrong tenant, not a broken agent.

```bash
playwright-cli -s=studio goto "$STUDIO_URL/dashboard/tenants"
playwright-cli -s=studio click "button:has-text('— select tenant —')"
playwright-cli -s=studio click "button:has-text('Tarekat-Restricted')"
```

Known tenants on this deployment (from `GET /api/v1/tenants`):

| Tenant | id |
|---|---|
| Tarekat-Testing | `b00c3019-36b6-4da6-9ce6-fc1519e04f05` — the one the **widget harness** uses |
| Tarekat-Restricted | `96cf3f00-3084-44c1-8b70-820073c8a516` — owns agent `b7868a92…` ("Support Agent") |
| Insaf-Testing | `3a26e3f9-8f38-4b75-ae01-0012bc74a54f` |
| test_tenant | `0a8dee86-e643-47c1-b98c-14260ff39abb` |

To find which tenant owns an unknown agent, watch the request rather than guessing:

```bash
playwright-cli -s=studio requests | grep "agents/<agent-id>"    # 200 = right tenant, 404 = wrong
```

Confirm the agent loaded before testing — the header strip names the agent, its knowledge
base and its model, e.g. `Agent: Support Agent · Knowledge base: Support Knowledge Base V2
· Model: self_hosted/gemma4`. **Record that line in the report**; it is what the results
are actually about.
</tenant>

<run>
## Step 3 — Run the questions

One question per line in a plain text file, then:

```bash
~/.claude/skills/test-chatbot-studio/run_tests.sh <agent-id> questions.txt out.json
```

Run it **from the directory where the browser was opened** (see <login>).

The driver reloads before each question, fills, verifies the field actually took, sends,
waits, and extracts. Tunables: `STUDIO_WAIT` (reply wait, default 30s),
`STUDIO_HYDRATE` (post-reload wait, default 6s), `STUDIO_SESSION`.

**Do not lower `STUDIO_HYDRATE` below ~6s.** Filling before React hydrates sets the DOM
value while component state stays empty, so Send silently does nothing and the row comes
back blank. The driver verifies and retries once; if you see `input did not take`,
raise the value rather than ignoring it.

The output is `{"meta": {...}, "results": [...]}`. Each record holds `asked`, `question`
(what the thread actually shows), `answer`, `sources`, `tools`, `quality`,
`faithfulness`; `meta` carries the tenant, agent, knowledge base, model and agent id.

The driver renders the report itself as its final step, so no separate command is
needed. To re-render an existing results file:

```bash
python3 ~/.claude/skills/test-chatbot-studio/render_report.py <results.json>
```

It writes **`reports/studio/<agent-id>/report.html`** and prints `REPORT URL:` as its
last line — attach that verbatim, as the last line of the run.

**Never write into `reports/conversations/`.** That belongs to
`chatbot-conversation-run`: its `report.html` would be overwritten, and
`run_conversations.py archive` sweeps every loose file there into a timestamped
widget-run folder, mixing two different systems' results. The per-agent subdirectory
matters too — a shared `reports/studio/report.html` would clobber itself whenever you
switch agents.

**Verify isolation before reporting anything:** `question` must equal `asked` on every
row. If it does not, the reload failed and that answer carries earlier context.

```bash
python3 -c "
import json;d=json.load(open('out.json'))
bad=[r['asked'] for r in d if r.get('question','').strip()!=r.get('asked','').strip()]
print('NOT ISOLATED:',bad) if bad else print(f'{len(d)} rows, all isolated')"
```
</run>

<reading>
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
</reading>

<gotchas>
## Hard-won specifics

- **Session is cwd-bound.** `.playwright-cli/` lives in the working directory. Mixed cwd
  = "browser is not open", silently, mid-script.
- **No tenant selected → 404 + blank page.** Not a bad agent id. See <tenant>.
- **No "new conversation" button.** Reload is the only reset.
- **`handoff/queue/stats` returns 400** on the dashboard regardless of state — pre-existing
  console noise, not caused by the test run. Ignore it; don't report it as a finding.
- **Refs (`e54`, `e61`) are per-snapshot.** Prefer text selectors —
  `input[placeholder='Ask something to test…']`, `button:has-text('Send')` — which have
  been stable across builds. Note the placeholder ends in a Unicode ellipsis `…`, not
  three dots.
- **The "Running as" strip does not exist until the first message is sent.** Reading
  agent / knowledge base / model on a freshly loaded playground returns null no matter
  how long you wait. The driver captures meta after the first answer for this reason.
- **`--raw` returns a JSON-*encoded* string**, so the bytes are `\"agent\":null`, not
  `"agent":null`. Shell pattern matches against the unescaped form silently never fire.
- **Never put the password in a report, a script, or a commit.** Read it from `.env`.
</gotchas>

<output>
## Step 5 — Summarise

1. Which **agent, tenant, knowledge base and model** were tested — copy the header strip.
2. Counts: questions, how many isolated, how many withheld.
3. Findings ranked by severity, quoting the answer and naming the source filenames.
4. Anything not captured, stated plainly.
5. The results file path, as the last line.

Always state that this was the **Studio playground**, which ignores the rollout
percentage — otherwise the results read as production behaviour.
</output>
