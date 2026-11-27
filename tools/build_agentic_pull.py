"""Turn the hand-written question set into a pull.json that replay_ops_user.py can replay.

    .venv/bin/python tools/build_agentic_pull.py [data/agentic_questions.json]
    .venv/bin/python tools/replay_ops_user.py data/prod_pulls/testing/agentic/pull.json --rules

Conversations whose `turns` are empty are skipped. Nothing is sent anywhere.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "prod_pulls" / "testing" / "agentic" / "pull.json"


def build(src: Path) -> dict:
    spec = json.loads(src.read_text(encoding="utf-8"))
    login_nid = str((spec.get("login_user") or {}).get("national_id") or "").strip()
    if login_nid and not re.fullmatch(r"[12]\d{9}", login_nid):
        sys.exit("login_user.national_id must be a 10-digit id starting with 1 or 2 (or empty).")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    convs, seen = [], set()
    for c in spec.get("conversations", []):
        turns = [t.strip() for t in c.get("turns") or [] if isinstance(t, str) and t.strip()]
        if not turns:
            continue
        cid = str(c["id"])
        if cid in seen:
            sys.exit(f"duplicate id {cid} in {src.name}")
        seen.add(cid)
        convs.append({
            "prod_conversation_id": cid, "created_at": now, "last_message_at": now,
            "status": "authored", "title": f"{cid} {c.get('category', '')}".strip(),
            "bot_id": None, "bot_name": "authored question set", "agent_id": None,
            "agent_name": None, "handoff_requested_at": None, "handoff_resolution_type": None,
            "message_count": len(turns), "user_turns": turns,
            "messages": [{"role": "user", "content": t} for t in turns],
            "authored": {k: c.get(k) for k in ("category", "what_it_checks", "expect")},
        })
    return {
        "pulled_at": now, "tenant": "authored", "tenant_id": None,
        "window": {"start": now, "end": now, "day": "agentic", "timezone": "UTC"},
        "selection": {"source": src.name, "login_national_id": login_nid or None}, "found_in_window": len(convs), "kept": len(convs),
        "skipped_below_min_turns": 0, "transcript_failures": 0, "prod_config": {},
        "conversations": convs,
    }


def main() -> None:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "agentic_questions.json"
    pull = build(src)
    if not pull["conversations"]:
        sys.exit(f"{src.name} has no conversation with turns yet — fill `turns` first.")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(pull, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}  ({len(pull['conversations'])} conversations)")
    print(f"NEXT: .venv/bin/python tools/replay_ops_user.py {OUT.relative_to(ROOT)} --rules")
    print("      (edited conversations are replayed again automatically; --fresh redoes all)")


if __name__ == "__main__":
    main()
