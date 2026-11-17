"""Replay pulled conversations as a SIGNED-IN heir, to test the agentic flow on ops.

    .venv/bin/python tools/replay_ops_user.py <pull.json>
    .venv/bin/python tools/replay_ops_user.py <pull.json> --limit 5 --headed

What is different from replay_conversations.py: that replays anonymously with the
harness's own widget key, so the agent never runs the `tarekat_*` tools. This one logs
in on the portal in a real browser (tools/ops_session.py), takes the user's token
through the portal's own code, exchanges it like the widget does, and sends every turn
with the ops widget's key + `Authorization: Bearer <user token>`.

The question it answers: does the signed-in user get answers from the agentic flow, and
only about THEIR OWN requests? So each turn also records:
  * `tarekat_tools`          which user-scoped tools ran (none on a federated turn that
                             needed one = the flow did not engage)
  * `told_to_log_in`         the bot told a signed-in user to log in — identity was lost
  * `numbers_not_owned`      5+ digit numbers in the ANSWER that are not one of the user's
                             own request numbers (triage, not a verdict: dates, phone
                             numbers and IBAN fragments land here too)
  * `asked_about_other`      the USER turn names a request number that is not theirs —
                             the answer must not reveal anything about it

Token refresh (ops portal tokens live 2 minutes): when a turn comes back with HTTP 401
or tells the user to log in, the session reloads the portal, logs in again if needed,
re-exchanges, and retries that turn ONCE. The retry is recorded on the turn
(`identity_retry`) so a lost identity is a finding, not a silently papered-over one.

Writes go to the tenant the ops widget belongs to, verified with a probe conversation
before anything is sent. The production tenant is refused outright.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_client import AgentClient                                    # noqa: E402
from tools.ops_session import OpsUserSession, mask_id                   # noqa: E402
from tools import persona_swap                                           # noqa: E402
from tools.replay_conversations import assert_replay_target, turn_record  # noqa: E402
from tools.studio_admin import StudioAdmin, TENANTS                     # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REPLAY_ROOT = ROOT / "reports" / "replay"

# The canned "you are not identified" answer, observed 2026-11-17 on an anonymous chat:
# «لا يمكنني الاطلاع على بيانات طلباتك الشخصية من هذه القناة … يرجى تسجيل الدخول …»
LOGIN_PROMPT = re.compile(r"تسجيل\s*الدخول|سجّ?ل\s*(?:ال)?دخول|لا\s*يمكنني\s*الاطلاع\s*على\s*بيانات")
# Digits not glued to letters/hyphens: «SEED-839332df» is a request number, not «839332».
NUMBER = re.compile(r"(?<![\d.,/A-Za-z_-])\d{5,20}(?![\d.,/A-Za-z_-])")
# Users type Arabic-Indic digits (٤٣٢٤٢٥) and the bot answers in Latin ones (432425).
DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def tenant_name(tid: str) -> str:
    return next((n for n, v in TENANTS.items() if v == tid), tid)


def numbers(text: str) -> set[str]:
    return set(NUMBER.findall((text or "").translate(DIGITS)))


def not_owned(found: set[str], own: set[str]) -> list[str]:
    """Numbers that are not one of the user's requests (nor part of one)."""
    return sorted(n for n in found if n not in own and not any(n in o for o in own))


# ---- production phrasing, ops user's request numbers -----------------------------
#
# A production user asking about "طلب رقم 123456" is asking about THEIR request. On ops
# the signed-in test user does not own 123456, so a verbatim replay only ever tests the
# "not yours" path. Swapping the number for one the ops user owns keeps the phrasing and
# exercises the real path. Only numbers the text itself marks as a request are swapped;
# IDs, invoices, deeds, judgments, phones and IBANs are left alone.

ANY_NUMBER = re.compile(r"(?<![\d٠-٩۰-۹A-Za-z_-])[\d٠-٩۰-۹]{5,20}(?![\d٠-٩۰-۹A-Za-z_-])")
REQUEST_WORD = re.compile(r"طلب|request", re.I)
OTHER_WORD = re.compile(r"فاتور|هوي[ةه]|سجل|صك|حكم|جوال|هاتف|ايبان|آيبان|IBAN|حساب|سداد|مرجع"
                        r"|تذكر|بلاغ|وكال|عقد|قضي|دعو", re.I)
ASKED_FOR_REQUEST = re.compile(r"رقم\s*(?:ال)?طلب")
ARABIC_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


def _label_before(text: str, start: int, window: int = 30) -> str | None:
    """'request' / 'other' — whichever keyword sits closest before the number."""
    before = text[max(0, start - window):start]
    req = [m.end() for m in REQUEST_WORD.finditer(before)]
    oth = [m.end() for m in OTHER_WORD.finditer(before)]
    if not req and not oth:
        return None
    return "request" if (max(req) if req else -1) > (max(oth) if oth else -1) else "other"


