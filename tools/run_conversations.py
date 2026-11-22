"""
Drive a question CSV through the bot as real multi-turn conversations.

Unlike tools/import_tickets.py (which needs an approved answer per row and grades
pass/fail), this collects TRANSCRIPTS for human review. Rows carry an
`expected_behavior` class, not an approved answer, so nothing is auto-graded here.

Four modes, run in order:

  1. ask     open one conversation per row and send the question
                .venv/bin/python tools/run_conversations.py ask data/questions.csv

  2. reply   continue specific conversations with a follow-up the reviewer wrote
                .venv/bin/python tools/run_conversations.py reply replies.json
             replies.json is {"Q03": "نعم، رقم الطلب ED-2-48257094", ...}
             or {"Q03": ["first follow-up", "second follow-up"]} for several turns.

  3. judge   LLM-grade every bot turn (accuracy, faithfulness, retrieval, behaviour)
             and print the review queue — a human reads only what it flags
                .venv/bin/python tools/run_conversations.py judge

  4. export  write the reviewable artefacts (judge verdicts included when present)
                .venv/bin/python tools/run_conversations.py export

State lives in reports/conversations/state.json and holds each conversation_id, so
`reply` resumes the *same* thread the bot already has context for. Re-running `ask`
skips rows already present unless you pass --fresh.
"""

import argparse
import collections
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

# Which widget the run talks to. Default: AGENT_API_KEY from .env (agent_client's own
# default). --ops logs in to the ops portal once, reads the Tarekat ops portal widget's
# public key + API base off the page (as the browser does) and runs ANONYMOUSLY through
# that bot — no user token, so nothing expires mid-run. Set by main().
_CLIENT_KW: dict = {}


def make_client(timeout: float) -> AgentClient:
    return AgentClient(timeout=timeout, **_CLIENT_KW)


def use_ops_widget(portal: str | None = None) -> None:
    """Point every client at the ops portal widget, and prove it writes to testing."""
    from tools.ops_session import OpsUserSession
    from tools.replay_conversations import assert_replay_target
    with OpsUserSession(portal) as s:
        _CLIENT_KW.update(origin=s.portal, api_key=s.widget_key, base_url=s.studio_api)
    with make_client(60) as c:
        probe = assert_replay_target(c, "testing")
    print(f"target     ops portal widget -> testing (probe {probe[:8]})")


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
            # The bot OFFERING a handoff (bool) — distinct from one that happened. Without
            # it the judge fails ESCALATE turns where the bot did offer a human.
            "handoff_offer": turn.handoff_offer,
            # Set when the vendor streamed {"error": ...} instead of generating. The text
            # is then empty because nothing was generated, not because the bot said nothing.
            "error": turn.error,
            "pii_intercept_count": turn.pii_intercept_count,
            "total_ms": turn.total_ms,
            "content_frames": turn.content_frames,
            "unknown_keys": sorted(turn.unknown_keys),
            # The platform grading itself — shown beside the judge's verdict as a
            # cross-check, never trusted on its own.
            "answer_quality_score": turn.answer_quality_score,
            "retrieval_score": turn.retrieval_score,
            "faithfulness_score": turn.faithfulness_score,
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
    # Optional. When present, `judge` also grades accuracy against it (turn 1 only —
    # the approved answer answers the opening question, not later follow-ups).
    "approved_answer":   ("approved_answer", "answer", "expected_answer", "approved",
                          "الإجابة", "الجواب"),
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
            "approved_answer": ((r[resolved["approved_answer"]] or "").strip()
                                if resolved["approved_answer"] else ""),
        })

    if not rows:
        sys.exit(f"every row in {path} has an empty question")
    skipped = len(raw) - len(rows)
    if skipped:
        print(f"skipped {skipped} row(s) with an empty question")
    return rows


