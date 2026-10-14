#!/usr/bin/env python3
"""Render a production-only HTML report straight from a pull.json — no replay involved.

`render_replay_html.py` needs a replay's state.json because it draws two columns. When
you only pulled production and are not spending LLM calls to replay it, there is nothing
to compare against and that renderer refuses. This one reads the pull alone and lays the
production conversations out for reading: what citizens asked, what the live bot said.

It is a reading surface, not a grader. The refusal / handoff / unanswered flags are a
reading order — open those first — and nothing more. Masking, refusal detection and the
page styling are imported from render_replay_html so there is exactly one definition of
each; in particular the PII patterns are NOT redefined here (see that module's warning
about \\b and Arabic).

usage: render_pull_html.py <pull.json | pull dir>   [-o OUTPUT.html]
"""

import html
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.render_replay_html import (  # noqa: E402
    CSS, asked_question, direction, is_refusal, mask, norm, rich,
)

# Sidebar filters. Kept deliberately few — a production read-through has no similarity
# score to slice on, so the only useful cuts are "who was left with nothing" and "who
# ended up with a human".
FILTERS = [("all", "All"), ("refusal", "Refusals"), ("handoff", "Handoff"),
           ("unanswered", "No reply"), ("multi", "Multi-turn")]


def window_label(win: dict) -> str:
    """Name the report directory after the window it actually covers.

    A `--day` pull carries `day`; a `--start/--end` pull carries `day: None`, and dating
    that one by its start date alone would make two different windows on the same date
    collide on one output directory — the second render would silently overwrite the
    first. Mirrors the puller's own labelling for exactly that reason.
    """
    if win.get("day"):
        return str(win["day"])
    start, end = str(win.get("start") or ""), str(win.get("end") or "")
    if not start or not end:
        return "pull"
    s = f"{start[:10]}T{start[11:16].replace(':', '')}"
    return (f"{s}-{end[11:16].replace(':', '')}Z" if start[:10] == end[:10]
            else f"{s}_to_{end[:10]}T{end[11:16].replace(':', '')}Z")


def turn_rows(conv: dict) -> list[dict]:
    """Pair each user message with the assistant text that followed it.

    Consecutive assistant messages are joined: the API splits a single answer across
    frames often enough that treating each as its own turn invents turns that never
    happened.
    """
    rows: list[dict] = []
    for m in conv.get("messages") or []:
        role, text = m.get("role"), m.get("content") or ""
        if role == "user":
            rows.append({"user": text, "bot": "", "at": m.get("created_at")})
        elif role == "assistant":
            if not rows:  # bot spoke first (greeting) — keep it, with no user turn
                rows.append({"user": "", "bot": "", "at": m.get("created_at")})
            rows[-1]["bot"] = (rows[-1]["bot"] + "\n\n" + text).strip() if rows[-1]["bot"] \
                else text
    return rows


def classify(conv: dict, rows: list[dict]) -> dict:
    tags: list[str] = []
    refusals = sum(1 for r in rows if is_refusal(r["bot"]))
    if refusals:
        tags.append(f"refusal×{refusals}" if refusals > 1 else "refusal")
    if any(r["user"] and not norm(r["bot"]) for r in rows):
        tags.append("no-reply")
    if conv.get("handoff_requested_at"):
        res = conv.get("handoff_resolution_type")
        tags.append(f"handoff:{res}" if res else "handoff")
    # The last thing said was a question back at the user, and the transcript ends there
    # — they never answered it. Worth reading: the bot may have stalled on a detail it
    # did not actually need.
    if rows and asked_question(rows[-1]["bot"]):
        tags.append("left-hanging")
    return {"tags": tags, "refusals": refusals}


def tag_html(tags: list[str]) -> str:
    def cls(t: str) -> str:
        if t.startswith("refusal") or t == "no-reply":
            return "tag bad"
        if t.startswith("handoff") or t == "left-hanging":
            return "tag warn"
        return "tag"
    return "".join(f'<span class="{cls(t)}">{html.escape(t)}</span>' for t in tags)