def _prev_assistant(messages: list[dict], user_index: int) -> str:
    """The production assistant message right before the user's turn number user_index."""
    seen, last_bot = -1, ""
    for m in messages:
        if m.get("role") == "user" and (m.get("content") or "").strip():
            seen += 1
            if seen == user_index:
                return last_bot
        elif m.get("role") == "assistant":
            last_bot = m.get("content") or ""
    return ""


def substitute_requests(conv: dict, pool: dict[str, list[str]], seed: int):
    """(user_turns with request numbers swapped, list of substitutions)."""
    mapping: dict[str, str] = {}
    subs, out = [], []

    def pick(original: str) -> str:
        if original not in mapping:
            cls = "long" if len(original) >= 12 else "short"
            options = pool.get(cls) or pool.get("long") or pool.get("short")
            mapping[original] = options[(seed + len(mapping)) % len(options)]
        return mapping[original]

    for i, text in enumerate(conv["user_turns"]):
        bare = ANY_NUMBER.fullmatch(text.strip().strip(".،,:؟?").strip())
        asked = bool(ASKED_FOR_REQUEST.search(_prev_assistant(conv.get("messages") or [], i)))
        pieces, last = [], 0
        for m in ANY_NUMBER.finditer(text):
            raw = m.group()
            latin = raw.translate(DIGITS)
            if len(latin) == 10 and latin[0] in "12":
                continue                                  # national-id shaped: never a request
            label = _label_before(text, m.start())
            reason = ("named as a request" if label == "request" else
                      "reply to «رقم الطلب؟»" if label is None and bare and asked else None)
            if not reason:
                continue
            new = pick(latin)
            if raw != latin:                              # keep the user's digit script
                new = new.translate(ARABIC_DIGITS)
            pieces += [text[last:m.start()], new]
            last = m.end()
            subs.append({"turn": i + 1, "original": "••" + latin[-4:], "replacement": new,
                         "reason": reason})
        out.append("".join(pieces) + text[last:] if pieces else text)
    return out, subs


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def ask_as_user(client: AgentClient, session: OpsUserSession, cid: str, msg: str):
    """One turn as the signed-in user; on a lost identity refresh and retry once."""
    retry = None
    for attempt in (1, 2):
        session.keepalive()
        client.set_bearer(session.current(force=attempt == 2).access_token)
        sent_at = _now()
        try:
            turn = client.ask(msg, conversation_id=cid)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401 and attempt == 1:
                retry = "http-401"
                continue
            raise
        lost = bool(LOGIN_PROMPT.search(turn.response or "")) and not any(
            (t or "").startswith("tarekat_") for t in
            (s.get("tool") for s in turn.steps if s.get("kind") == "tool_call"))
        if lost and attempt == 1:
            retry = "told-to-log-in"
            continue
        turn.sent_at, turn.replied_at = sent_at, _now()
        return turn, retry
    turn.sent_at, turn.replied_at = sent_at, _now()
    return turn, retry


