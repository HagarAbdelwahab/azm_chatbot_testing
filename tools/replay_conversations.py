"""Replay pulled production conversations against the Tarekat-Testing tenant.

    .venv/bin/python tools/replay_conversations.py data/prod_pulls/restricted/2026-08-31/pull.json
    .venv/bin/python tools/replay_conversations.py <pull.json> --limit 10 --workers 4

Each production conversation gets ONE fresh conversation on testing, and its user turns
are sent in their original order. The production *answers* are never sent — only what the
human typed — so the testing agent must reach its own conclusions from the same input.

The replay is FAITHFUL, not adaptive: turn 3 goes out even if the testing bot answered
turn 2 completely differently. That is deliberate. The moment you start rewriting the
user's turns to suit the new bot you are no longer measuring the same conversation, and a
"better" transcript would just mean a better improviser. Divergence is the signal — a
follow-up that has become a non-sequitur is itself a finding about the config change.

Resumable: results land in the run's state.json keyed by prod conversation id, and an
already-replayed conversation is skipped unless --fresh is passed.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_client import AgentClient                                   # noqa: E402
from tools.studio_admin import StudioAdmin, resolve_tenant, TENANTS    # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REPLAY_ROOT = ROOT / "reports" / "replay"

_lock = threading.Lock()


def assert_replay_target(client: AgentClient, tenant: str) -> str:
    """Prove the widget key writes to the tenant we think it does — before sending anything.

    THE FLAG DOES NOT ROUTE THE TRAFFIC. `--tenant` selects which config to snapshot and
    how to label the report; where messages actually land is decided solely by
    AGENT_API_KEY in .env. If that key were ever repointed, a replay would write into
    PRODUCTION while the report cheerfully claimed "testing" — real citizens' history
    polluted with synthetic traffic, and a comparison against itself.

    So: open one throwaway conversation, then ask the admin API which tenant can see it.
    A conversation is only visible to its owning tenant (others 404), which makes this a
    definitive check and not an inference. One empty conversation is a cheap price.
    """
    target = resolve_tenant(tenant)
    probe = client.new_conversation()          # created, never sent a message
    try:
        with StudioAdmin() as api:
            api.get(f"/conversations/{probe}", tenant_id=target)
    except Exception:
        owner = "unknown"
        try:
            with StudioAdmin() as api:
                for name, tid in TENANTS.items():
                    try:
                        api.get(f"/conversations/{probe}", tenant_id=tid)
                        owner = f"{name} ({tid})"
                        break
                    except Exception:
                        continue
        except Exception:
            pass
        raise SystemExit(
            f"ABORTED — the widget AGENT_API_KEY does not write to '{tenant}'.\n"
            f"  probe conversation {probe} is not visible to {target}\n"
            f"  it belongs to: {owner}\n"
            f"Nothing was replayed. Fix AGENT_API_KEY in .env, or pass the --tenant that "
            f"key actually belongs to. Never replay into a production tenant.")
    return probe


def turn_record(turn, idx: int) -> dict:
    return {
        "n": idx,
        "user": turn.user_input,
        "bot": turn.response,
        "sources": list(turn.retrieved_contexts),
        # Grounding + the platform's self-scores (2026-09-01 contract). These are the
        # sharpest per-turn signal of a config change: same question, same KB, a
        # retrieval_score that moved is retrieval behaving differently.
        "retrieved_chunks": len(turn.retrieved_chunks),
        "answer_quality_score": turn.answer_quality_score,
        "retrieval_score": turn.retrieval_score,
        "faithfulness_score": turn.faithfulness_score,
        "scoring_version": turn.scoring_version,
        "steps": list(turn.steps),
        "tools": [s.get("tool") for s in turn.steps if s.get("kind") == "tool_call"],
        "tool_failures": [s.get("tool") for s in turn.steps
                          if s.get("kind") == "tool_result" and s.get("ok") is False],
        "confirmation_request": turn.confirmation_request,
        "abstention_reason": turn.abstention_reason,
        "handoff_status": turn.handoff_status,
        # Bot offered the user a human instead of answering. Absent means no offer;
        # not the same as handoff_status, which is a handoff that actually happened.
        "handoff_offer": turn.handoff_offer,
        "pii_intercept_count": turn.pii_intercept_count,
        "content_frames": turn.content_frames,
        "message_id": turn.message_id,
        "run_id": turn.run_id,
        "ttfb_ms": turn.ttfb_ms,
        "first_content_ms": turn.first_content_ms,
        "total_ms": turn.total_ms,
        "unknown_keys": sorted(turn.unknown_keys),
        # Vendor streamed an error instead of an answer (HTTP 200, no done frame).
        # A turn with this set has no reply and must not be diffed as a silent bot.
        "turn_error": turn.error,
    }


def replay_one(client: AgentClient, conv: dict, state: dict, state_file: Path) -> None:
    pid = conv["prod_conversation_id"]
    rec = {"prod_conversation_id": pid, "user_turns": conv["user_turns"], "turns": [],
           "error": None}
    try:
        thread = client.conversation()
        rec["replay_conversation_id"] = thread.id
        for i, msg in enumerate(conv["user_turns"], 1):
            turn = thread.ask(msg)
            rec["turns"].append(turn_record(turn, i))
    except Exception as e:                  # a dead thread must not kill the whole run
        rec["error"] = f"{type(e).__name__}: {e}"
        print(f"  !! {pid[:8]} failed after {len(rec['turns'])} turns: {rec['error']}")

    with _lock:
        state[pid] = rec
        state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    flag = "ERR " if rec["error"] else ""
    print(f"  {flag}{pid[:8]} -> {rec.get('replay_conversation_id','-')[:8]} "
          f"({len(rec['turns'])}/{len(conv['user_turns'])} turns)")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("pull", help="path to a pull.json from pull_prod_conversations.py")
    p.add_argument("--tenant", default="testing", help="replay target (default: testing)")
    p.add_argument("--limit", type=int, help="replay only the first N conversations")
    p.add_argument("--workers", type=int, default=4,
                   help="keep at 4; higher rate-limits the widget key")
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument("--fresh", action="store_true", help="discard previous replay results")
    p.add_argument("--out", help="run directory (default: reports/replay/<tenant>/<day>)")
    args = p.parse_args()

    pull_file = Path(args.pull).resolve()
    if not pull_file.exists():
        sys.exit(f"no such pull file: {pull_file}")
    pull = json.loads(pull_file.read_text(encoding="utf-8"))

    convs = pull["conversations"][: args.limit] if args.limit else pull["conversations"]
    if not convs:
        sys.exit("that pull holds no conversations.")

    # A --day pull is labelled by its day. A windowed (--start/--end) pull must NOT
    # collapse to the same label: a 2-hour window keyed as "2026-09-02" merges into the
    # full-day run's state.json, silently mixing two different pulls into one report and
    # overwriting the day run's meta.json.
    if pull["window"].get("day"):
        day = pull["window"]["day"]
    else:
        w = pull["window"]
        day = f"{w['start'][:10]}T{w['start'][11:16].replace(':', '')}" \
              f"-{w['end'][11:16].replace(':', '')}Z"
    # Name the folder for the DIRECTION, not just the source. `replay/restricted/…`
    # reads as "a replay on restricted", which is the opposite of what happened.
    route = f"{pull['tenant']}-to-{args.tenant}"
    run_dir = Path(args.out) if args.out else REPLAY_ROOT / route / day
    run_dir.mkdir(parents=True, exist_ok=True)
    state_file = run_dir / "state.json"

    state: dict = {}
    if state_file.exists() and not args.fresh:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    todo = [c for c in convs if c["prod_conversation_id"] not in state]

    turns = sum(len(c["user_turns"]) for c in todo)
    print(f"pull       {pull_file}")
    print(f"replaying  {len(todo)} conversations / {turns} user turns onto '{args.tenant}'")
    if len(todo) < len(convs):
        print(f"resuming   {len(convs) - len(todo)} already in {state_file.name}, skipped")
    # Replaying into the tenant you pulled from would inject synthetic traffic into
    # production AND compare a config against itself. Refuse outright.
    if resolve_tenant(args.tenant) == pull["tenant_id"]:
        sys.exit(f"ABORTED — refusing to replay into '{args.tenant}', the same tenant the "
                 f"pull came from ({pull['tenant_id']}). That would write synthetic "
                 f"conversations into production.")

    if not todo:
        print("nothing left to replay.")
    else:
        with AgentClient(timeout=args.timeout) as client:
            probe = assert_replay_target(client, args.tenant)
            print(f"target ok  widget key writes to '{args.tenant}' "
                  f"(verified with probe conversation {probe[:8]})")
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                list(pool.map(lambda c: replay_one(client, c, state, state_file), todo))

    # Snapshot what the replay target was running, at replay time. Without this the run
    # cannot be interpreted six weeks later — or compared against the next one.
    test_cfg = {}
    try:
        with StudioAdmin() as api:
            test_cfg = api.config_snapshot(resolve_tenant(args.tenant))
    except Exception as e:
        print(f"  (config snapshot unavailable: {e})")

    meta = {
        "replayed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pull_file": str(pull_file),
        "pull_window": pull["window"],
        "prod_tenant": pull["tenant"],
        "prod_config": pull.get("prod_config", {}),
        "replay_tenant": args.tenant,
        "replay_config": test_cfg,
        "surface": "widget API (/chat/completions/stream) — rollout % applies, saved to history",
        "conversations_requested": len(convs),
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                       encoding="utf-8")

    # Scope the summary to THIS run. `state` is cumulative — it can hold results from an
    # earlier replay of the same run_dir, and counting those made a 13-conversation run
    # report "69/69 clean".
    this_run = [state[c["prod_conversation_id"]] for c in convs
                if c["prod_conversation_id"] in state]

    # A conversation is only clean if the client survived AND every turn came back with
    # an actual answer. A vendor `error` frame returns HTTP 200, so without this check a
    # run where every single turn failed still reported itself as clean.
    def _turn_ok(t: dict) -> bool:
        return not t.get("turn_error") and bool((t.get("bot") or "").strip()
                                                or t.get("confirmation_request"))

    ok = sum(1 for r in this_run
             if not r.get("error") and r["turns"] and all(_turn_ok(t) for t in r["turns"]))
    failed_turns = [(r["prod_conversation_id"], t) for r in this_run
                    for t in r.get("turns", []) if not _turn_ok(t)]

    a = (test_cfg.get("agent") or {})
    print(f"\ndone       {ok}/{len(this_run)} conversations replayed clean")
    if failed_turns:
        reasons = sorted({t.get("turn_error") or "empty-reply" for _, t in failed_turns})
        print(f"!! {len(failed_turns)} FAILED TURNS on '{args.tenant}' — {', '.join(reasons)}.")
        print("   These turns have NO answer. Do not render or interpret this run as a "
              "comparison; fix the target agent and re-run with --fresh.")
        if any((t.get("turn_error") or "") == "stream_failed" for _, t in failed_turns):
            print("   'stream_failed' is usually a batch-only model on the target agent "
                  "(a ':batch' model_id cannot stream). Check the agent's model.")
    print(f"replay cfg model={a.get('model')} temp={a.get('temperature')} top_k={a.get('top_k')}")
    drift = {k for r in state.values() for t in r.get("turns", []) for k in t["unknown_keys"]}
    if drift:
        print(f"!! CONTRACT DRIFT — unparsed SSE frame keys: {sorted(drift)}. "
              f"Data is being dropped; teach agent_client.py about them before trusting this run.")
    print(f"STATE: {state_file}")
    print(f"NEXT: .venv/bin/python tools/render_replay_html.py {run_dir}")


if __name__ == "__main__":
    main()