def ask_one(row: dict) -> tuple[str, dict]:
    cid = row.get("id") or row["question"][:20]
    with make_client(120) as c:
        turn = c.ask(row["question"])
    return cid, {
        "id": cid,
        "question": row["question"],
        "category": row.get("category", ""),
        "expected_behavior": row.get("expected_behavior", ""),
        "risk": row.get("risk", ""),
        "approved_answer": row.get("approved_answer", ""),
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
    client = make_client(180)
    transcript: list[dict] = []
    cid = ""
    try:
        cid = client.new_conversation()
        for i, msg in enumerate(sc["turns"], start=1):
            turn = ask_with_retry(client, msg, cid)
            transcript.extend(_turn_record(turn))
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
        client = make_client(120)
        try:
            for step in steps:
                msg = opening if step.get("repeat_opening") else step["text"]
                turn = client.ask(msg, conversation_id=rec["conversation_id"])
                rec["transcript"].extend(_turn_record(turn))
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
        client = make_client(180)
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
                if entry.get("handoff_offer"):
                    meta.append("handoff_offer")
                if entry["pii_intercept_count"]:
                    meta.append(f"pii_intercepts={entry['pii_intercept_count']}")
                if entry.get("error"):
                    body = f"_(generation failed: `{entry['error']}` — not judged)_"
                elif entry["text"].strip():
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
    # When `judge` has run, its worst per-conversation verdict fills the judge_* columns;
    # the human `verdict` column is for the reviewer. `judge --agree` reads the per-turn
    # turns_to_label.csv instead (written below).
    judgments = current_judgments(state, load_judgments())
    csv_path = OUT / "results.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "question", "category", "expected_behavior", "risk",
                    "turns", "tools_used", "abstention", "handoff",
                    "judge_verdict", "judge_severity", "judge_failed_turns", "judge_reason",
                    "response", "verdict", "reviewer_notes"])
        for r in records:
            bot = [e for e in r["transcript"] if e["role"] == "bot"]
            convo = "\n\n".join(
                (f"— المستخدم: {e['text']}" if e["role"] == "user" else f"— البوت: {e['text']}")
                for e in r["transcript"])
            tools = sorted({t for e in bot for t in e["tools"] if t})
            mine = sorted(((int(k.split("/")[1]), j) for k, j in judgments.items()
                           if k.split("/")[0] == r["id"]), key=lambda kv: kv[0])
            flagged = [(n, j) for n, j in mine if j["verdict"] != "pass"]
            worst = max(flagged, key=lambda nj: VERDICT_RANK[nj[1]["verdict"]],
                        default=(None, None))[1]
            w.writerow([
                r["id"], r["question"], r["category"], r["expected_behavior"], r["risk"],
                len(bot), ", ".join(tools) or "—",
                ", ".join(e["abstention_reason"] for e in bot if e["abstention_reason"]) or "—",
                ", ".join(e["handoff_status"] for e in bot if e["handoff_status"]) or "—",
                (worst or {}).get("verdict") or ("pass" if mine else ""),
                (worst or {}).get("severity", ""),
                ", ".join(str(n) for n, _ in flagged),
                " | ".join(f"t{n}: {j['reason']}" for n, j in flagged),
                convo, "", "",
            ])

    label_path = write_label_sheet(state)
    digest = write_digest(state)
    print(f"exported {len(state)} conversations (transcripts.md, results.csv, "
          f"{label_path.name}) · DIGEST: {digest.relative_to(ROOT)}")


# ---------------------------------------------------------------- agent digests
# The agent never needs the whole transcripts.md: `pending` writes only the threads
# that need a reply, `verify` prints only holes, and `export` writes digest.md for the
# final summary. All three are masked and clipped.

SOFT_CLOSE = re.compile(r"هل (يمكنني|أستطيع|استطيع) (مساعدتك|أساعدك|اساعدك) (في|ب)\S*\s+(أي\s+)?"
                        r"(أمر|شيء|شي|استفسار|سؤال|خدمة)\s+(آخر|اخر|أخرى|اخرى)|anything else", re.I)
ASKS_DATA = re.compile(r"رقم (الطلب|طلب|الهوية|الصك|السجل)|الرقم المرجعي|request number|national id"
                       r"|(زودني|تزويدي|أرسل|ارسل|أدخل|ادخل)\S*\s+(ب|برقم)?", re.I)
OFFERS = re.compile(r"هل (ترغب|تريد|تود|تودين|ترغبين)|تحب (أن|ان)|would you like|shall i", re.I)
OPTIONS = re.compile(r"(^|\n)\s*(\d+[.)\-]|[•\-*])\s+\S.*\n\s*(\d+[.)\-]|[•\-*])\s+\S", re.S)