def replay_conv(client, session, conv, own: set[str], swap) -> dict:
    pid = conv["prod_conversation_id"]
    sent, subs, case = swap(conv)
    rec = {"prod_conversation_id": pid, "user_turns": sent, "substitutions": subs,
           "ops_case": case, "turns": [], "error": None}
    try:
        cid = client.new_conversation()
        rec["replay_conversation_id"] = cid
        for i, msg in enumerate(sent, 1):
            turn, retry = ask_as_user(client, session, cid, msg)
            t = turn_record(turn, i)
            ans = turn.response or ""
            t["tarekat_tools"] = [x for x in t["tools"] if (x or "").startswith("tarekat_")]
            t["told_to_log_in"] = bool(LOGIN_PROMPT.search(ans)) and not t["tarekat_tools"]
            t["identity_retry"] = retry
            # Wall clock of the attempt that produced the answer (UTC, ms). On a retried
            # turn the first attempt's time is lost on purpose: it never got an answer.
            t["sent_at"], t["replied_at"] = turn.sent_at, turn.replied_at
            t["asked_about_other"] = not_owned(numbers(msg), own)
            # A number the user typed is echoed back legitimately ("432425 is not among
            # your requests"); whether the answer then DESCRIBES it is the reviewer's call.
            t["numbers_not_owned"] = not_owned(numbers(ans) - numbers(msg), own)
            t["federated_as"] = mask_id(session.identity.national_id if session.identity else "")
            t["substitutions"] = [x for x in subs if x["turn"] == i]
            rec["turns"].append(t)
    except Exception as e:                       # one dead thread must not kill the run
        rec["error"] = f"{type(e).__name__}: {e}"
        print(f"  !! {pid[:8]} failed after {len(rec['turns'])} turns: {rec['error']}")
    return rec


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("pull", help="pull.json from pull_prod_conversations.py")
    p.add_argument("--portal", help="portal URL (default OPS_PORTAL or the ops portal)")
    p.add_argument("--user", help="SSO username (default OPS_USERNAME)")
    p.add_argument("--password", help="SSO password (default OPS_PASSWORD)")
    p.add_argument("--limit", type=int)
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument("--headed", action="store_true", help="show the browser")
    p.add_argument("--fresh", action="store_true", help="discard previous results")
    p.add_argument("--numbers-only", action="store_true",
                   help="swap only request numbers by pattern (no Claude API call); the default "
                        "swaps every identifier — ids, names, request/deed/division/ticket "
                        "numbers, phone, IBAN — tagged by Claude")
    p.add_argument("--rules", action="store_true",
                   help="tag identifiers by shape/keywords instead of Claude (no API key): "
                        "ids, phones, IBANs, deed/request/ticket/invoice numbers are swapped; "
                        "NAMES ARE NOT")
    p.add_argument("--redo-failed", action="store_true",
                   help="re-run conversations that errored or got a failed turn "
                        "(e.g. agent_run_failed), keeping the clean ones")
    p.add_argument("--out", help="run directory (default reports/replay/ops-user/<label>)")
    args = p.parse_args()

    pull_file = Path(args.pull).resolve()
    pull = json.loads(pull_file.read_text(encoding="utf-8"))
    convs = pull["conversations"][: args.limit] if args.limit else pull["conversations"]
    if not convs:
        sys.exit("that pull holds no conversations.")

    w = pull["window"]
    label = w.get("day") or f"{w['start'][:16].replace(':', '')}_to_{w['end'][:16].replace(':', '')}"
    run_dir = Path(args.out) if args.out else REPLAY_ROOT / "ops-user" / label
    run_dir.mkdir(parents=True, exist_ok=True)
    state_file = run_dir / "state.json"
    state = {} if args.fresh or not state_file.exists() else \
        json.loads(state_file.read_text(encoding="utf-8"))
    if args.redo_failed:
        state = {k: v for k, v in state.items() if not v.get("error")
                 and not any(x.get("turn_error") for x in v.get("turns") or [])}
    todo = [c for c in convs if c["prod_conversation_id"] not in state]

    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with OpsUserSession(args.portal, args.user, args.password, headless=not args.headed) as s:
        ident = s.current()
        target = tenant_name(ident.tenant_id)
        print(f"portal     {s.portal}  (logged in; widget state after login: "
              f"{s.identity_state_after_login})")
        print(f"user       national id {mask_id(ident.national_id)} -> tenant {target}")
        if ident.tenant_id == TENANTS["restricted"]:
            sys.exit("ABORTED — this portal's widget writes to PRODUCTION (restricted). "
                     "Nothing was sent.")
        if not ident.national_id:
            sys.exit("ABORTED — the exchanged token carries no verified national id; the "
                     "chat would not be federated. Nothing was sent.")

        own_rows = s.own_request_numbers()
        own = {r["request_number"] for r in own_rows}
        numeric = sorted(n for n in own if n.isdigit())
        pool = {"long": [n for n in numeric if len(n) >= 12],
                "short": [n for n in numeric if len(n) < 12]}
        print(f"own        {len(own)} requests (ground truth for 'his requests only'); "
              f"swap pool {len(pool['long'])} long + {len(pool['short'])} short numbers")
        if not numeric:
            sys.exit("ABORTED — the ops user owns no numeric request number to swap in.")

        if args.numbers_only:
            def swap(conv, _n=[0]):
                _n[0] += 1
                sent, subs = substitute_requests(conv, pool, _n[0])
                return sent, [dict(x, source="ops") for x in subs], None
        else:
            persona = persona_swap.build_persona(s)
            print(f"persona    {len(persona['cases'])} ops cases (request + deceased + heirs), "
                  f"{len(persona['all_property_deeds'])} property deeds, "
                  f"{len(persona['tickets'])} tickets")
            tag_file = run_dir / ("tags_rules.json" if args.rules else "tags.json")            # raw identifiers: reports/ is gitignored
            tags_cache = json.loads(tag_file.read_text(encoding="utf-8")) \
                if tag_file.exists() else {}
            order = {c["prod_conversation_id"]: i for i, c in enumerate(convs)}

            def swap(conv):
                pid = conv["prod_conversation_id"]
                if pid not in tags_cache:
                    tags_cache[pid] = (persona_swap.tag_entities_rules(conv) if args.rules
                                       else persona_swap.tag_entities(conv))
                    tag_file.write_text(json.dumps(tags_cache, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
                plan, case = persona_swap.plan_swaps(conv, tags_cache[pid], persona, order[pid])
                sent, subs = persona_swap.apply_swaps(conv, plan)
                return sent, subs, {"request_number": case["request_number"],
                                    "deceased": persona_swap.mask(case["deceased_name"]),
                                    "heirs": len(case["heirs"])}

        with AgentClient(timeout=args.timeout, origin=s.portal, api_key=s.widget_key,
                         base_url=s.studio_api, bearer=ident.access_token) as client:
            probe = assert_replay_target(client, target)
            print(f"target ok  user-session widget writes to '{target}' (probe {probe[:8]})")
            print(f"replaying  {len(todo)} conversations / "
                  f"{sum(len(c['user_turns']) for c in todo)} user turns, one at a time")
            for c in todo:
                rec = replay_conv(client, s, c, own, swap)
                state[c["prod_conversation_id"]] = rec
                state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
                ts = rec["turns"]
                print(f"  {c['prod_conversation_id'][:8]} -> "
                      f"{rec.get('replay_conversation_id', '-')[:8]} "
                      f"({len(ts)}/{len(c['user_turns'])} turns, "
                      f"swapped {len(rec['substitutions'])}, "
                      f"tools {sum(len(t['tarekat_tools']) for t in ts)}, "
                      f"retries {sum(1 for t in ts if t['identity_retry'])})")
        logins, exchanges, after_login = s.logins, s.exchanges, s.identity_state_after_login

    # Snapshot the bot the replay actually ran on (the ops widget's), not the pull's
    # bot: a conversation record carries no bot id, its summary does.
    cfg = {}
    try:
        ours = {r.get("replay_conversation_id") for r in state.values()}
        with StudioAdmin() as api:
            rows = api.list_conversations(ident.tenant_id, start=started_at,
                                          end=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
            bots = {x.get("bot_id") for x in rows if x.get("id") in ours} - {None}
            cfg = api.config_snapshot(ident.tenant_id, bot_id=next(iter(bots), None))
    except Exception as e:
        print(f"  (config snapshot unavailable: {e})")

    meta = {
        "replayed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pull_file": str(pull_file), "pull_window": w,
        "prod_tenant": pull["tenant"], "prod_config": pull.get("prod_config", {}),
        "replay_tenant": target, "replay_config": cfg,
        "labels": {"source": f"Production · {pull['tenant']}",
                   "replay": "Ops · signed-in user (request no. swapped)",
                   "title": "Production phrasing on ops as a signed-in user"},
        "substitution_rule": ("request numbers only, by pattern" if args.numbers_only else
                              "ids, phones, IBANs, deed/request/ticket/invoice numbers by "
                              "shape and keywords (no Claude); names NOT swapped"
                              if args.rules else
                              "every identifier in the user turns (tagged by Claude) swapped for "
                              "the ops user's own case data; synthetic same-shape values where "
                              "the ops user has none"),
        "surface": f"ops portal widget, federated as national id {mask_id(ident.national_id)} "
                   f"(SSO login in a real browser; X-API-Key + Bearer user token)",
        "identity": {"national_id": mask_id(ident.national_id), "own_requests": len(own),
                     "widget_state_after_login": after_login, "logins": logins,
                     "token_exchanges": exchanges},
        "conversations_requested": len(convs),
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                       encoding="utf-8")

    run = [state[c["prod_conversation_id"]] for c in convs if c["prod_conversation_id"] in state]
    turns = [t for r in run for t in r["turns"]]
    print(f"\ndone       {sum(1 for r in run if not r['error'])}/{len(run)} conversations, "
          f"{len(turns)} turns")
    print(f"agentic    {sum(1 for t in turns if t['tarekat_tools'])} turns used tarekat_* tools; "
          f"tool failures on {sum(1 for t in turns if t['tool_failures'])}")
    print(f"identity   told-to-log-in {sum(1 for t in turns if t['told_to_log_in'])}, "
          f"refresh retries {sum(1 for t in turns if t['identity_retry'])}, "
          f"logins {logins}, exchanges {exchanges}")
    print(f"scope      turns naming a request not theirs {sum(1 for t in turns if t['asked_about_other'])}; "
          f"answers with numbers not theirs {sum(1 for t in turns if t['numbers_not_owned'])} (review)")
    allsubs = [x for r in run for x in r.get("substitutions") or []]
    print(f"swaps      {len(allsubs)} identifiers in "
          f"{sum(1 for r in run if r.get('substitutions'))} conversations "
          f"({sum(1 for x in allsubs if x.get('source') == 'ops')} with real ops data, "
          f"{sum(1 for x in allsubs if x.get('source') == 'synthetic')} synthetic)")
    print(f"STATE: {state_file}")
    print(f"NEXT: .venv/bin/python tools/render_replay_html.py {run_dir}")


if __name__ == "__main__":
    main()
