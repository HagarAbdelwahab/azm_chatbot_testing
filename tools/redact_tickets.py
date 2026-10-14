"""
Replace real PII in a ticket CSV with stable dummy values before anything is sent.

    .venv/bin/python tools/redact_tickets.py data/tickets_prepared_1_100.csv \
        --out data/tickets_prepared_1_100_redacted.csv

Why stable: the same real value always maps to the same dummy, so a ticket that
mentions one national ID twice still reads coherently, and two tickets about the same
person stay linked. Random-per-occurrence substitution would destroy that.

Structured identifiers are handled by regex and are reliable. NAMES ARE NOT — Arabic
personal names have no safe pattern, so they are replaced from an explicit map in
NAME_MAP below, which a human must fill in after reading the flagged rows. The script
reports any row still flagged NAME whose text it did not change, so nothing slips
through silently.

The mapping table is written next to the output as *_redaction_map.json. Keep it out
of version control — it is the only thing that can reverse the redaction.
"""

import argparse
import csv
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Real Arabic names found in the PII-flagged rows -> stable dummies.
# Longest-first at substitution time so "بدر طعيمس المطيري" wins over "بدر".
# Filled in 2026-08-18 after reading every NAME-flagged row. Most NAME flags were
# false positives — banks, courts and ministries, not people. These are the real ones.
# Shared family names stay shared (the two sisters in T070) so kinship survives.
NAME_MAP: dict[str, str] = {
    "بدر طعيمس المطيري":                  "فهد سالم القحطاني",
    "حمد براهيم عبدالرحمن الحرابي":       "سعد إبراهيم عبدالله الدوسري",
    "فوزية علي العريني":                  "نورة علي السبيعي",
    "رقية علي النملة":                    "سارة علي الحربي",
    "سلطان بن فاطم المطيري":              "خالد بن ناصر الشمري",
    "ناصر بن عبدالعزيز بن ناصر العمير":   "عبدالله بن محمد بن عبدالله الزهراني",
    "نايف حمود":                          "ماجد سعود",
    "هياء سالم سليمان اللافي":            "منيرة سالم سليمان العتيبي",
    "منى سالم سليمان اللافي":             "لطيفة سالم سليمان العتيبي",
}

# Order matters: IBAN and request numbers before the generic long-digit rule,
# otherwise the digit rule eats their numeric tails.
PATTERNS: list[tuple[str, str]] = [
    ("IBAN",        r"\bSA\d{22}\b"),
    ("REQUEST_NO",  r"\bED-\d-\d+\b"),
    ("PHONE",       r"(?:\+?966|00966)5\d{8}\b|\b05\d{8}\b"),
    ("NATIONAL_ID", r"\b[12]\d{9}\b"),
    ("DEED_NO",     r"\b\d{8,12}\b"),          # deed / ticket / request numbers
]

DUMMY = {
    "IBAN":        lambda n: f"SA{35 + n:02d}" + f"{10**19 + n * 7919:020d}"[:20],
    "REQUEST_NO":  lambda n: f"ED-2-{900000000 + n * 13:09d}",
    "PHONE":       lambda n: f"05{50000000 + n * 137:08d}",
    "NATIONAL_ID": lambda n: f"1{900000000 + n * 977:09d}",
    "DEED_NO":     lambda n: f"{700000000 + n * 31:09d}",
}


def redact_text(text: str, table: dict[str, dict[str, str]]) -> str:
    """Substitute names, then identifiers.

    Identifiers go via sentinels, NOT straight to their dummy. A dummy national ID is
    itself a 10-digit run, so the generic DEED_NO rule that follows would match it and
    redact it a second time — turning an ID into a deed-shaped number and changing how
    the bot reads it. Sentinels contain no digits, so nothing downstream can match them.
    """
    for real in sorted(NAME_MAP, key=len, reverse=True):
        if real and real in text:
            table["NAME"][real] = NAME_MAP[real]
            text = text.replace(real, NAME_MAP[real])

    pending: dict[str, str] = {}                      # sentinel -> dummy

    for kind, pattern in PATTERNS:
        def sub(m: re.Match) -> str:
            real = m.group(0)
            if real not in table[kind]:
                table[kind][real] = DUMMY[kind](len(table[kind]) + 1)
            sentinel = f"\x00{kind}~{len(pending)}\x00"
            pending[sentinel] = table[kind][real]
            return sentinel
        text = re.sub(pattern, sub, text)

    for sentinel, dummy in pending.items():
        text = text.replace(sentinel, dummy)
    return text


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path, required=True)
    ap.add_argument("--text-col", default="text")
    ap.add_argument("--flag-col", default="pii_present")
    args = ap.parse_args()

    with args.csv.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        sys.exit(f"{args.csv} has no rows")

    table: dict[str, dict[str, str]] = {k: {} for k in list(DUMMY) + ["NAME"]}
    unchanged_names: list[str] = []

    for r in rows:
        before = r[args.text_col]
        r[args.text_col] = redact_text(before, table)
        flags = (r.get(args.flag_col) or "")
        if "NAME" in flags and r[args.text_col] == before:
            unchanged_names.append(r.get("seq") or r.get("id") or "?")

    with args.out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    map_path = args.out.with_name(args.out.stem + "_redaction_map.json")
    map_path.write_text(json.dumps(table, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"wrote {args.out}")
    print(f"wrote {map_path}  (KEEP OUT OF GIT — reverses the redaction)")
    for kind, m in table.items():
        if m:
            print(f"  {kind:12} {len(m)} distinct value(s) replaced")
    if unchanged_names:
        print(f"\n!! {len(unchanged_names)} row(s) flagged NAME but unchanged — "
              f"add them to NAME_MAP: {', '.join(unchanged_names)}")


if __name__ == "__main__":
    main()