def _clip(t: str | None, n: int) -> str:
    from tools.render_replay_html import mask
    t = " ".join(mask(t or "").split())
    return t if len(t) <= n else t[:n] + "…"


def _head_tail(t: str | None, n: int) -> str:
    """Start AND end of a reply: the ending is what decides whether it needs an answer."""
    t = _clip(t, 10**6)
    if len(t) <= n:
        return t
    tail = max(120, n // 3)
    return t[:n - tail] + " … " + t[-tail:]


def classify_thread(rec: dict) -> tuple[str, bool]:
    """(category, needs a reply?) from the LAST bot turn — the SKILL.md reply table in code."""
    from tools.render_replay_html import is_refusal
    t = rec["transcript"]
    bots = [e for e in t if e["role"] == "bot"]
    if not bots:
        return "no-bot-turn (hole)", False
    b = bots[-1]
    text = (b.get("text") or "").strip()
    if b.get("confirmation_request"):
        prev = bots[-2].get("confirmation_request") if len(bots) > 1 else None
        return ("gate re-issued — stop" if prev else "write gate (approve path not testable)"), False
    if not text:
        return "empty reply (hole)", False
    tail = text[-300:]

    def refused(x: str) -> bool:
        # is_refusal() also hits "contact support" inside real answers; the canned
        # fallbacks are short, so only a short reply counts as a refusal here.
        return is_refusal(x) and len(" ".join(x.split())) < 350

    if refused(text):
        # The reply table: press a refusal ONCE. Two refusals in a row = pressed and held.
        if len(bots) > 1 and refused(bots[-2].get("text") or ""):
            return "refusal held after a press — stop", False
        return "refusal — press once", True
    if SOFT_CLOSE.search(tail):
        return "soft close — nothing blocked", False
    if ASKS_DATA.search(tail):
        return "asks for data — send fact-sheet value", True
    if OPTIONS.search(text) and tail.rstrip().endswith(("؟", "?")):
        return "options list — pick the scenario's item", True
    if OFFERS.search(tail):
        return "offers an action — accept it", True
    if tail.rstrip().endswith(("؟", "?")):
        return "asks a question — answer it", True
    return "answered — nothing blocked", False


def cmd_pending(args) -> None:
    state = load_state()
    if not state:
        sys.exit(f"no {STATE.relative_to(ROOT)} — run `ask` first")
    need, other = [], collections.Counter()
    for rid in sorted(state):
        rec = state[rid]
        cat, reply = classify_thread(rec)
        if reply:
            need.append((rid, cat, rec))
        else:
            other[cat] += 1
    out = [f"# Pending replies — {len(need)} of {len(state)} threads need one",
           "Write replies_roundN.json for THESE ids only (never invent a national ID / IBAN).",
           "- not needing a reply: " + (" · ".join(f"{k}: {v}" for k, v in other.most_common())
                                       or "none"), ""]
    for rid, cat, rec in need:
        t = rec["transcript"]
        lu = next((e["text"] for e in reversed(t) if e["role"] == "user"), "")
        lb = next((e["text"] for e in reversed(t) if e["role"] == "bot"), "")
        out += [f"## {rid} · {cat} · {sum(e['role'] == 'bot' for e in t)} bot turns",
                f"- original: {_clip(rec.get('question') or t[0].get('text'), 200)}",
                f"- expected: {_clip(rec.get('expected_behavior'), 160) or '—'}",
                f"- last user: {_clip(lu, 200)}", f"- last bot: {_head_tail(lb, args.clip)}", ""]
    path = OUT / "pending.md"
    path.write_text("\n".join(out), encoding="utf-8")
    print(f"pending  {len(need)}/{len(state)} threads need a reply -> {path.relative_to(ROOT)}")


def find_holes(state: dict) -> list[str]:
    holes = []
    for k in sorted(state):
        t = state[k]["transcript"]
        u = [e for e in t if e["role"] == "user"]
        b = [e for e in t if e["role"] == "bot"]
        if len(u) != len(b):
            holes.append(f"{k}: {len(u)} user vs {len(b)} bot turns")
        for i, e in enumerate(b, 1):
            if not (e.get("text") or "").strip() and not e.get("confirmation_request"):
                holes.append(f"{k} bot turn {i}: no text and no gate — run_id {e.get('run_id')}")
            if e.get("unknown_keys"):
                holes.append(f"{k} bot turn {i}: unparsed frame keys {e['unknown_keys']}")
    return holes


def cmd_verify(args) -> None:
    state = load_state()
    holes = find_holes(state)
    print(f"verify   {len(state)} conversations · {len(holes)} hole(s)")
    for h in holes:
        print(f"  !! {h}")


def write_digest(state: dict) -> pathlib.Path:
    """digest.md for the final summary: counts, holes, judge queue, gates, and
    substantive answers with nothing retrieved — so the agent never reads the
    full transcripts.md or report.html to write its hand-over."""
    bot = [(rid, n, b) for rid, rec in state.items() for n, _, b, _ in bot_turns(rec)]
    rounds = sorted(OUT.glob("replies_round*.json"))
    judg = current_judgments(state, load_judgments())
    holes = find_holes(state)
    out = [f"# Conversation run digest — {len(state)} conversations · {len(bot)} bot turns · "
           f"{len(rounds)} reply round(s)"]
    if judg:
        c = collections.Counter(j["verdict"] for j in judg.values())
        sev = collections.Counter(j["severity"] for j in judg.values() if j["verdict"] == "fail")
        out.append(f"- judge: pass {c['pass']} · fail {c['fail']} (critical {sev['critical']}, "
                   f"major {sev['major']}, minor {sev['minor']}) · needs_review {c['needs_review']}"
                   f" · unjudged {len(bot) - len(judg)}")
    else:
        out.append("- judge: not run (needs ANTHROPIC_API_KEY) — findings below are triage only")
    out += ["", f"## Holes in the record ({len(holes)})", *([f"- !! {h}" for h in holes] or ["- none"])]
    if judg:
        q = sorted((k for k, j in judg.items() if j["verdict"] != "pass"),
                   key=lambda k: (-VERDICT_RANK[judg[k]["verdict"]], k))
        out += ["", f"## Judge review queue ({len(q)})"]
        out += [f"- {k} · {judg[k]['verdict']} {judg[k].get('severity', '')} · "
                f"{_clip(judg[k].get('reason'), 220)}" for k in q[:20]]
    gates = [(rid, n, b) for rid, n, b in bot if b.get("confirmation_request")]
    out += ["", f"## Write gates ({len(gates)}) — check arguments for fabricated identifiers"]
    for rid, n, b in gates[:15]:
        cr = b["confirmation_request"]
        args = cr.get("arguments") or cr.get("args") or cr.get("params") or {}
        out.append(f"- {rid}/{n} · {cr.get('tool') or cr.get('name') or '?'} · "
                   f"{_clip(json.dumps(args, ensure_ascii=False), 200)}")
    def grounded(b):
        return bool(b.get("sources") or b.get("searched_kb") or any(
            (s.get("tool") or "").startswith(("search_knowledge_base", "tarekat_"))
            for s in b.get("steps") or []))
    ung = [(rid, n, b) for rid, n, b in bot
           if len((b.get("text") or "")) > 220 and not grounded(b) and not b.get("confirmation_request")]
    out += ["", f"## Substantive answers with nothing retrieved ({len(ung)}) — any figure here is invented"]
    out += [f"- {rid}/{n}: {_clip(b.get('text'), 220)}" for rid, n, b in ung[:12]]
    cats = collections.Counter(classify_thread(r)[0] for r in state.values())
    out += ["", "## Thread endings", *[f"- {k}: {v}" for k, v in cats.most_common()]]
    # One line per conversation so findings can be ranked without transcripts.md:
    # what the sheet expected, how the thread ended, and the most substantive answer.
    out += ["", "## Expected vs outcome (one line each; open a conversation only to confirm)"]
    for rid in sorted(state):
        rec = state[rid]
        bots = [e for e in rec["transcript"] if e["role"] == "bot"]
        best = max((e.get("text") or "" for e in bots), key=len, default="")
        out.append(f"- {rid} · expected {rec.get('expected_behavior') or '—'} · "
                   f"{classify_thread(rec)[0]} · {len(bots)} turns · {_clip(best, 200)}")
    path = OUT / "digest.md"
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path


LABELS = OUT / "turns_to_label.csv"
LABEL_COLS = ["key", "id", "turn", "expected_behavior", "risk", "user_message", "bot_reply",
              "approved_answer", "run_id", "verdict", "reviewer_notes"]


def write_label_sheet(state: dict) -> pathlib.Path:
    """One row per bot turn, for a human to mark pass/fail — the input to `judge --agree`.

    Deliberately WITHOUT the judge's verdict: a labeller who can see it tends to agree
    with it, and the agreement score then measures nothing. Labels already filled in are
    kept on re-export as long as the turn's run_id is unchanged (same answer)."""
    kept = {}
    if LABELS.exists():
        with LABELS.open(encoding="utf-8-sig", newline="") as f:
            kept = {r["key"]: r for r in csv.DictReader(f) if (r.get("verdict") or "").strip()}
    with LABELS.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LABEL_COLS)
        w.writeheader()
        for rid, rec in sorted(state.items()):
            for n, user, bot, _ in bot_turns(rec):
                key = f"{rid}/{n}"
                old = kept.get(key, {})
                same = old.get("run_id") == bot.get("run_id", "")
                w.writerow({
                    "key": key, "id": rid, "turn": n,
                    "expected_behavior": rec.get("expected_behavior", ""),
                    "risk": rec.get("risk", ""),
                    "user_message": user["text"],
                    "bot_reply": (f"(generation failed: {bot['error']})" if bot.get("error")
                                  else bot["text"]),
                    "approved_answer": rec.get("approved_answer", "") if n == 1 else "",
                    "run_id": bot.get("run_id", ""),
                    "verdict": old.get("verdict", "") if same else "",
                    "reviewer_notes": old.get("reviewer_notes", "") if same else "",
                })
    return LABELS


