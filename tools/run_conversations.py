"""
Drive a question CSV through the bot as real multi-turn conversations.

Unlike tools/import_tickets.py (which needs an approved answer per row and grades
pass/fail), this collects TRANSCRIPTS for human review. Rows carry an
`expected_behavior` class, not an approved answer, so nothing is auto-graded here.

Three modes, run in order:

  1. ask     open one conversation per row and send the question
                .venv/bin/python tools/run_conversations.py ask data/questions.csv

  2. reply   continue specific conversations with a follow-up the reviewer wrote
                .venv/bin/python tools/run_conversations.py reply replies.json
             replies.json is {"Q03": "نعم، رقم الطلب ED-2-48257094", ...}
             or {"Q03": ["first follow-up", "second follow-up"]} for several turns.

  3. export  write the reviewable artefacts
                .venv/bin/python tools/run_conversations.py export

State lives in reports/conversations/state.json and holds each conversation_id, so
`reply` resumes the *same* thread the bot already has context for. Re-running `ask`
skips rows already present unless you pass --fresh.
"""

import argparse
import csv
import datetime
import json
import pathlib
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))       # run from anywhere: tools/ is not a package

from agent_client import AgentClient  # noqa: E402

OUT = ROOT / "reports" / "conversations"
STATE = OUT / "state.json"


# ---------------------------------------------------------------- state

def load_state() -> dict:
    if not STATE.exists():
        return {}
    return json.loads(STATE.read_text(encoding="utf-8"))


def save_state(state: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


_SAVE_LOCK = threading.Lock()


def save_state_locked(state: dict) -> None:
    """Checkpoint from a worker thread. Long runs must survive a mid-flight failure —
    a 480-turn run that dies on turn 77 and saves nothing is 20 minutes thrown away."""
    with _SAVE_LOCK:
        save_state(state)


def ask_with_retry(client, message: str, conversation_id: str, attempts: int = 3):
    """The vendor stream times out sporadically on long turns. Retry before giving up,
    so one slow response cannot abort a whole batch."""
    import httpx
    for n in range(1, attempts + 1):
        try:
            return client.ask(message, conversation_id=conversation_id)
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError,
                httpx.ConnectError) as exc:
            if n == attempts:
                raise
            print(f"    retry {n}/{attempts - 1} after {type(exc).__name__}", flush=True)
            time.sleep(2 * n)


def _turn_record(turn) -> list[dict]:
    """One exchange -> two transcript entries, keeping the trace metadata on the bot side."""
    return [
        {"role": "user", "text": turn.user_input},
        {
            "role": "bot",
            "text": turn.response,
            "run_id": turn.run_id,
            "message_id": turn.message_id,
            "tools": [s.get("tool") for s in turn.steps if s.get("kind") == "tool_call"],
            # Full trace, not just the names: `seq` pairs a call with its result and
            # `ok` is the only signal that a tool FAILED rather than never ran.
            "steps": turn.steps,
            # The KB documents the answer was grounded in — Studio's "Sources" panel.
            # Without these a reviewer cannot tell a grounded answer from an invented one.
            "sources": turn.retrieved_contexts,
            "searched_kb": turn.searched_kb,
            "abstention_reason": turn.abstention_reason,
            "confirmation_request": turn.confirmation_request,
            "handoff_status": turn.handoff_status,
            "pii_intercept_count": turn.pii_intercept_count,
            "total_ms": turn.total_ms,
            "content_frames": turn.content_frames,
            "unknown_keys": sorted(turn.unknown_keys),
        },
    ]


# ---------------------------------------------------------------- ask

# Real sheets never arrive in the canonical shape. Map whatever they do use onto it,
# so a new export does not need hand-editing before it can run.
COLUMN_ALIASES = {
    "question":          ("question", "text", "q", "user_input", "prompt", "body",
                          "سؤال", "السؤال", "الاستفسار", "نص التذكرة"),
    "id":                ("id", "seq", "ticket_id", "case_id", "ticket", "رقم"),
    "category":          ("category", "length_band", "type", "نوع", "التصنيف"),
    "expected_behavior": ("expected_behavior", "behavior", "expected", "السلوك المتوقع"),
    "risk":              ("risk", "watch_for", "pii_present", "الخطر"),
}


def _norm(h: str) -> str:
    return re.sub(r"[\s_\-]+", "", (h or "").strip().lower())


