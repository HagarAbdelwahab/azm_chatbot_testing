## Hard-won specifics — read before interpreting anything

**Date-only bounds silently return zero.** `start_date=2026-08-31` matches nothing: the API
compares against a timestamp, so an empty result looks like a quiet day rather than a bad
query. Always go through `--day`, which builds full ISO bounds via `day_bounds()`.

**No tenant selected → 404 on a perfectly valid id.** The studio account is a super-admin
with `tenant_id: null`. Every admin call here passes `tenant_id` explicitly for that
reason; a 404 usually means wrong tenant, not missing data.

**`sources` is empty even when retrieval worked** (contract change 2026-09-01). The
retrieved text moved to `retrieved_chunks`. Any check reading "sources == [] means
ungrounded" is now wrong — `agent_client.py` folds chunks into `retrieved_contexts`, so
read that.

**`content` is token-streamed now**, many frames per answer. `content_frames > 1` is
normal and no longer signals anything.

**Contract drift aborts trust, not the run.** `unknown_keys` on any turn means the vendor
is sending data being dropped. The replayer prints `!! CONTRACT DRIFT` with the keys. Fix
`agent_client.py` (add to `KNOWN_FRAME_KEYS`, add a `Turn` field, handle in `_extract`,
document the shape and date in the module docstring) and re-run — turns captured before
the fix keep their gap, and it is not recoverable from `state.json`.

**A write tool never executes.** It streams a `confirmation_request` and the turn carries
no text — correct behaviour, not an empty answer. Replying "نعم" just re-issues the gate,
so the approve path is not testable here.

**Tools on testing are mocked.** `resolve_case` returns the same fixture regardless of
input. Heir names and failure reasons in a replayed answer are fixture data — do not file
them as a leak. Confirm whether a tool is wired to a real datastore before assigning
severity to anything depending on its output.

**Rollout percentage applies here.** This is the widget endpoint, the same one the real
site uses, so a behaviour difference may be the rollout setting rather than the model.
(The Studio playground ignores rollout — that is `test-chatbot-studio`, a different
surface. Never mix results from the two without saying which is which.)

**Not the same thing as `chatbot-conversation-run`.** That skill runs an authored CSV and
writes to `reports/conversations/`. This one replays real traffic and writes to
`reports/replay/`. Keep them apart: that skill's `archive` command sweeps loose files in
its own directory, and mixing the two makes both unreviewable.