# ---------------------------------------------------------------- judge

JUDGMENTS = OUT / "judgments.json"
VERDICT_RANK = {"fail": 2, "needs_review": 1, "pass": 0}


def load_judgments() -> dict:
    if not JUDGMENTS.exists():
        return {}
    return json.loads(JUDGMENTS.read_text(encoding="utf-8"))


def bot_turns(rec: dict) -> list[tuple[int, dict, dict, list[dict]]]:
    """(bot-turn number, user entry, bot entry, history before this exchange)."""
    out, history, last_user, n = [], [], None, 0
    for e in rec["transcript"]:
        if e["role"] == "user":
            last_user = e
            continue
        n += 1
        out.append((n, last_user or {"role": "user", "text": ""}, e, list(history)))
        history += [last_user, e] if last_user else [e]
        last_user = None
    return out


def current_judgments(state: dict, judgments: dict) -> dict:
    """Drop judgments that no longer describe the transcript: the turn was re-asked
    (`ask`/`scenario --fresh`) so its run_id changed, or the turn is gone. Otherwise a
    report shows an old verdict next to a new answer."""
    live = {f"{rid}/{n}": bot.get("run_id", "")
            for rid, rec in state.items() for n, _, bot, _ in bot_turns(rec)}
    return {k: j for k, j in judgments.items()
            if k in live and j.get("run_id", "") == live[k]}