def read_csv(path: pathlib.Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        raw = list(csv.DictReader(f))
    if not raw:
        sys.exit(f"{path} has no rows")

    headers = list(raw[0])
    normalized = {_norm(h): h for h in headers}
    resolved = {field: next((normalized[_norm(a)] for a in aliases
                             if _norm(a) in normalized), None)
                for field, aliases in COLUMN_ALIASES.items()}

    if not resolved["question"]:
        sys.exit(f"no question column in {path}\n"
                 f"  headers  : {headers}\n"
                 f"  accepted : {', '.join(COLUMN_ALIASES['question'])}")
    print("column mapping: " + ", ".join(
        f"{f}<-{c}" for f, c in resolved.items() if c) +
        (f"  (unmapped: {[f for f,c in resolved.items() if not c]})"
         if any(c is None for c in resolved.values()) else ""))

    rows, seen = [], set()
    for n, r in enumerate(raw, start=1):
        q = (r[resolved["question"]] or "").strip()
        if not q:
            continue
        rid = (r[resolved["id"]] or "").strip() if resolved["id"] else ""
        rid = rid or f"row-{n:03d}"
        if rid in seen:                        # ids key the state dict — must be unique
            rid = f"{rid}-{n}"
        seen.add(rid)
        rows.append({
            "id": rid,
            "question": q,
            "category": (r[resolved["category"]] or "").strip() if resolved["category"] else "",
            "expected_behavior": ((r[resolved["expected_behavior"]] or "").strip()
                                  if resolved["expected_behavior"] else "UNSPECIFIED"),
            "risk": (r[resolved["risk"]] or "").strip() if resolved["risk"] else "",
        })

    if not rows:
        sys.exit(f"every row in {path} has an empty question")
    skipped = len(raw) - len(rows)
    if skipped:
        print(f"skipped {skipped} row(s) with an empty question")
    return rows


def ask_one(row: dict) -> tuple[str, dict]:
    cid = row.get("id") or row["question"][:20]
    with AgentClient(timeout=120) as c:
        turn = c.ask(row["question"])
    print(f"  {cid}: {turn.total_ms}ms  tools={[s.get('tool') for s in turn.steps] or '—'}  "
          f"abstain={turn.abstention_reason or '—'}", flush=True)
    return cid, {
        "id": cid,
        "question": row["question"],
        "category": row.get("category", ""),
        "expected_behavior": row.get("expected_behavior", ""),
        "risk": row.get("risk", ""),
        "conversation_id": turn.conversation_id,
        "transcript": _turn_record(turn),
    }


def cmd_ask(args) -> None:
    rows = read_csv(args.csv)
    state = {} if args.fresh else load_state()
    todo = [r for r in rows if args.fresh or (r.get("id") or "") not in state]
    if not todo:
        print(f"all {len(rows)} row(s) already in {STATE.relative_to(ROOT)} — use --fresh to redo")
        return

    print(f"asking {len(todo)} question(s) with {args.workers} worker(s)...")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for cid, record in pool.map(ask_one, todo):
            state[cid] = record
    save_state(state)
    print(f"\nwrote {len(state)} conversation(s) -> {STATE.relative_to(ROOT)}")


# ---------------------------------------------------------------- scenario

def run_scenario(sc: dict) -> tuple[str, dict]:
    """Send an ordered script of user turns down ONE thread.

    The sequence is the test — repetition, self-contradiction, topic drift. Turns are
    sent regardless of what the bot replies, so the script stays comparable across runs.
    """
    sid = sc["id"]
    client = AgentClient(timeout=180)
    transcript: list[dict] = []
    cid = ""
    try:
        cid = client.new_conversation()
        for i, msg in enumerate(sc["turns"], start=1):
            turn = ask_with_retry(client, msg, cid)
            transcript.extend(_turn_record(turn))
            print(f"  {sid} turn {i}/{len(sc['turns'])}: {turn.total_ms}ms  "
                  f"tools={[s.get('tool') for s in turn.steps] or '—'}", flush=True)
    except Exception as exc:                 # keep the turns we did get
        print(f"  !! {sid} stopped after {len(transcript)//2} turn(s): "
              f"{type(exc).__name__}: {exc}", flush=True)
    finally:
        client.close()

    # Shape it like a CSV record so export/render need no special case.
    return sid, {
        "id": sid,
        "question": sc["turns"][0],
        "category": f"{sc['pattern']} — {sc['name']}",
        "expected_behavior": sc["behavior"],
        "risk": sc["watch_for"],
        "conversation_id": cid,
        "transcript": transcript,
    }


def cmd_scenario(args) -> None:
    import yaml
    scenarios = yaml.safe_load(args.yaml.read_text(encoding="utf-8"))
    state = load_state()
    todo = [s for s in scenarios if args.fresh or s["id"] not in state]
    if not todo:
        print(f"all {len(scenarios)} scenario(s) already run — use --fresh to redo")
        return

    total = sum(len(s["turns"]) for s in todo)
    print(f"running {len(todo)} scenario(s), {total} scripted turns, "
          f"{args.workers} worker(s)...")
    def run_and_save(sc):
        sid, record = run_scenario(sc)
        if record["transcript"]:            # a scenario that got nothing is not worth storing
            state[sid] = record
            save_state_locked(state)        # checkpoint — survive a mid-run failure
        return sid

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(run_and_save, todo))
    save_state(state)

    done = {s["id"] for s in todo if s["id"] in state}
    short = {s["id"]: len(state[s["id"]]["transcript"]) // 2
             for s in todo if s["id"] in state
             and len(state[s["id"]]["transcript"]) // 2 < len(s["turns"])}
    print(f"\nwrote {len(done)}/{len(todo)} scenario(s) -> {STATE.relative_to(ROOT)}")
    if missing := [s["id"] for s in todo if s["id"] not in state]:
        print(f"no turns at all: {missing}")
    if short:
        print(f"incomplete (rerun to top up): {short}")


# ---------------------------------------------------------------- deepen

def cmd_deepen(args) -> None:
    """Top every conversation up to a minimum number of bot turns using a fixed probe
    ladder. Because the same ladder runs on every thread, the added turns are a
    controlled comparison rather than filler — the identical probe across all topics
    shows which subjects make the bot fabricate and which make it hold."""
    import yaml
    ladder = yaml.safe_load(args.ladder.read_text(encoding="utf-8"))
    state = load_state()
    if not state:
        sys.exit(f"no {STATE.relative_to(ROOT)} — run `ask` or `scenario` first")

    def n_bot(rec: dict) -> int:
        return sum(1 for e in rec["transcript"] if e["role"] == "bot")

    def already_sent(rec: dict, step: dict) -> bool:
        """A step counts as sent if its exact text is in the user turns. For the
        repeat_opening step, the opening message appearing twice means it ran."""
        sent = [e["text"] for e in rec["transcript"] if e["role"] == "user"]
        if step.get("repeat_opening"):
            return sent.count(sent[0]) > 1 if sent else False
        return step["text"] in sent

    plan: dict[str, list[dict]] = {}
    for cid, rec in state.items():
        need = args.target - n_bot(rec)
        if need <= 0:
            continue
        # Advance through the ladder — never resend a step this thread already got,
        # otherwise a second `deepen` run just repeats L1/L2 instead of going deeper.
        remaining = [st for st in ladder if not already_sent(rec, st)]
        if len(remaining) < need:
            sys.exit(f"{cid} needs {need} more turn(s) but only {len(remaining)} unsent "
                     f"ladder step(s) remain — add steps to {args.ladder.name}")
        plan[cid] = remaining[:need]

    if not plan:
        print(f"every conversation already has >= {args.target} bot turns")
        return

    total = sum(len(v) for v in plan.values())
    print(f"topping up {len(plan)} conversation(s) to >= {args.target} turns "
          f"({total} probes, {args.workers} workers)...")

    def deepen_one(item):
        cid, steps = item
        rec = state[cid]
        opening = next(e["text"] for e in rec["transcript"] if e["role"] == "user")
        client = AgentClient(timeout=120)
        try:
            for step in steps:
                msg = opening if step.get("repeat_opening") else step["text"]
                turn = client.ask(msg, conversation_id=rec["conversation_id"])
                rec["transcript"].extend(_turn_record(turn))
                print(f"  {cid} {step['id']} ({step['pattern']}): {turn.total_ms}ms  "
                      f"tools={[s.get('tool') for s in turn.steps] or '—'}", flush=True)
        finally:
            client.close()
        return cid

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(deepen_one, plan.items()))
    save_state(state)

    short = {k: n_bot(v) for k, v in state.items() if n_bot(v) < args.target}
    print(f"\nupdated {len(plan)} conversation(s) -> {STATE.relative_to(ROOT)}")
    print("still short: " + (str(short) if short else "none"))


