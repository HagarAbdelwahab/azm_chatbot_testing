"""
Render reports/conversations/state.json as a self-contained HTML review page.

    .venv/bin/python tools/render_html.py
    open reports/conversations/report.html

Everything is inlined — no CDN, no fonts, no network. The file works offline and
never leaves the machine unless you send it somewhere yourself.

The page is built for one job: reading a conversation and judging it. User and bot
are on opposite sides with distinct colours, every bot turn carries its tool trace
and run_id, and confirmation gates render as their own block with the arguments
the agent invented. Filter by expected behaviour, or search across all turns.
"""

import collections
import html
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "reports" / "conversations"
STATE = OUT / "state.json"
PAGE = OUT / "report.html"

ARABIC = re.compile(r"[؀-ۿ]")


def rich(text: str) -> str:
    """Escape, then honour the light markdown the bot actually emits: **bold**,
    blank-line paragraphs, and single newlines inside a paragraph."""
    esc = html.escape(text)
    esc = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", esc, flags=re.S)
    paras = [p.strip() for p in re.split(r"\n\s*\n", esc) if p.strip()]
    return "".join(f"<p>{p.replace(chr(10), '<br>')}</p>" for p in paras)


def direction(text: str) -> str:
    return "rtl" if ARABIC.search(text) else "ltr"


def panels(entry: dict) -> str:
    """Sources + Tools used, the two panels Agent Studio shows under every answer.

    `sources` and `steps` were added to the record on 2026-08-24. Turns captured
    before that carry neither, and the payload is NOT recoverable from state.json —
    say so rather than rendering an empty panel that reads as "nothing retrieved".
    """
    out = ""

    if "sources" in entry:
        srcs = entry["sources"]
        if srcs:
            items = "".join(f'<li><span class="doc">📄</span>{html.escape(str(s))}</li>'
                            for s in srcs)
            out += (f'<details class="pan" open><summary>Sources ({len(srcs)})</summary>'
                    f'<ul class="srcs">{items}</ul></details>')
        else:
            out += ('<details class="pan"><summary>Sources (0)</summary>'
                    '<p class="note">no KB documents returned — this answer was not '
                    'grounded in retrieval.</p></details>')
    else:
        out += ('<p class="note warn">⚠ sources not captured — this turn predates the '
                'client fix. Re-run to record them.</p>')

    # Pair each tool_call with its tool_result by `seq`; `ok:false` is the only
    # signal that a tool ran and FAILED, which a bare name list hides entirely.
    steps = entry.get("steps")
    if steps:
        results = {s.get("seq"): s for s in steps if s.get("kind") == "tool_result"}
        rows = []
        for s in steps:
            if s.get("kind") != "tool_call":
                continue
            res = results.get(s.get("seq"))
            if res is None:
                mark, cls, lbl = "…", "pend", "no result frame"
            elif res.get("ok"):
                mark, cls, lbl = "✓", "ok", ""
            else:
                mark, cls, lbl = "✗", "bad", "failed"
            rows.append(f'<li class="{cls}"><span class="mk">{mark}</span>'
                        f'<code>{html.escape(str(s.get("tool")))}</code>'
                        f'<span class="seq">#{s.get("seq")}</span>'
                        f'<span class="lbl">{lbl}</span></li>')
        out += (f'<details class="pan"><summary>Tools used ({len(rows)})</summary>'
                f'<ul class="tls">{"".join(rows)}</ul></details>')
    elif entry.get("tools"):
        names = "".join(f'<li class="pend"><span class="mk">·</span><code>{html.escape(str(t))}</code></li>'
                        for t in entry["tools"])
        out += (f'<details class="pan"><summary>Tools used ({len(entry["tools"])})</summary>'
                f'<ul class="tls">{names}</ul>'
                '<p class="note">names only — call/result trace predates the client fix.</p>'
                '</details>')
    return out


