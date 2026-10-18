"""Build the frozen regression set — the same conversations replayed before and after a
model change (docs/IMPROVEMENT_PLAN.md, Phase 0 item 2).

    .venv/bin/python tools/build_frozen_set.py --pick     # once, on purpose: choose prod ids
    .venv/bin/python tools/build_frozen_set.py            # build the replay input
    .venv/bin/python tools/replay_conversations.py data/prod_pulls/frozen/v1/pull.json

The CHOICE lives in data/frozen_set.yaml (committed: ids and file names, no citizen text).
The BUILT file is pull.json-shaped so replay_conversations.py runs it unchanged, and it
sits under data/prod_pulls/ (gitignored) because it carries real production turns.

Contents: prod conversations from one pull + every data/golden.yaml case (one turn) +
every scenario in the scenario YAML (its scripted turns).
"""

import argparse
import json
import pathlib
import sys
from datetime import datetime, timezone

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "data" / "frozen_set.yaml"

ABSTAIN = "لا أملك معلومات كافية"


def _spread(items: list, n: int) -> list:
    """n items evenly spaced through the list, so a pick is not just the first hour."""
    if len(items) <= n:
        return items
    return [items[i * len(items) // n] for i in range(n)]


def pick(pull: dict, n_handoff: int, n_abstain: int, n_other: int) -> list[str]:
    """Multi-turn only, one conversation per title, and the handoffs and abstentions
    are guaranteed a share — they are the turns a model change is most likely to flip."""
    convs = sorted((c for c in pull["conversations"] if len(c["user_turns"]) > 1),
                   key=lambda c: c["created_at"])
    seen, unique = set(), []
    for c in convs:
        if c["title"] not in seen:
            seen.add(c["title"])
            unique.append(c)

    def abstained(c):
        return any(ABSTAIN in (m.get("content") or "") for m in c["messages"]
                   if m.get("role") != "user")

    handoff = [c for c in unique if c["handoff_requested_at"]]
    abstain = [c for c in unique if not c["handoff_requested_at"] and abstained(c)]
    other = [c for c in unique if c not in handoff and c not in abstain]
    chosen = _spread(handoff, n_handoff) + _spread(abstain, n_abstain) + _spread(other, n_other)
    return [c["prod_conversation_id"] for c in chosen]


def cmd_pick(args) -> None:
    if MANIFEST.exists() and not args.force:
        sys.exit(f"{MANIFEST.relative_to(ROOT)} exists — the set is frozen. "
                 "Pass --force to re-pick (that starts a new baseline).")
    pull = json.loads((ROOT / args.pull).read_text(encoding="utf-8"))
    ids = pick(pull, args.handoff, args.abstain, args.other)
    manifest = {
        "version": args.version,
        "picked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "rule": f"multi-turn, one per title, {args.handoff} handoff + {args.abstain} "
                f"abstention + {args.other} other, evenly spaced in time",
        "prod_pull": args.pull,
        "prod_ids": ids,
        "golden": "data/golden.yaml",
        "scenarios": "data/conversation_scenarios.yaml",
    }
    MANIFEST.write_text(
        "# Frozen regression set — change only on purpose; a change means a new baseline.\n"
        "# Built into a replay input by tools/build_frozen_set.py.\n"
        + yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"picked {len(ids)} prod conversation(s) -> {MANIFEST.relative_to(ROOT)}")


def cmd_build(args) -> None:
    if not MANIFEST.exists():
        sys.exit(f"no {MANIFEST.relative_to(ROOT)} — run with --pick first")
    m = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    pull = json.loads((ROOT / m["prod_pull"]).read_text(encoding="utf-8"))

    by_id = {c["prod_conversation_id"]: c for c in pull["conversations"]}
    if missing := [i for i in m["prod_ids"] if i not in by_id]:
        sys.exit(f"{len(missing)} frozen id(s) not in {m['prod_pull']}: {missing}")
    convs = [by_id[i] | {"source": "prod"} for i in m["prod_ids"]]

    # Synthetic cases: ids are prefixed so they can never collide with a prod uuid.
    for g in yaml.safe_load((ROOT / m["golden"]).read_text(encoding="utf-8")):
        steps = g.get("turns") or [g]           # multi-turn cases list {q, expect} per turn
        convs.append({"prod_conversation_id": f"golden:{g['id']}", "source": "golden",
                      "user_turns": [s["q"] for s in steps],
                      "expect": [s["expect"] for s in steps]})
    for s in yaml.safe_load((ROOT / m["scenarios"]).read_text(encoding="utf-8")):
        convs.append({"prod_conversation_id": f"scenario:{s['id']}", "source": "scenario",
                      "title": s["name"], "user_turns": s["turns"],
                      "expected_behavior": s["behavior"], "risk": s["watch_for"]})

    out = ROOT / "data" / "prod_pulls" / "frozen" / m["version"] / "pull.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "pulled_at": pull["pulled_at"],
        # Kept as the source tenant: replay refuses to write into it, which is the point.
        "tenant": pull["tenant"],
        "tenant_id": pull["tenant_id"],
        # A fixed label, so every replay of this set lands in replay/<route>/frozen-<v>/.
        "window": {"day": f"frozen-{m['version']}", "source_window": pull["window"]},
        "selection": {"frozen_set": m["version"], "rule": m["rule"]},
        "prod_config": pull.get("prod_config", {}),
        "conversations": convs,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    counts = {k: sum(c["source"] == k for c in convs) for k in ("prod", "golden", "scenario")}
    turns = sum(len(c["user_turns"]) for c in convs)
    print(f"built {len(convs)} conversation(s), {turns} user turns {counts}")
    print(f"  -> {out.relative_to(ROOT)}")
    print(f"NEXT: .venv/bin/python tools/replay_conversations.py {out.relative_to(ROOT)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pick", action="store_true", help="choose the prod ids and write the manifest")
    ap.add_argument("--force", action="store_true", help="with --pick: overwrite the manifest")
    ap.add_argument("--pull", default="data/prod_pulls/restricted/"
                                      "2026-09-10T0000_to_2026-09-13T1125Z/pull.json")
    ap.add_argument("--version", default="v1")
    ap.add_argument("--handoff", type=int, default=8)
    ap.add_argument("--abstain", type=int, default=8)
    ap.add_argument("--other", type=int, default=9)
    args = ap.parse_args()
    (cmd_pick if args.pick else cmd_build)(args)


if __name__ == "__main__":
    main()