# ---------------------------------------------------------------- reply

def cmd_reply(args) -> None:
    state = load_state()
    if not state:
        sys.exit(f"no {STATE.relative_to(ROOT)} — run `ask` first")

    replies = json.loads(args.replies.read_text(encoding="utf-8"))
    unknown = [k for k in replies if k not in state]
    if unknown:
        sys.exit(f"no conversation for id(s): {unknown}")

    failed: list[tuple[str, str]] = []

    def follow_up(item):
        cid, messages = item
        if isinstance(messages, str):
            messages = [messages]
        record = state[cid]
        client = AgentClient(timeout=180)
        try:
            for msg in messages:
                # One slow turn must not discard the whole round — every turn that
                # did land is already in `record`, and save_state runs regardless.
                try:
                    turn = client.ask(msg, conversation_id=record["conversation_id"])
                except Exception as exc:
                    failed.append((cid, f"{type(exc).__name__}: {exc}"))
                    print(f"  {cid}: FAILED — {type(exc).__name__}", flush=True)
                    break
                record["transcript"].extend(_turn_record(turn))
                print(f"  {cid}: +1 turn  {turn.total_ms}ms  "
                      f"tools={[s.get('tool') for s in turn.steps] or '—'}", flush=True)
        finally:
            client.close()
        return cid

    print(f"sending follow-ups to {len(replies)} conversation(s)...")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(follow_up, replies.items()))
    save_state(state)
    print(f"\nupdated {len(replies) - len(failed)} conversation(s) -> {STATE.relative_to(ROOT)}")
    if failed:
        print(f"failed {len(failed)} — rerun these ids: {[c for c, _ in failed]}")
        for cid, err in failed:
            print(f"  {cid}: {err}")