def cmd_judge(args) -> None:
    sys.path.insert(0, str(ROOT))
    import judge

    if args.agree:
        return judge_agreement(args.agree)

    state = load_state()
    if not state:
        sys.exit(f"no {STATE.relative_to(ROOT)} — run `ask` or `scenario` first")
    if not judge.HAVE_KEY:
        sys.exit("ANTHROPIC_API_KEY is not set in .env — the judge needs it. Nothing was graded.")

    judgments = {} if args.fresh else load_judgments()
    todo, errored = [], []
    for rid, rec in sorted(state.items()):
        if args.only and rid not in args.only:
            continue
        for n, user, bot, history in bot_turns(rec):
            key = f"{rid}/{n}"
            if bot.get("error"):              # nothing was generated — nothing to grade
                errored.append(key)
                judgments.pop(key, None)
                continue
            # Same run_id = same answer already graded. A different one means the turn
            # was re-asked since, so the stored verdict is about an answer that is gone.
            if key in judgments and judgments[key].get("run_id") == bot.get("run_id", ""):
                continue
            todo.append((key, rec, n, user, bot, history))

    if not todo:
        print(f"every bot turn already judged in {JUDGMENTS.relative_to(ROOT)} — use --fresh to regrade")
    else:
        model = args.model or judge.JUDGE_MODEL
        print(f"judging {len(todo)} bot turn(s) with {model}, {args.workers} worker(s)...")

        def judge_one(item):
            key, rec, n, user, bot, history = item
            try:
                j = judge.evaluate_turn(
                    question=user["text"], answer=bot["text"], history=history,
                    chunks=bot.get("sources") or [],
                    approved=rec.get("approved_answer") if n == 1 else None,
                    expected_behavior=rec.get("expected_behavior", ""),
                    risk=rec.get("risk", ""), model=model,
                    meta={"abstention_reason": bot.get("abstention_reason"),
                          "handoff_status": bot.get("handoff_status"),
                          "handoff_offer": bot.get("handoff_offer"),
                          "confirmation_request": bot.get("confirmation_request"),
                          "searched_kb": bot.get("searched_kb")})
            except Exception as exc:          # one bad call must not lose the batch
                print(f"  {key}: ERROR {type(exc).__name__}: {exc}", flush=True)
                return
            rec_j = j.to_dict() | {"run_id": bot.get("run_id", "")}
            with _SAVE_LOCK:
                judgments[key] = rec_j
                OUT.mkdir(parents=True, exist_ok=True)
                JUDGMENTS.write_text(json.dumps(judgments, ensure_ascii=False, indent=1),
                                     encoding="utf-8")
            sev = f" [{j.severity}]" if j.verdict == "fail" else ""
            print(f"  {key}: {j.verdict}{sev}  acc={j.accuracy} faith={j.faithfulness} "
                  f"retr={j.retrieval} beh={j.behavior}", flush=True)

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            list(pool.map(judge_one, todo))

    # Summary over everything judged so far, not just this call — minus stale verdicts.
    judgments = current_judgments(state, load_judgments())
    counts = collections.Counter(j["verdict"] for j in judgments.values())
    sev = collections.Counter(j["severity"] for j in judgments.values() if j["verdict"] == "fail")
    print(f"\n{len(judgments)} turn(s) judged -> {JUDGMENTS.relative_to(ROOT)}")
    print(f"  pass {counts['pass']} · fail {counts['fail']} "
          f"(critical {sev['critical']}, major {sev['major']}, minor {sev['minor']}) · "
          f"needs_review {counts['needs_review']}")
    queue = sorted((k for k, j in judgments.items() if j["verdict"] != "pass"),
                   key=lambda k: (-VERDICT_RANK[judgments[k]["verdict"]], k))
    if queue:
        print("review queue (open these, skip the rest):")
        for k in queue:
            j = judgments[k]
            print(f"  {k:12} {j['verdict']:12} {j['severity']:8} {j['reason'][:110]}")
    else:
        print("review queue empty — every judged turn passed")
    if errored:
        print(f"skipped {len(errored)} errored turn(s) — generation failed, re-ask them: "
              + ", ".join(errored))