def bubble(entry: dict, n: int) -> str:
    """One turn. User right, bot left — the reading order Arabic chat expects."""
    if entry["role"] == "user":
        return (
            f'<div class="row user"><div class="bub ub" dir="{direction(entry["text"])}">'
            f'<div class="who">المستخدم · User <span class="tn">turn {n}</span></div>'
            f'{rich(entry["text"])}</div></div>'
        )

    text = entry["text"]
    gate = entry.get("confirmation_request")
    if text.strip():
        body = rich(text)
    elif gate:
        body = '<p class="note">لا يوجد نص — البوت طلب تأكيد الإجراء أدناه.<br>' \
               '<em>No answer text — the bot raised the confirmation gate below.</em></p>'
    else:
        body = '<p class="note warn">⚠ gate payload not captured — this turn predates the ' \
               '<code>confirmation_request</code> client fix. Open the run_id in ' \
               'Studio → Runs &amp; Traces.</p>'

    if gate:
        args = "".join(
            f'<tr><td>{html.escape(str(k))}</td>'
            f'<td dir="{direction(str(v))}">{html.escape(str(v))}</td></tr>'
            for k, v in (gate.get("arguments") or {}).items())
        body += (
            '<div class="gate">'
            f'<div class="gh">⏸ confirmation gate — <code>{html.escape(str(gate.get("tool")))}</code></div>'
            f'<div class="gq" dir="auto">{html.escape(str(gate.get("question") or ""))}</div>'
            f'<table class="gargs">{args}</table>'
            f'<div class="gt">target <code>{html.escape(str(gate.get("target")))}</code>'
            f' · pending_action_id <code>{html.escape(str(gate.get("pending_action_id")))}</code></div>'
            '</div>')

    body += panels(entry)

    chips = [f'<span class="chip ms">{entry["total_ms"]} ms</span>']
    for t in entry["tools"]:
        cls = "chip tool" + (" write" if t == "send_iban_reminder" else "")
        chips.append(f'<span class="{cls}">{html.escape(t)}</span>')
    if not entry["tools"]:
        chips.append('<span class="chip none">no tools</span>')
    if entry["abstention_reason"]:
        chips.append(f'<span class="chip abs">{html.escape(entry["abstention_reason"])}</span>')
    if entry["handoff_status"]:
        chips.append(f'<span class="chip abs">handoff: {html.escape(str(entry["handoff_status"]))}</span>')
    if entry["pii_intercept_count"]:
        chips.append(f'<span class="chip warnchip">pii×{entry["pii_intercept_count"]}</span>')
    if entry["unknown_keys"]:
        chips.append(f'<span class="chip warnchip">unknown: {html.escape(", ".join(entry["unknown_keys"]))}</span>')

    return (
        f'<div class="row bot"><div class="bub bb" dir="{direction(text) if text.strip() else "rtl"}">'
        f'<div class="who">البوت · Bot <span class="tn">turn {n}</span></div>'
        f'{body}'
        f'<div class="meta" dir="ltr">{"".join(chips)}'
        f'<span class="rid" title="click to copy" data-rid="{html.escape(entry["run_id"])}">'
        f'run_id {html.escape(entry["run_id"][:8])}…</span></div>'
        f'</div></div>'
    )


def card(r: dict) -> str:
    turns = r["transcript"]
    nbot = sum(1 for e in turns if e["role"] == "bot")
    gated = any(e.get("confirmation_request") for e in turns if e["role"] == "bot")
    bubbles = []
    n = 0
    for e in turns:
        if e["role"] == "user":
            n += 1
        bubbles.append(bubble(e, n))

    beh = r["expected_behavior"]
    return (
        f'<section class="conv" id="{r["id"]}" data-beh="{html.escape(beh)}" '
        f'data-turns="{nbot}" data-gate="{str(gated).lower()}">'
        f'<header class="ch">'
        f'<div class="chl"><span class="cid">{r["id"]}</span>'
        f'<span class="cat" dir="rtl">{html.escape(r["category"])}</span></div>'
        f'<div class="chr"><span class="beh">{html.escape(beh)}</span>'
        f'<span class="cnt">{nbot} turn{"s" if nbot != 1 else ""}</span></div>'
        f'</header>'
        f'<div class="risk" dir="rtl"><b>الخطر المتوقع:</b> {html.escape(r["risk"])}</div>'
        f'<div class="thread">{"".join(bubbles)}</div>'
        f'<footer class="cf" dir="ltr">conversation_id <code>{html.escape(r["conversation_id"])}</code></footer>'
        f'</section>'
    )