# ---------------------------------------------------------------- export

def cmd_export(args) -> None:
    state = load_state()
    if not state:
        sys.exit(f"no {STATE.relative_to(ROOT)} — run `ask` first")

    records = sorted(state.values(), key=lambda r: r["id"])

    # Markdown — what a human actually reads.
    md = ["# Conversation transcripts", ""]
    for r in records:
        md += [f"## {r['id']} — {r['category']}", "",
               f"- expected behavior: `{r['expected_behavior']}`",
               f"- risk: {r['risk']}",
               f"- conversation_id: `{r['conversation_id']}`", ""]
        for entry in r["transcript"]:
            if entry["role"] == "user":
                md += [f"**المستخدم:** {entry['text']}", ""]
            else:
                meta = [f"{entry['total_ms']}ms",
                        f"tools={entry['tools'] or '—'}"]
                if "sources" in entry:
                    meta.append(f"sources={entry['sources'] or '—'}")
                if entry["abstention_reason"]:
                    meta.append(f"abstention={entry['abstention_reason']}")
                if entry["handoff_status"]:
                    meta.append(f"handoff={entry['handoff_status']}")
                if entry["pii_intercept_count"]:
                    meta.append(f"pii_intercepts={entry['pii_intercept_count']}")
                if entry["text"].strip():
                    body = entry["text"]
                elif entry.get("confirmation_request"):
                    body = "_(no answer text — the bot raised the confirmation gate below)_"
                else:
                    # Recorded before agent_client learned the confirmation_request frame.
                    # The turn is real; only its payload is missing. Open the run_id in
                    # Studio -> Runs & Traces to see what the gate contained.
                    body = ("_(gate payload NOT captured — this run predates the "
                            "`confirmation_request` client fix; see run_id in Studio)_")
                md += [f"**البوت:** {body}", ""]
                if confirm := entry.get("confirmation_request"):
                    args = ", ".join(f"{k}={v}" for k, v in (confirm.get("arguments") or {}).items())
                    md += [f"> ⏸ **confirmation gate** — `{confirm.get('tool')}` "
                           f"via `{confirm.get('target')}`  ",
                           f"> asks: {confirm.get('question')}  ",
                           f"> args: `{args}`  ",
                           f"> pending_action_id: `{confirm.get('pending_action_id')}`", ""]
                md += [f"<sub>{' · '.join(meta)} · run_id `{entry['run_id']}`</sub>", ""]
        md += ["---", ""]
    md_path = OUT / "transcripts.md"
    md_path.write_text("\n".join(md), encoding="utf-8")

    # CSV — the original sheet with `response` filled in, verdict left for the reviewer.
    csv_path = OUT / "results.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "question", "category", "expected_behavior", "risk",
                    "turns", "tools_used", "abstention", "handoff",
                    "response", "verdict", "reviewer_notes"])
        for r in records:
            bot = [e for e in r["transcript"] if e["role"] == "bot"]
            convo = "\n\n".join(
                (f"— المستخدم: {e['text']}" if e["role"] == "user" else f"— البوت: {e['text']}")
                for e in r["transcript"])
            tools = sorted({t for e in bot for t in e["tools"] if t})
            w.writerow([
                r["id"], r["question"], r["category"], r["expected_behavior"], r["risk"],
                len(bot), ", ".join(tools) or "—",
                ", ".join(e["abstention_reason"] for e in bot if e["abstention_reason"]) or "—",
                ", ".join(e["handoff_status"] for e in bot if e["handoff_status"]) or "—",
                convo, "", "",
            ])

    print(f"wrote {md_path.relative_to(ROOT)}\nwrote {csv_path.relative_to(ROOT)}")


