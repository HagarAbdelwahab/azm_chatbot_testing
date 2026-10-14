"""Pull one day of PRODUCTION conversations off the Tarekat-Restricted tenant.

    .venv/bin/python tools/pull_prod_conversations.py --day 2026-08-31 --all
    .venv/bin/python tools/pull_prod_conversations.py --day 2026-08-31 --limit 25 --min-turns 2

Output: data/prod_pulls/<tenant>/<day>/pull.json — the frozen input for a replay.
Re-pulling the same day refuses to clobber an existing pull unless --fresh is given,
because a replay compared against a *different* pull than it ran on is worse than no
comparison at all.

This reads real citizens' messages. Nothing here redacts: the pull is the faithful
record, and the replay needs the original text to reproduce production behaviour.
Redaction happens at render time. Keep this file out of git — data/prod_pulls/ is
gitignored by this script's own check.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.studio_admin import StudioAdmin, day_bounds, resolve_tenant, TENANTS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PULL_ROOT = ROOT / "data" / "prod_pulls"


def tenant_label(tid: str) -> str:
    for name, val in TENANTS.items():
        if val == tid:
            return name
    return tid[:8]


def ensure_gitignored() -> None:
    """Production transcripts must never be committable. Enforce it, don't assume it."""
    gi = ROOT / ".gitignore"
    line = "data/prod_pulls/"
    body = gi.read_text(encoding="utf-8") if gi.exists() else ""
    if line not in body:
        gi.write_text(body.rstrip("\n") + f"\n\n# Real production transcripts — never commit\n{line}\n",
                      encoding="utf-8")
        print(f"  added {line} to .gitignore")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--day", help="UTC day to pull, YYYY-MM-DD")
    p.add_argument("--start", help="explicit ISO lower bound (overrides --day)")
    p.add_argument("--end", help="explicit ISO upper bound (overrides --day)")
    p.add_argument("--tenant", default="restricted", help="name or uuid (default: restricted)")
    p.add_argument("--all", action="store_true", help="take every conversation in the window")
    p.add_argument("--limit", type=int, help="take only the first N (most recent first)")
    p.add_argument("--min-turns", type=int, default=1,
                   help="skip conversations with fewer than N user messages (default 1)")
    p.add_argument("--contains", help="server-side ILIKE filter over message content")
    p.add_argument("--include-test", action="store_true",
                   help="also include playground traffic (default: production only)")
    p.add_argument("--fresh", action="store_true", help="overwrite an existing pull")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--count-only", action="store_true",
                   help="report how many conversations the window holds, fetch nothing")
    args = p.parse_args()

    if not args.day and not (args.start and args.end):
        p.error("give --day YYYY-MM-DD, or both --start and --end")
    if not args.all and not args.limit and not args.count_only:
        p.error("say how much to take: --all, or --limit N")

    tid = resolve_tenant(args.tenant)
    start, end = (args.start, args.end) if args.start else day_bounds(args.day)
    # A windowed pull must carry its TIMES in the label. Dating it by day alone makes
    # every window on one day collide on the same directory, so a second window looks
    # like a re-pull and --fresh silently destroys the first one's frozen input.
    if args.day:
        label = args.day
    else:
        label = (f"{start[:10]}T{start[11:16].replace(':', '')}"
                 f"-{end[11:16].replace(':', '')}Z" if start[:10] == end[:10]
                 else f"{start[:10]}T{start[11:16].replace(':', '')}"
                      f"_to_{end[:10]}T{end[11:16].replace(':', '')}Z")

    out_dir = PULL_ROOT / tenant_label(tid) / label
    out_file = out_dir / "pull.json"
    if out_file.exists() and not args.fresh and not args.count_only:
        sys.exit(f"{out_file} already exists — pass --fresh to overwrite it, or replay the "
                 f"existing pull as-is.")

    print(f"tenant   {tenant_label(tid)} ({tid})")
    print(f"window   {start}  ..  {end}   [UTC]")

    with StudioAdmin() as api:
        # Always list the whole window first — summaries are cheap and carry
        # `message_count`, which lets --min-turns prefilter BEFORE the expensive
        # per-conversation transcript fetches. Capping the list instead would make
        # `--limit 25 --min-turns 2` quietly return fewer than 25.
        summaries = api.list_conversations(
            tid, start=start, end=end, include_test=args.include_test, q=args.contains)

        print(f"found    {len(summaries)} conversations "
              f"({'production only' if not args.include_test else 'including playground'})")
        if args.count_only:
            hist = Counter(s.get("message_count", 0) for s in summaries)
            print("message-count distribution:",
                  ", ".join(f"{k}:{v}" for k, v in sorted(hist.items())[:15]))
            return

        # A conversation needs at least one assistant reply per user turn, so
        # message_count < 2*min_turns cannot possibly qualify. Exact filtering still
        # happens after the transcript arrives.
        if args.min_turns > 1:
            before = len(summaries)
            summaries = [s for s in summaries
                         if (s.get("message_count") or 0) >= 2 * args.min_turns]
            print(f"prefilter {before - len(summaries)} dropped on message_count "
                  f"(< {2 * args.min_turns} messages)")
        if args.limit:
            summaries = summaries[: args.limit]

        if not summaries:
            sys.exit("nothing in that window. If you passed a bare date to --start/--end, "
                     "that is the cause: the API compares against a timestamp and silently "
                     "returns 0. Use --day, which builds proper bounds.")

        print(f"fetching transcripts with {args.workers} workers…")
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            transcripts = list(pool.map(
                lambda s: _safe_transcript(api, s["id"], tid), summaries))

        cfg = api.config_snapshot(tid, bot_id=summaries[0].get("bot_id"))

    convs, skipped_short, failed = [], 0, 0
    for s, t in zip(summaries, transcripts):
        if t is None:
            failed += 1
            continue
        msgs = t.get("messages") or []
        user_msgs = [m for m in msgs if m.get("role") == "user" and (m.get("content") or "").strip()]
        if len(user_msgs) < args.min_turns:
            skipped_short += 1
            continue
        convs.append({
            "prod_conversation_id": s["id"],
            "created_at": s.get("created_at"),
            "last_message_at": s.get("last_message_at"),
            "status": s.get("status"),
            "title": t.get("title") or s.get("title"),
            "bot_id": s.get("bot_id"),
            "bot_name": t.get("bot_name"),
            "agent_id": s.get("agent_id"),
            "agent_name": t.get("agent_name"),
            "handoff_requested_at": t.get("handoff_requested_at"),
            "handoff_resolution_type": t.get("handoff_resolution_type"),
            "message_count": t.get("message_count", len(msgs)),
            "user_turns": [m["content"] for m in user_msgs],
            "messages": [{"role": m.get("role"), "content": m.get("content"),
                          "created_at": m.get("created_at")} for m in msgs],
        })

    out_dir.mkdir(parents=True, exist_ok=True)
    ensure_gitignored()
    payload = {
        "pulled_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tenant": tenant_label(tid), "tenant_id": tid,
        "window": {"start": start, "end": end, "day": args.day, "timezone": "UTC"},
        "selection": {"mode": "all" if args.all else f"first {args.limit}",
                      "min_turns": args.min_turns, "contains": args.contains,
                      "include_test": args.include_test},
        "found_in_window": len(summaries),
        "kept": len(convs), "skipped_below_min_turns": skipped_short, "transcript_failures": failed,
        "prod_config": cfg,
        "conversations": convs,
    }
    out_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    turns = sum(len(c["user_turns"]) for c in convs)
    print(f"kept     {len(convs)} conversations / {turns} user turns")
    if skipped_short:
        print(f"skipped  {skipped_short} below --min-turns {args.min_turns}")
    if failed:
        print(f"FAILED   {failed} transcripts could not be fetched — they are NOT in the pull")
    a = (cfg.get("agent") or {})
    print(f"prod cfg model={a.get('model')} temp={a.get('temperature')} top_k={a.get('top_k')}")
    if _has_pii(convs):
        print("NOTE     pulled text contains identifier-shaped strings (ID/IBAN/phone). "
              "The pull is unredacted by design; the HTML masks them.")
    print(f"\nPULL FILE: {out_file}")
    print(f"NEXT: .venv/bin/python tools/replay_conversations.py {out_file}")


def _safe_transcript(api: StudioAdmin, cid: str, tid: str):
    try:
        return api.transcript(cid, tid)
    except Exception as e:
        print(f"  !! transcript {cid[:8]} failed: {e}")
        return None


_PII = re.compile(r"\bSA\d{22}\b|\b[12]\d{9}\b|(?:\+?966|00966)5\d{8}\b|\b05\d{8}\b")


def _has_pii(convs: list[dict]) -> bool:
    return any(_PII.search(t) for c in convs for t in c["user_turns"])


if __name__ == "__main__":
    main()