def conv_page(idx: int, total: int, conv: dict, rows: list[dict], cls: dict) -> str:
    opening = norm(conv["user_turns"][0]) if conv.get("user_turns") else \
        str(conv.get("title") or "(untitled)")
    turns = "".join(
        f'''<section class="turn">
  <div class="turnbar"><span class="turnno">Turn {i}<span class="of"> / {len(rows)}</span></span>
    <span class="pill {'bad' if is_refusal(r['bot']) else 'quiet'}">
      {'refusal' if is_refusal(r['bot']) else (r.get('at') or '')[11:16] or '—'}</span></div>
  <div class="user" dir="{direction(r['user'])}"><span class="lbl">User</span>{rich(r['user'])}</div>
  <div class="col prod" dir="{direction(r['bot'])}">
    <div class="colhead"><span class="dot p"></span>Production reply</div>
    <div class="ans">{rich(r['bot'])}</div></div>
</section>''' for i, r in enumerate(rows, 1))

    prev = f'<a class="nav" href="#c{idx-1}">‹ Previous</a>' if idx > 1 else "<span></span>"
    nxt = f'<a class="nav" href="#c{idx+1}">Next ›</a>' if idx < total else "<span></span>"
    return f'''<div class="page" id="page-c{idx}">
  <div class="pagehead">
    <div class="ph-top"><span class="counter">Conversation {idx} of {total}</span></div>
    <h2 dir="{direction(opening)}">{html.escape(mask(opening))}</h2>
    <div class="tags">{tag_html(cls["tags"])}</div>
    <p class="ids">{len(rows)} turn(s) · started <code>{html.escape(str(conv.get("created_at") or "—"))}</code>
      · status <code>{html.escape(str(conv.get("status") or "—"))}</code>
      · id <code>{html.escape(str(conv.get("prod_conversation_id") or "—"))}</code></p>
  </div>
  {turns}
  <div class="pagenav">{prev}{nxt}</div>
</div>'''


def overview_page(pull: dict, index_rows: list[dict], stats: dict, pull_path: Path) -> str:
    cfg = pull.get("prod_config") or {}
    agent, bot = cfg.get("agent") or {}, cfg.get("bot") or {}
    win = pull.get("window") or {}
    sel = pull.get("selection") or {}

    cfg_rows = "".join(
        f"<tr><td>{html.escape(k)}</td><td>{html.escape(str(v))}</td></tr>"
        for k, v in [
            ("tenant", pull.get("tenant")),
            ("bot", f'{bot.get("name")} ({bot.get("channel_type")})'),
            ("agent", agent.get("name")),
            ("model", agent.get("model")),
            ("temperature", agent.get("temperature")),
            ("top_k", agent.get("top_k")),
            ("agentic_rollout_percentage", bot.get("agentic_rollout_percentage")),
        ] if v is not None)

    flag_rows = "".join(
        f'<span class="tag {"bad" if t.startswith(("refusal", "no-reply")) else "warn"}">'
        f'{html.escape(t)} · {n}</span>'
        for t, n in sorted(stats["flags"].items(), key=lambda kv: -kv[1])) or \
        '<span class="tag">none</span>'

    idx_rows = "".join(
        f'<tr onclick="location.hash=\'c{r["idx"]}\'">'
        f'<td>{r["idx"]}</td><td class="t" dir="{direction(r["title"])}">'
        f'{html.escape(r["title"])}</td><td>{r["turns"]}</td>'
        f'<td class="tg">{tag_html(r["tags"])}</td></tr>' for r in index_rows)

    return f'''<div class="page" id="page-overview">
  <div class="pagehead">
    <div class="ph-top"><span class="counter">Overview</span></div>
    <h2>Production conversations — {html.escape(window_label(win))}</h2>
    <p class="lede">{html.escape(str(pull.get("tenant")))} ·
      {html.escape(str(win.get("start")))} to {html.escape(str(win.get("end")))}
      <b>({html.escape(str(win.get("timezone") or "UTC"))} day)</b> ·
      selection <code>{html.escape(json.dumps(sel, ensure_ascii=False))}</code></p>
    <p class="banner">Read-only pull. Nothing was sent to production, and no replay was
      run — there is no testing side in this report.</p>
  </div>

  <ul class="stats">
    <li><b>{pull.get("found_in_window")}</b>found in window</li>
    <li><b>{pull.get("kept")}</b>kept</li>
    <li><b>{stats["turns"]}</b>user turns</li>
    <li><b>{stats["refusal_convs"]}</b>conversations with a refusal</li>
    <li><b>{stats["refusal_rate"]:.0f}%</b>refusal rate (conversations)</li>
    <li><b>{stats["handoffs"]}</b>handoffs</li>
    <li><b>{stats["mean_turns"]:.1f}</b>mean turns</li>
  </ul>
  <div class="flags tags">{flag_rows}</div>

  <h3>Production configuration at pull time</h3>
  <table class="cfgt"><thead><tr><th>Field</th><th>Value</th></tr></thead>
    <tbody>{cfg_rows}</tbody></table>

  <h3>All conversations</h3>
  <table class="idx"><thead><tr><th>#</th><th>Opening message</th><th>Turns</th>
    <th>Flags</th></tr></thead><tbody>{idx_rows}</tbody></table>

  <p class="meta-line">Pulled {html.escape(str(pull.get("pulled_at")))} ·
    source <code>{html.escape(str(pull_path))}</code> ·
    skipped below min-turns: {pull.get("skipped_below_min_turns")} ·
    transcript failures: {pull.get("transcript_failures")}</p>
  <p class="note">Identifiers are masked for display. The pull itself is unredacted by
    design — quote from this report, never from pull.json.</p>
</div>'''


