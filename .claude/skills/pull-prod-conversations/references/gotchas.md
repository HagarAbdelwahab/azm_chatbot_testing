## Hard-won specifics

**Date-only bounds silently return zero.** `start_date=2026-08-31` matches nothing: the
API compares against a timestamp, so an empty result looks like a quiet day rather than a
bad query. Always go through `--day` or full ISO `Z` bounds — `--day` builds them via
`day_bounds()`.

**No tenant selected → 404 on a perfectly valid id.** The studio account is a super-admin
with `tenant_id: null`. Every admin call passes `tenant_id` explicitly for that reason; a
404 usually means wrong tenant, not missing data.

**`sources` is empty even when retrieval worked** (contract change 2026-09-01). The
retrieved text moved to `retrieved_chunks`; `agent_client.py` folds them into
`retrieved_contexts`. Any check reading "sources == [] means ungrounded" is wrong.

**`message_count` counts both sides.** `--min-turns` counts **user** turns. A
`message_count` of 2 is one exchange, which is `--min-turns 1`.

## Pull flags
| Flag | Use |
|---|---|
| `--all` | every conversation in the window (the default choice) |
| `--limit N` | first N, most recent first |
| `--min-turns N` | skip conversations with fewer than N **user** messages — drops the one-shot "شكرا" threads |
| `--contains "<text>"` | server-side ILIKE over message content; scope the read to one topic |
| `--include-test` | also take playground traffic (off by default, so you get genuine user traffic) |
| `--tenant <name\|uuid>` | another tenant; default `restricted` |
| `--fresh` | overwrite an existing pull |

`--min-turns` prefilters on `message_count` before fetching transcripts, so `--limit 25
--min-turns 2` really does give 25.
