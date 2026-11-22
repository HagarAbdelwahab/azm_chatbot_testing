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

## Tunables and output (run_tests.sh)
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