JS = """
const pages=[...document.querySelectorAll('.page')];
const links=[...document.querySelectorAll('nav a')];
function show(id){
  pages.forEach(p=>p.classList.toggle('on', p.id==='page-'+id));
  links.forEach(a=>a.classList.toggle('on', a.dataset.page===id));
  const on=links.find(a=>a.dataset.page===id);
  if(on) on.scrollIntoView({block:'nearest'});
  window.scrollTo(0,0);
}
function route(){ show((location.hash||'#overview').slice(1)); }
window.addEventListener('hashchange',route);

let mode='all';
function applyFilter(){
  const q=document.getElementById('q').value.trim().toLowerCase();
  let n=0;
  links.forEach(a=>{
    if(a.dataset.page==='overview') return;
    const tags=a.dataset.tags||'';
    let ok=true;
    if(mode==='refusal') ok=tags.includes('refusal');
    else if(mode==='handoff') ok=tags.includes('handoff');
    else if(mode==='unanswered') ok=tags.includes('no-reply');
    else if(mode==='multi') ok=parseInt(a.dataset.turns||'0',10)>1;
    if(ok&&q){
      const pg=document.getElementById('page-'+a.dataset.page);
      ok=(a.textContent+' '+(pg?pg.textContent:'')).toLowerCase().includes(q);
    }
    a.style.display=ok?'':'none';
    if(ok) n++;
  });
  document.getElementById('shown').textContent=n;
}
document.querySelectorAll('.fbtns button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('.fbtns button').forEach(x=>x.classList.remove('on'));
  b.classList.add('on'); mode=b.dataset.m; applyFilter();
});
document.getElementById('q').oninput=applyFilter;

document.addEventListener('keydown',e=>{
  if(e.target.tagName==='INPUT') return;
  const vis=links.filter(a=>a.style.display!=='none'&&a.dataset.page!=='overview');
  const cur=vis.findIndex(a=>a.classList.contains('on'));
  if(e.key==='j'&&cur<vis.length-1) location.hash=vis[cur+1].dataset.page;
  if(e.key==='k'&&cur>0) location.hash=vis[cur-1].dataset.page;
});

applyFilter(); route();
"""


