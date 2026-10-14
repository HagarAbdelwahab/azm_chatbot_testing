"""
Convert a spreadsheet of approved Q&A into a golden file the suite can run.

    .venv/bin/python tools/import_tickets.py data/tickets.xlsx
    .venv/bin/python tools/import_tickets.py data/tickets.csv --out data/golden_tickets.yaml

Accepts .csv or .xlsx. Column names are matched case-insensitively and tolerate
Arabic headers, so you do not have to rename anything before importing:

    question / q / سؤال / السؤال          → the user's message      (required)
    answer / a / expected / جواب / الإجابة → the approved answer     (required)
    id / ticket / رقم                      → stable case id         (optional)
    lang                                   → ar | en                (optional, auto-detected)
    notes / ملاحظات                        → carried through as a comment

Every row becomes a case with an `expected_answer`, which the semantic judge
grades on meaning. Rerun this whenever the spreadsheet changes — it overwrites
the output file, so keep hand-edits in data/golden.yaml instead.
"""

import argparse
import csv
import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent

COLUMNS = {
    "question": ("question", "q", "user_input", "prompt", "سؤال", "السؤال", "الاستفسار"),
    "answer":   ("answer", "a", "expected", "expected_answer", "response",
                 "جواب", "الجواب", "الإجابة", "الاجابة", "الرد"),
    "id":       ("id", "ticket", "ticket_id", "case", "case_id", "رقم", "المعرف"),
    "lang":     ("lang", "language", "اللغة"),
    "notes":    ("notes", "note", "comment", "ملاحظات", "ملاحظة"),
}

ARABIC = re.compile(r"[؀-ۿ]")


def _norm_header(h: str) -> str:
    return re.sub(r"[\s_\-]+", "", (h or "").strip().lower())


def _map_headers(headers: list[str]) -> dict[str, int]:
    """field name -> column index"""
    found: dict[str, int] = {}
    normalized = [_norm_header(h) for h in headers]
    for field, aliases in COLUMNS.items():
        for alias in aliases:
            key = _norm_header(alias)
            if key in normalized:
                found[field] = normalized.index(key)
                break
    return found


def _read_csv(path: pathlib.Path) -> tuple[list[str], list[list[str]]]:
    # utf-8-sig strips the BOM Excel writes on "Save as CSV UTF-8"
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        sys.exit(f"{path} is empty")
    return rows[0], rows[1:]


def _read_xlsx(path: pathlib.Path) -> tuple[list[str], list[list[str]]]:
    from openpyxl import load_workbook
    ws = load_workbook(path, data_only=True).active
    rows = [["" if c is None else str(c) for c in r] for r in ws.iter_rows(values_only=True)]
    if not rows:
        sys.exit(f"{path} has no rows")
    return rows[0], rows[1:]


def _slug(text: str, index: int) -> str:
    ascii_words = re.findall(r"[a-z0-9]+", text.lower())[:5]
    return "-".join(ascii_words) if ascii_words else f"ticket-{index:03d}"


def build_cases(headers: list[str], rows: list[list[str]]) -> list[dict]:
    cols = _map_headers(headers)
    for required in ("question", "answer"):
        if required not in cols:
            sys.exit(
                f"could not find a '{required}' column.\n"
                f"  headers seen : {headers}\n"
                f"  accepted     : {', '.join(COLUMNS[required])}"
            )

    def cell(row: list[str], field: str) -> str:
        i = cols.get(field)
        return (row[i].strip() if i is not None and i < len(row) and row[i] else "")

    cases, seen_ids, skipped = [], set(), 0
    for n, row in enumerate(rows, start=1):
        q, a = cell(row, "question"), cell(row, "answer")
        if not q or not a:
            skipped += 1
            continue

        cid = cell(row, "id") or _slug(q, n)
        if cid in seen_ids:                       # ids must be unique — pytest uses them
            cid = f"{cid}-{n}"
        seen_ids.add(cid)

        lang = cell(row, "lang").lower() or ("ar" if ARABIC.search(q) else "en")
        case = {"id": cid, "q": q, "expected_answer": a, "expect": {"lang": lang}}
        if notes := cell(row, "notes"):
            case["notes"] = notes
        cases.append(case)

    if skipped:
        print(f"skipped {skipped} row(s) missing a question or an answer", file=sys.stderr)
    return cases


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spreadsheet", type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path, default=ROOT / "data" / "golden_tickets.yaml")
    args = ap.parse_args()

    path = args.spreadsheet
    if not path.exists():
        sys.exit(f"no such file: {path}")

    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        headers, rows = _read_xlsx(path)
    elif suffix == ".csv":
        headers, rows = _read_csv(path)
    else:
        sys.exit(f"unsupported file type '{suffix}' — use .csv or .xlsx")

    cases = build_cases(headers, rows)
    if not cases:
        sys.exit("no usable rows found")

    header = (
        f"# Generated by tools/import_tickets.py from {path.name} — DO NOT EDIT BY HAND.\n"
        f"# Rerun the importer after changing the spreadsheet.\n"
        f"# Each case carries the approved answer; the semantic judge grades on meaning.\n"
    )
    args.out.write_text(
        header + yaml.safe_dump(cases, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )
    try:
        shown = args.out.resolve().relative_to(ROOT)
    except ValueError:
        shown = args.out
    print(f"wrote {len(cases)} case(s) → {shown}")
    print(f"run them with:  .venv/bin/pytest -m tickets")


if __name__ == "__main__":
    main()