CSS = """
*{box-sizing:border-box}
:root{
 --bg:#f4f5f7; --card:#fff; --ink:#16181d; --dim:#6b7280; --line:#e3e5e9;
 --user:#e8f0fe; --userln:#2f6fed; --bot:#f7f8fa; --botln:#8b93a1;
 --gate:#fff7e6; --gateln:#d98b00; --warn:#b42318; --accent:#2f6fed;
}
@media(prefers-color-scheme:dark){:root{
 --bg:#0f1115; --card:#171a20; --ink:#e8eaed; --dim:#9aa1ac; --line:#272b33;
 --user:#16283f; --userln:#5b9dff; --bot:#1c1f26; --botln:#6b7280;
 --gate:#2b2113; --gateln:#e0a300; --warn:#ff6b5e; --accent:#5b9dff;
}}
body{margin:0;background:var(--bg);color:var(--ink);
 font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Tahoma,Arial,sans-serif}
header.top{position:sticky;top:0;z-index:10;background:var(--card);
 border-bottom:1px solid var(--line);padding:14px 20px}
h1{margin:0 0 3px;font-size:17px}
.sub{color:var(--dim);font-size:13px;margin-bottom:10px}
.res{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px;
 font-size:12px;color:var(--dim)}
.res a{display:inline-flex;gap:6px;align-items:center;text-decoration:none;
 color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:4px 9px}
.res a:hover{border-color:var(--accent);color:var(--accent)}
.res .sz{color:var(--dim);font-size:11px}
.pan{margin:10px 0 0;border:1px solid var(--line);border-radius:8px;
 background:var(--bg);overflow:hidden}
.pan>summary{cursor:pointer;padding:7px 11px;font-size:12px;color:var(--dim);
 letter-spacing:.04em;text-transform:uppercase;user-select:none}
.pan>summary:hover{color:var(--accent)}
.pan ul{list-style:none;margin:0;padding:2px 11px 9px}
.pan li{display:flex;gap:8px;align-items:center;padding:3px 0;font-size:13px}
.srcs li{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
.srcs .doc{opacity:.6}
.tls code{font-size:12.5px}
.tls .mk{width:14px;text-align:center;font-weight:700}
.tls li.ok .mk{color:#0a8a3f}
.tls li.bad .mk{color:var(--warn)}
.tls li.pend .mk{color:var(--dim)}
.tls .seq,.tls .lbl{color:var(--dim);font-size:11px}
.tls li.bad .lbl{color:var(--warn)}
.controls{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
button.f{border:1px solid var(--line);background:transparent;color:var(--ink);
 padding:5px 11px;border-radius:99px;cursor:pointer;font-size:12.5px}
button.f:hover{border-color:var(--accent)}
button.f.on{background:var(--accent);border-color:var(--accent);color:#fff}
#q{flex:1;min-width:200px;padding:6px 11px;border:1px solid var(--line);
 border-radius:8px;background:transparent;color:var(--ink);font-size:13px}
main{max-width:1000px;margin:0 auto;padding:20px}
.conv{background:var(--card);border:1px solid var(--line);border-radius:12px;
 margin-bottom:20px;overflow:hidden}
.ch{display:flex;justify-content:space-between;align-items:center;gap:10px;
 padding:11px 16px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.chl,.chr{display:flex;gap:9px;align-items:center;flex-wrap:wrap}
.cid{font-weight:700;font-size:15px}
.cat{color:var(--dim);font-size:13px}
.beh{font:600 11px/1 ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:.3px;
 background:var(--bot);border:1px solid var(--line);padding:5px 9px;border-radius:6px}
.cnt{color:var(--dim);font-size:12px}
.risk{padding:9px 16px;background:var(--bot);border-bottom:1px solid var(--line);
 font-size:13px;color:var(--dim)}
.thread{padding:16px}
.row{display:flex;margin-bottom:13px}
.row.user{justify-content:flex-end}
.row.bot{justify-content:flex-start}
.bub{max-width:86%;border-radius:12px;padding:11px 14px;font-size:14.5px}
.ub{background:var(--user);border:1px solid var(--line);border-right:3px solid var(--userln)}
.bb{background:var(--bot);border:1px solid var(--line);border-left:3px solid var(--botln)}
.who{font:600 11px/1 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--dim);
 margin-bottom:7px;letter-spacing:.4px;direction:ltr;text-align:left}
.tn{opacity:.6;font-weight:400}
.bub p{margin:0 0 8px}.bub p:last-of-type{margin-bottom:0}
.note{color:var(--dim);font-style:italic}
.note.warn{color:var(--warn);font-style:normal}
.gate{margin-top:11px;background:var(--gate);border:1px solid var(--gateln);
 border-radius:9px;padding:11px}
.gh{font:600 12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;margin-bottom:6px;direction:ltr}
.gq{font-size:13.5px;margin-bottom:9px}
.gargs{width:100%;border-collapse:collapse;font-size:12.5px;margin-bottom:7px}
.gargs td{border-top:1px solid var(--gateln);padding:5px 7px;vertical-align:top}
.gargs td:first-child{font:600 12px ui-monospace,SFMono-Regular,Menlo,monospace;
 white-space:nowrap;width:1%;opacity:.85;direction:ltr}
.gt{font-size:11px;color:var(--dim);direction:ltr}
.meta{margin-top:9px;padding-top:8px;border-top:1px dashed var(--line);
 display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.chip{font:600 10.5px/1 ui-monospace,SFMono-Regular,Menlo,monospace;padding:4px 7px;
 border-radius:5px;border:1px solid var(--line);color:var(--dim);white-space:nowrap}
.chip.tool{color:var(--accent);border-color:var(--accent)}
.chip.tool.write{color:var(--gateln);border-color:var(--gateln)}
.chip.abs{color:var(--gateln);border-color:var(--gateln)}
.chip.warnchip{color:var(--warn);border-color:var(--warn)}
.rid{font:11px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--dim);
 cursor:pointer;margin-left:auto}
.rid:hover{color:var(--accent);text-decoration:underline}
.cf{padding:9px 16px;border-top:1px solid var(--line);font-size:11px;color:var(--dim)}
code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--bot);
 padding:1px 5px;border-radius:4px}
.hidden{display:none}
#empty{color:var(--dim);text-align:center;padding:40px}
"""

