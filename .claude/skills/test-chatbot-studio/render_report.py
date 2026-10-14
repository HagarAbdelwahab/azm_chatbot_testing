#!/usr/bin/env python3
"""Render Agent Studio playground results as a reviewable HTML report.

    python3 render_report.py studio_results.json [--out DIR]

Writes <out>/reports/studio/<agent-id>/report.html — deliberately NOT
reports/conversations/, which belongs to the widget harness: that directory is
swept wholesale by `run_conversations.py archive`, and its report.html would be
overwritten. Keying on agent id keeps two agents from clobbering each other.
"""

import html
import json
import pathlib
import sys

CSS = """
*{box-sizing:border-box}
:root{--bg:#f4f5f7;--card:#fff;--ink:#16181d;--dim:#6b7280;--line:#e3e5e9;
 --user:#e8f0fe;--userln:#2f6fed;--bot:#f7f8fa;--botln:#8b93a1;
 --warn:#b42318;--accent:#2f6fed;--ok:#0a8a3f}
:root:not([data-theme=light]){}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
 --bg:#0f1115;--card:#171a20;--ink:#e8eaed;--dim:#9aa1ac;--line:#272b33;
 --user:#16283f;--userln:#5b9dff;--bot:#1c1f26;--botln:#6b7280;
 --warn:#ff6b5e;--accent:#5b9dff;--ok:#3ecf7a}}
:root[data-theme=dark]{--bg:#0f1115;--card:#171a20;--ink:#e8eaed;--dim:#9aa1ac;
 --line:#272b33;--user:#16283f;--userln:#5b9dff;--bot:#1c1f26;--botln:#6b7280;
 --warn:#ff6b5e;--accent:#5b9dff;--ok:#3ecf7a}
body{margin:0;background:var(--bg);color:var(--ink);
 font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header.top{position:sticky;top:0;z-index:10;background:var(--card);
 border-bottom:1px solid var(--line);padding:14px 20px}
h1{margin:0 0 4px;font-size:19px}
.sub{color:var(--dim);font-size:13px;margin-bottom:8px}
.meta-strip{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px;font-size:12px}
.meta-strip span{border:1px solid var(--line);border-radius:6px;padding:3px 9px;
 background:var(--bg);color:var(--dim)}
.meta-strip b{color:var(--ink);font-weight:600}
.controls{display:flex;gap:7px;flex-wrap:wrap;align-items:center}
button.f{border:1px solid var(--line);background:transparent;color:var(--ink);
 border-radius:8px;padding:5px 11px;font-size:13px;cursor:pointer}
button.f:hover{border-color:var(--accent)}
button.f.on{background:var(--accent);border-color:var(--accent);color:#fff}
#q{flex:1;min-width:200px;padding:6px 11px;border:1px solid var(--line);
 border-radius:8px;background:transparent;color:var(--ink);font-size:13px}
main{padding:18px 20px;max-width:1000px;margin:0 auto}
.conv{background:var(--card);border:1px solid var(--line);border-radius:12px;
 margin-bottom:16px;overflow:hidden}
.ch{display:flex;gap:10px;align-items:center;padding:11px 16px;
 border-bottom:1px solid var(--line);flex-wrap:wrap}
.cid{font-weight:700}
.rows{padding:14px 16px}
.row{display:flex;margin-bottom:10px}
.row.user{justify-content:flex-end}
.bub{max-width:86%;padding:10px 13px;border-radius:12px;white-space:pre-wrap;
 word-wrap:break-word;overflow-wrap:anywhere}
.ub{background:var(--user);border:1px solid var(--line);border-right:3px solid var(--userln)}
.bb{background:var(--bot);border:1px solid var(--line);border-left:3px solid var(--botln)}
.who{font-size:11px;color:var(--dim);margin-bottom:5px;letter-spacing:.03em}
.pan{margin:10px 0 0;border:1px solid var(--line);border-radius:8px;
 background:var(--bg);overflow:hidden}
.pan>summary{cursor:pointer;padding:7px 11px;font-size:12px;color:var(--dim);
 letter-spacing:.04em;text-transform:uppercase;user-select:none}
.pan>summary:hover{color:var(--accent)}
.pan ul{list-style:none;margin:0;padding:2px 11px 9px}
.pan li{display:flex;gap:8px;align-items:center;padding:3px 0;font-size:13px;
 font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.doc{opacity:.6}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:9px}
.chip{font-size:11px;border-radius:20px;padding:3px 9px;border:1px solid var(--line);
 color:var(--dim);font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.chip.good{color:var(--ok);border-color:var(--ok)}
.chip.low{color:var(--warn);border-color:var(--warn)}
.chip.held{color:var(--warn);border-color:var(--warn)}
.chip.nosrc{color:var(--warn);border-color:var(--warn)}
.note{color:var(--dim);font-size:13px;margin:6px 0 0}
.hidden{display:none}
#empty{color:var(--dim);text-align:center;padding:40px}
"""