def judge_agreement(path: pathlib.Path) -> None:
    """Judge vs human, per bot turn: compare each judged turn to the reviewer's `verdict`
    (pass / fail, or ✓ / ✗, or نجح / فشل) in turns_to_label.csv. Run this before trusting
    the judge to replace manual review — and again whenever the judge prompt or model changes.

    Agreement is over turns where the judge DECIDED (pass/fail). needs_review is shown in
    its own column, not counted as a fail: it is the judge abstaining, not disagreeing."""
    judgments = current_judgments(load_state(), load_judgments())
    if not judgments:
        sys.exit(f"no current judgments in {JUDGMENTS.relative_to(ROOT)} — run `judge` first")
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if rows and "key" not in rows[0]:
        sys.exit(f"{path} has no `key` column — label turns_to_label.csv (written by "
                 "`export`), one row per bot turn")
    norm = {"pass": "pass", "ok": "pass", "✓": "pass", "نجح": "pass", "سليم": "pass",
            "fail": "fail", "✗": "fail", "x": "fail", "فشل": "fail", "خطأ": "fail"}
    matrix = collections.Counter()               # (human, judge) -> count
    disagree, stale = [], []
    for r in rows:
        human = norm.get((r.get("verdict") or "").strip().lower())
        key = (r.get("key") or "").strip()
        if not human or key not in judgments:
            continue
        j = judgments[key]
        if r.get("run_id") and r["run_id"] != j.get("run_id"):
            stale.append(key)                    # labelled a different answer than was judged
            continue
        matrix[human, j["verdict"]] += 1
        if j["verdict"] in ("pass", "fail") and j["verdict"] != human:
            disagree.append((key, human, j))
    labelled = sum(matrix.values())
    if not labelled:
        sys.exit(f"no row in {path} has both a human `verdict` and a judged turn — "
                 "label a sample first")

    decided = sum(n for (_, jv), n in matrix.items() if jv != "needs_review")
    agree = decided - len(disagree)
    print(f"{labelled} labelled turn(s)\n")
    print(f"{'':14}{'judge pass':>12}{'judge fail':>12}{'needs_review':>14}")
    for h in ("pass", "fail"):
        print(f"{'human ' + h:14}" + "".join(
            f"{matrix[h, jv]:>{w}}" for jv, w in (("pass", 12), ("fail", 12), ("needs_review", 14))))
    if decided:
        print(f"\nagreement {agree}/{decided} = {100 * agree / decided:.0f}% of turns the judge "
              "decided  (target ≥ 90% before reviewing only the flagged queue)")
    # The costly miss: the judge passed a turn a human failed — it would never be read.
    print(f"missed fails (human fail, judge pass): {matrix['fail', 'pass']}"
          f"   false alarms (human pass, judge fail): {matrix['pass', 'fail']}")
    for key, human, j in sorted(disagree, key=lambda d: d[1] != "fail"):
        print(f"  {key:12} human={human:5} judge={j['verdict']:5} {j['reason'][:100]}")
    if stale:
        print(f"skipped {len(stale)} label(s) on answers since re-asked: {', '.join(stale)}")


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
    ap.add_argument("--ops", action="store_true",
                    help="talk to the Tarekat ops portal widget (testing) instead of the "
                         "AGENT_API_KEY bot; logs in once with OPS_USERNAME/OPS_PASSWORD")
    ap.add_argument("--portal", help="ops portal URL (default OPS_PORTAL)")
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

    pe = sub.add_parser("pending", help="write pending.md: only the threads that need a reply")
    pe.add_argument("--clip", type=int, default=400, help="chars of the last bot turn to show")
    pe.set_defaults(func=cmd_pending)

    v = sub.add_parser("verify", help="print holes in the record (empty turns, drift)")
    v.set_defaults(func=cmd_verify)

    e = sub.add_parser("export", help="write transcripts.md + results.csv")
    e.set_defaults(func=cmd_export)

    j = sub.add_parser("judge", help="LLM-grade every bot turn; prints the review queue")
    j.add_argument("--workers", type=int, default=4)
    j.add_argument("--fresh", action="store_true", help="regrade turns already judged")
    j.add_argument("--only", nargs="+", metavar="ID", help="judge only these conversation ids")
    j.add_argument("--model", help="override JUDGE_MODEL, e.g. claude-opus-5-5 for a release gate")
    j.add_argument("--agree", type=pathlib.Path, metavar="CSV",
                   help="compare judge verdicts per turn to a labelled turns_to_label.csv instead")
    j.set_defaults(func=cmd_judge)

    ar = sub.add_parser("archive", help="move the finished run into a date-time folder")
    ar.add_argument("--label", default="",
                    help="optional suffix after the timestamp; omit for a bare timestamp")
    ar.set_defaults(func=cmd_archive)

    args = ap.parse_args()
    if args.ops and args.cmd in ("ask", "scenario", "deepen", "reply"):
        use_ops_widget(args.portal)
    args.func(args)


if __name__ == "__main__":
    main()