def main() -> None:
    argv = [a for a in sys.argv[1:]]
    if not argv:
        sys.exit("usage: render_pull_html.py <pull.json | pull dir> [-o OUTPUT.html]")
    out_override = None
    if "-o" in argv:
        i = argv.index("-o")
        out_override = Path(argv[i + 1])
        del argv[i:i + 2]

    target = Path(argv[0]).resolve()
    pull_path = target / "pull.json" if target.is_dir() else target
    if not pull_path.exists():
        sys.exit(f"no pull.json at {pull_path} — run pull_prod_conversations.py first.")

    pull = json.loads(pull_path.read_text(encoding="utf-8"))
    convs = sorted(pull.get("conversations") or [], key=lambda c: c.get("created_at") or "")
    if not convs:
        sys.exit(f"{pull_path} holds no conversations.")

    pages, index_rows = [], []
    flags: dict[str, int] = {}
    turns_total = refusal_convs = handoffs = 0
    for i, conv in enumerate(convs, 1):
        rows = turn_rows(conv)
        cls = classify(conv, rows)
        turns_total += len(conv.get("user_turns") or [])
        if cls["refusals"]:
            refusal_convs += 1
        if conv.get("handoff_requested_at"):
            handoffs += 1
        for t in cls["tags"]:
            key = t.split("×")[0].split(":")[0]
            flags[key] = flags.get(key, 0) + 1
        pages.append(conv_page(i, len(convs), conv, rows, cls))
        opening = norm(conv["user_turns"][0]) if conv.get("user_turns") else \
            str(conv.get("title") or "(untitled)")
        index_rows.append({"idx": i, "title": mask(opening), "turns": len(rows),
                           "tags": cls["tags"]})

    stats = {
        "turns": turns_total,
        "refusal_convs": refusal_convs,
        "refusal_rate": 100.0 * refusal_convs / len(convs),
        "handoffs": handoffs,
        "mean_turns": turns_total / len(convs),
        "flags": flags,
    }

    day = window_label(pull.get("window") or {})
    nav = ['<a href="#overview" data-page="overview">Overview</a>',
           '<div class="sechead">Conversations</div>']
    for r in index_rows:
        tags = " ".join(r["tags"])
        nav.append(
            f'<a href="#c{r["idx"]}" data-page="c{r["idx"]}" data-tags="{html.escape(tags)}" '
            f'data-turns="{r["turns"]}">'
            f'<span class="nrow"><span class="ntitle" dir="{direction(r["title"])}">'
            f'{r["idx"]}. {html.escape(r["title"])}</span>'
            f'<span class="nsim">{r["turns"]}t</span></span>'
            f'<span class="nflag">{html.escape(tags) or "&nbsp;"}</span></a>')

    fbtns = "".join(
        f'<button data-m="{k}"{" class=\"on\"" if k == "all" else ""}>{lbl}</button>'
        for k, lbl in FILTERS)

    doc = f'''<!doctype html><html lang="ar" dir="ltr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Production pull {html.escape(day)} — Tarekat-Restricted</title>
<style>{CSS}</style></head><body>
<div class="app">
  <aside class="side">
    <div class="brand"><b>Production pull</b><span>{html.escape(day)} · restricted ·
      <span id="shown">{len(convs)}</span>/{len(convs)} shown</span></div>
    <div class="sidetools"><input id="q" placeholder="Search conversations…">
      <div class="fbtns">{fbtns}</div></div>
    <nav>{"".join(nav)}</nav>
  </aside>
  <main>{overview_page(pull, index_rows, stats, pull_path)}{"".join(pages)}</main>
</div>
<script>{JS}</script></body></html>'''

    out = out_override or (Path("reports/prod_pulls") / day / "report.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8")
    print(f"conversations {len(convs)} · user turns {turns_total} · "
          f"refusal conversations {refusal_convs} ({stats['refusal_rate']:.0f}%) · "
          f"handoffs {handoffs}")
    print(f"REPORT URL: file://{out.resolve()}")


if __name__ == "__main__":
    main()