# ---------------------------------------------------------------- cli

# ---------------------------------------------------------------- archive

def cmd_archive(args) -> None:
    """Park the finished run under reports/conversations/<YYYY-MM-DD_HHMM>[-<label>]/.

    Timestamped rather than run1/run2/… so folders sort chronologically and a run
    can be matched to the day it was taken without opening it. The stamp comes from
    state.json's mtime — when the run actually finished, not when it was archived.

    No default label: a hardcoded one (it used to be "investment-division") ends up
    on runs it does not describe. When --label is omitted the composition is derived
    from the row-id prefixes actually present, so a mixed run says so.
    """
    if not STATE.exists():
        sys.exit(f"no {STATE.relative_to(ROOT)} — nothing to archive")

    label = args.label
    if not label:
        kinds = {"Q": "questions", "S": "scenarios", "T": "tickets", "P": "probes"}
        state = json.loads(STATE.read_text(encoding="utf-8"))
        seen = sorted({kinds.get(k[0], k[0].lower()) for k in state})
        label = "+".join(seen)

    stamp = datetime.datetime.fromtimestamp(STATE.stat().st_mtime).strftime("%Y-%m-%d_%H%M")
    dest = OUT / (f"{stamp}-{label}" if label else stamp)
    if dest.exists():
        sys.exit(f"{dest.relative_to(ROOT)} already exists — refusing to overwrite it")

    moved = []
    dest.mkdir(parents=True)
    for f in sorted(OUT.glob("*")):
        if f.is_dir():
            continue
        shutil.move(str(f), str(dest / f.name))
        moved.append(f.name)

    print(f"archived {len(moved)} file(s) -> {dest.relative_to(ROOT)}")
    print("  " + ", ".join(moved))
    print("reports/conversations/ is now clear for the next run")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("ask", help="send each CSV question in its own conversation")
    a.add_argument("csv", type=pathlib.Path)
    a.add_argument("--workers", type=int, default=4)
    a.add_argument("--fresh", action="store_true", help="discard existing state and redo every row")
    a.set_defaults(func=cmd_ask)

    sc = sub.add_parser("scenario", help="run scripted multi-turn scenarios (one thread each)")
    sc.add_argument("yaml", type=pathlib.Path)
    sc.add_argument("--workers", type=int, default=4)
    sc.add_argument("--fresh", action="store_true", help="rerun scenarios already in state")
    sc.set_defaults(func=cmd_scenario)

    d = sub.add_parser("deepen", help="top every conversation up to a minimum turn count")
    d.add_argument("ladder", type=pathlib.Path, nargs="?",
                   default=ROOT / "data" / "deepen_ladder.yaml")
    d.add_argument("--target", type=int, default=4, help="minimum bot turns per conversation")
    d.add_argument("--workers", type=int, default=4)
    d.set_defaults(func=cmd_deepen)

    r = sub.add_parser("reply", help="continue conversations with reviewer-written follow-ups")
    r.add_argument("replies", type=pathlib.Path)
    r.add_argument("--workers", type=int, default=4)
    r.set_defaults(func=cmd_reply)

    e = sub.add_parser("export", help="write transcripts.md + results.csv")
    e.set_defaults(func=cmd_export)

    ar = sub.add_parser("archive", help="move the finished run into a date-time folder")
    ar.add_argument("--label", default="",
                    help="optional suffix after the timestamp; omit for a bare timestamp")
    ar.set_defaults(func=cmd_archive)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