JS = """
const convs=[...document.querySelectorAll('.conv')];
let beh='all';
function apply(){
  const q=document.getElementById('q').value.trim().toLowerCase();
  let shown=0;
  convs.forEach(c=>{
    const okB = beh==='all' || (beh==='gate'? c.dataset.gate==='true'
              : beh==='multi'? +c.dataset.turns>1 : c.dataset.beh===beh);
    const okQ = !q || c.textContent.toLowerCase().includes(q);
    const ok = okB && okQ;
    c.classList.toggle('hidden',!ok); if(ok) shown++;
  });
  document.getElementById('empty').classList.toggle('hidden',shown>0);
  document.getElementById('shown').textContent=shown;
}
document.querySelectorAll('button.f').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('button.f').forEach(x=>x.classList.remove('on'));
  b.classList.add('on'); beh=b.dataset.v; apply();
});
document.getElementById('q').oninput=apply;
document.addEventListener('click',e=>{
  const r=e.target.closest('.rid'); if(!r)return;
  navigator.clipboard.writeText(r.dataset.rid);
  const o=r.textContent; r.textContent='copied ✓';
  setTimeout(()=>r.textContent=o,1100);
});
apply();
"""


def main() -> None:
    if not STATE.exists():
        sys.exit(f"no {STATE.relative_to(ROOT)} — run `run_conversations.py ask` first")
    state = json.loads(STATE.read_text(encoding="utf-8"))
    records = sorted(state.values(), key=lambda r: r["id"])

    behaviours = sorted({r["expected_behavior"] for r in records})
    n_bot = sum(1 for r in records for e in r["transcript"] if e["role"] == "bot")
    n_gate = sum(1 for r in records if any(
        e.get("confirmation_request") for e in r["transcript"] if e["role"] == "bot"))

    # Describe what the run actually held. The sheet name used to be hardcoded here,
    # which mislabelled every run that mixed sources — the scenario scripts are not
    # about any one service.
    kinds = {"Q": "ticket questions", "S": "messiness scenarios",
             "T": "ticket probes", "P": "early probes"}
    seen = collections.Counter(r["id"][0] for r in records)
    composition = " + ".join(f"{n} {kinds.get(p, p + '-rows')}"
                             for p, n in sorted(seen.items(), key=lambda kv: -kv[1]))

    filters = ['<button class="f on" data-v="all">all ({})</button>'.format(len(records)),
               f'<button class="f" data-v="multi">multi-turn</button>',
               f'<button class="f" data-v="gate">confirmation gate ({n_gate})</button>']
    filters += [f'<button class="f" data-v="{html.escape(b)}">{html.escape(b)}</button>'
                for b in behaviours]

    # The report is the entry point to the run, so it carries links to every other
    # artefact. Relative hrefs — they resolve under file:// and under http.server alike.
    # replies_round*.json are globbed: a run may have any number of rounds.
    fixed = [("transcripts.md", "same transcripts, plain text and greppable"),
             ("results.csv", "the sheet with `response` filled, `verdict` blank"),
             ("state.json", "raw record incl. every conversation_id")]
    rounds = [(p.name, "the replies sent as the user")
              for p in sorted(OUT.glob("replies_round*.json"))]
    links = []
    for name, why in fixed + rounds:
        f = OUT / name
        if not f.exists():
            continue
        kb = max(1, f.stat().st_size // 1024)
        links.append(f'<a href="{html.escape(name)}" title="{html.escape(why)}">'
                     f'{html.escape(name)}<span class="sz">{kb} KB</span></a>')
    resources = f'<div class="res"><span>run files:</span>{"".join(links)}</div>'

    page = f"""<!doctype html>
<html lang="ar"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Merath chatbot — conversation review</title>
<style>{CSS}</style></head><body>
<header class="top">
 <h1>Merath chatbot — conversation review</h1>
 <div class="sub">{composition} · {len(records)} conversations ·
  {n_bot} bot turns · showing <span id="shown">{len(records)}</span></div>
 {resources}
 <div class="controls">{"".join(filters)}
  <input id="q" placeholder="search all turns… (Arabic or English)">
 </div>
</header>
<main>
{"".join(card(r) for r in records)}
<div id="empty" class="hidden">no conversation matches that filter</div>
</main>
<script>{JS}</script>
</body></html>"""

    PAGE.write_text(page, encoding="utf-8")
    print(f"wrote {PAGE.relative_to(ROOT)}  ({PAGE.stat().st_size // 1024} KB)")
    print(f"linked {len(links)} run file(s) in the report header")
    # Emitted here so the clickable URL is a property of the run, not of whoever
    # happens to be reporting it afterwards.
    print(f"REPORT URL: {PAGE.resolve().as_uri()}")


if __name__ == "__main__":
    main()