JS = """
const cards=[...document.querySelectorAll('.conv')];
const shown=document.getElementById('shown');
const empty=document.getElementById('empty');
let filter='all', term='';
function apply(){
  let n=0;
  cards.forEach(c=>{
    const okF = filter==='all'
      || (filter==='held' && c.dataset.held==='1')
      || (filter==='nosrc' && c.dataset.nosrc==='1')
      || (filter==='answered' && c.dataset.held==='0');
    const okT = !term || c.innerText.toLowerCase().includes(term);
    const vis = okF && okT;
    c.classList.toggle('hidden',!vis); if(vis) n++;
  });
  shown.textContent=n; empty.classList.toggle('hidden',n>0);
}
document.querySelectorAll('button.f').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('button.f').forEach(x=>x.classList.remove('on'));
  b.classList.add('on'); filter=b.dataset.v; apply();
});
document.getElementById('q').oninput=e=>{term=e.target.value.toLowerCase().trim();apply();};
"""


def esc(t):
    return html.escape(str(t or ""))


def rtl(t):
    return "rtl" if any("؀" <= c <= "ۿ" for c in str(t or "")) else "ltr"


def card(i, r):
    asked = r.get("asked") or r.get("question") or ""
    answer = r.get("answer") or ""
    srcs = r.get("sources") or []
    tools = r.get("tools") or []
    held = "Withheld" in answer or not answer.strip()
    # A confident answer grounded in nothing is the flag worth surfacing, so make
    # "answered with zero sources" filterable rather than buried in the panel.
    nosrc = (not held) and not srcs

    isolated = (r.get("question") or "").strip() == asked.strip()

    body = esc(answer) if answer.strip() else '<span class="note">(no answer captured)</span>'

    pan = ""
    if srcs:
        items = "".join(f'<li><span class="doc">📄</span>{esc(s)}</li>' for s in srcs)
        pan += (f'<details class="pan" open><summary>Sources ({len(srcs)})</summary>'
                f'<ul>{items}</ul></details>')
    else:
        pan += ('<details class="pan"><summary>Sources (0)</summary>'
                '<p class="note" style="padding:0 11px 9px">no documents retrieved — '
                'this answer was not grounded in the knowledge base.</p></details>')
    if tools:
        items = "".join(f'<li>{esc(t)}</li>' for t in tools)
        pan += (f'<details class="pan"><summary>Tools used ({len(tools)})</summary>'
                f'<ul>{items}</ul></details>')

    chips = []
    q, f = r.get("quality"), r.get("faithfulness")
    if q:
        try:
            chips.append(f'<span class="chip {"good" if float(q) >= 0.9 else "low"}">Quality {esc(q)}</span>')
        except ValueError:
            chips.append(f'<span class="chip">Quality {esc(q)}</span>')
    if f:
        try:
            chips.append(f'<span class="chip {"good" if float(f) >= 0.9 else "low"}">Faithfulness {esc(f)}</span>')
        except ValueError:
            chips.append(f'<span class="chip">Faithfulness {esc(f)}</span>')
    if held:
        chips.append('<span class="chip held">withheld — clarifying question</span>')
    if nosrc:
        chips.append('<span class="chip nosrc">answered with 0 sources</span>')
    if not isolated:
        chips.append('<span class="chip low">NOT ISOLATED — thread carried context</span>')

    return f"""
<div class="conv" data-held="{1 if held else 0}" data-nosrc="{1 if nosrc else 0}">
 <header class="ch"><span class="cid">#{i}</span>
  <span class="note" style="margin:0">{esc(asked[:90])}</span></header>
 <div class="rows">
  <div class="row user"><div class="bub ub" dir="{rtl(asked)}">
   <div class="who">المستخدم · User</div>{esc(asked)}</div></div>
  <div class="row bot"><div class="bub bb" dir="{rtl(answer)}">
   <div class="who">الوكيل · Agent</div>{body}{pan}
   <div class="chips">{"".join(chips)}</div></div></div>
 </div>
</div>"""


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: render_report.py <results.json> [--out DIR]")
    src = pathlib.Path(sys.argv[1]).resolve()
    # Default to the working directory, NOT the json's parent — otherwise writing the
    # results into reports/studio/ produces reports/studio/reports/studio/<agent>/.
    root = pathlib.Path(sys.argv[sys.argv.index("--out") + 1]) if "--out" in sys.argv \
        else pathlib.Path.cwd()

    data = json.loads(src.read_text(encoding="utf-8"))
    # Accept both shapes: a bare list (older runs) and {meta, results}.
    if isinstance(data, list):
        meta, results = {}, data
    else:
        meta, results = data.get("meta") or {}, data.get("results") or []
    if not results:
        sys.exit(f"no results in {src}")

    agent_id = meta.get("agent_id") or "unknown-agent"
    out_dir = root / "reports" / "studio" / agent_id
    out_dir.mkdir(parents=True, exist_ok=True)
    page_path = out_dir / "report.html"

    held = sum(1 for r in results if "Withheld" in (r.get("answer") or "")
               or not (r.get("answer") or "").strip())
    notiso = sum(1 for r in results
                 if (r.get("question") or "").strip() != (r.get("asked") or "").strip())
    nosrc = sum(1 for r in results
                if (r.get("answer") or "").strip() and "Withheld" not in (r.get("answer") or "")
                and not (r.get("sources") or []))

    strip = "".join(
        f"<span>{esc(k.replace('_', ' '))}: <b>{esc(v)}</b></span>"
        for k, v in (("tenant", meta.get("tenant")), ("agent", meta.get("agent")),
                     ("knowledge base", meta.get("knowledge_base")),
                     ("model", meta.get("model")), ("agent id", agent_id))
        if v)

    warn = ""
    if notiso:
        warn = (f'<p class="note" style="color:var(--warn)">⚠ {notiso} conversation(s) '
                f'were NOT isolated — those answers carry earlier context.</p>')

    filters = "".join([
        f'<button class="f on" data-v="all">all ({len(results)})</button>',
        f'<button class="f" data-v="answered">answered ({len(results) - held})</button>',
        f'<button class="f" data-v="held">withheld ({held})</button>',
        f'<button class="f" data-v="nosrc">0 sources ({nosrc})</button>',
    ])

    page = f"""<!doctype html>
<html lang="ar"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent Studio — playground review</title>
<style>{CSS}</style></head><body>
<header class="top">
 <h1>Agent Studio — playground review</h1>
 <div class="sub">{len(results)} conversations, one question each ·
  showing <span id="shown">{len(results)}</span></div>
 <div class="meta-strip">{strip}</div>
 {warn}
 <p class="note">Playground run — <b>ignores the rollout percentage</b>, not saved to
  history, not billed. Not evidence of production behaviour.</p>
 <div class="controls">{filters}
  <input id="q" placeholder="search all turns… (Arabic or English)">
 </div>
</header>
<main>
{"".join(card(i, r) for i, r in enumerate(results, 1))}
<div id="empty" class="hidden">no conversation matches that filter</div>
</main>
<script>{JS}</script>
</body></html>"""

    page_path.write_text(page, encoding="utf-8")
    print(f"wrote {page_path}  ({page_path.stat().st_size // 1024} KB)")
    print(f"REPORT URL: {page_path.resolve().as_uri()}")


if __name__ == "__main__":
    main()
