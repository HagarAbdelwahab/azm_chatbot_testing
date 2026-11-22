## Hard-won specifics — read before interpreting anything

**Tools are mocked.** Every confirmation gate shows
`target: "Tarekat Support (Mock Studio) / …"`. `resolve_case` returns the same fixture
regardless of input — case `DIV-2026-88421`, heirs سارة/محمد/خالد/نورة/فهد. Heir names
and transfer-failure reasons in a transcript are FIXTURE DATA. Do NOT file them as a
data leak. Confirm whether a tool is wired to a real datastore before assigning severity
to any finding that depends on its output.

**The real finding in that area** is that no identity check gates the retrieval, and
that the agent fabricates arguments for write actions — `heir_national_id` has come back
as `"خالد"` (a name), `"10XXXXXXXX"` (a literal placeholder), and `"1020304050"`. Check
the gate arguments table on every gate.

**A write tool never executes.** It streams a `confirmation_request` frame and the turn
carries NO content text — that is correct behaviour, not an empty response. Approval goes
through `pending_action_id` on a separate call the widget makes. Replying "نعم" in chat
just re-issues a fresh gate forever, so the approve path is NOT testable here.

**`abstention_reason: awaiting_confirmation`** marks those turns. `content_frames` is 0.

**The semantic judge is off** — `.env` has no `ANTHROPIC_API_KEY`, so `judge.py` and
`pytest -m tickets` skip. This skill is human review by design; do not try to auto-grade.

**This CSV shape is not `import_tickets.py`'s shape.** That importer needs an approved
`answer` column to grade against. A sheet carrying `expected_behavior` + `risk` and blank
`response`/`verdict` columns belongs to THIS skill instead.

**Screenshotting the report:** playwright-cli blocks the `file:` protocol. Serve it first:
`.venv/bin/python -m http.server 8791 --directory reports/conversations`, visit
`http://localhost:8791/report.html`, then kill the server.

## When `unknown_keys` is non-empty

`Turn.unknown_keys` is the drift detector — it collects any SSE frame key the client does
not consume. A non-empty value means the vendor is sending data that is being dropped.

Fix it, do not ignore it:
1. Dump the raw frames to see the shape — `agent_client.probe("<question>")`, or stream
   manually with `c._c.stream(...)` and print each `data:` line.
2. Add the key to `KNOWN_FRAME_KEYS`, add a field to `Turn`, handle it in `_extract`.
3. Document the frame shape in the `agent_client.py` module docstring with the date.
4. Surface it in `tools/run_conversations.py` (`_turn_record` + markdown) and in
   `tools/render_html.py` so it reaches the reviewer.

Turns captured BEFORE the fix keep their gap — the payload is not recoverable from
`state.json`. Re-trigger it in-thread to capture a fresh one, and label the old turns
honestly in the report rather than leaving them looking blank.
