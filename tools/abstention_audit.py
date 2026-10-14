"""Audit how often a tenant's bot punts instead of answering — the effectiveness question.

    .venv/bin/python tools/abstention_audit.py --tenant restricted --day 2026-09-03
    .venv/bin/python tools/abstention_audit.py --tenant restricted \
        --start 2026-09-03T09:00:00Z --end 2026-09-03T12:00:00Z

Finds every conversation holding at least one ASSISTANT message that says the canned
"I don't have enough information" line, and reports what share of real traffic that is,
which citizens were left with nothing, and which questions the knowledge base cannot
answer. Read-only: this module only ever issues admin-API GETs, against whichever tenant
you name. It never sends a message to anything.

Output: reports/abstention/<tenant>/<label>/ — audit.json (raw) + report.html (masked).

WHY THE DENOMINATOR IS THE POINT. "14 conversations abstained" is not a finding; "14 of
16" is. The unfiltered window count is therefore always fetched, and every rate in the
report is drawn against it. A run that reported only the numerator would let a quiet hour
and a broken knowledge base look identical.

MATCHING IS ON A STEM, NOT THE WHOLE SENTENCE. The default phrase is the stable opening
`لا أملك معلومات كافية` rather than the full `…للإجابة على ذلك.` — trailing punctuation
and follow-on clauses vary between agent versions, and a full-sentence match silently
drops the variants. Every distinct exact form that matched is counted and listed in the
report, so widening the net never hides what was actually said. Pass --phrase to pin an
exact string.

THE SERVER-SIDE FILTER IS EXACT BUT ROLE-BLIND. `q=` on /conversation-summaries is an
ILIKE over message content and does match assistant text (verified 2026-09-03 against
restricted 09:55–11:55Z: q returned 14, hand-counting transcripts returned the same 14).
But it cannot tell an assistant message from a user one, so a citizen quoting the phrase
back would be a false positive. Every candidate is therefore re-checked on the transcript
with role == "assistant" before it counts. Use --matched-only to lean on the filter for
speed on very large windows; the turn-level denominator is unavailable in that mode and
the report says so rather than estimating it.

Date-only bounds silently return zero — the API compares against a timestamp, so
`--day` is built into full ISO bounds via day_bounds(). The window is a UTC day; Riyadh
is UTC+3, so 01:00 local belongs to the previous UTC day.

No tenant selected -> 404 on a valid id: the studio account is a super-admin with
tenant_id null, so every call passes tenant_id explicitly. A 404 here means wrong
tenant, not missing data.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.studio_admin import (StudioAdmin, day_bounds,  # noqa: E402
                                resolve_tenant, TENANTS)
from render_replay_html import ARABIC, mask  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
AUDIT_ROOT = ROOT / "reports" / "abstention"

# The stable stem of the canned abstention. See the module docstring for why this is
# not the full sentence.
DEFAULT_PHRASE = "لا أملك معلومات كافية"

# Conversation-level abstention rate -> status role. Thresholds are for triage colour
# only; they are not a verdict on the bot.
BANDS = [(0.20, "good", "◆"), (0.40, "warning", "▲"), (0.60, "serious", "▲")]


def tenant_label(tid: str) -> str:
    for name, val in TENANTS.items():
        if val == tid:
            return name
    return tid[:8]


def window_label(start: str, end: str, day: str | None) -> str:
    """A windowed run must carry its TIMES, or two windows on one day collide."""
    if day:
        return day
    return (f"{start[:10]}T{start[11:16].replace(':', '')}"
            f"-{end[11:16].replace(':', '')}Z")


def norm(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "")).strip()


def band(rate: float) -> tuple[str, str]:
    for limit, role, icon in BANDS:
        if rate < limit:
            return role, icon
    return "critical", "■"


# ---- collection --------------------------------------------------------------

def conv_id(summary: dict) -> str:
    return summary.get("id") or summary.get("conversation_id") or ""


def analyse(tr: dict, phrase: str) -> dict:
    """Turn one transcript into the per-conversation facts the report needs."""
    msgs = tr.get("messages") or []
    bot = [m for m in msgs if m.get("role") == "assistant"]
    hits = [i for i, m in enumerate(bot) if phrase in norm(m.get("content") or "")]

    # The user turn each abstention answered — this is the actionable knowledge-base gap.
    # Walk back from the assistant message to the nearest preceding user message.
    triggers: list[str] = []
    last_user = ""
    for m in msgs:
        if m.get("role") == "user":
            last_user = norm(m.get("content") or "")
        elif m.get("role") == "assistant" and phrase in norm(m.get("content") or ""):
            triggers.append(last_user)

    return {
        "conversation_id": tr.get("id"),
        "title": tr.get("title") or "",
        "status": tr.get("status"),
        "created_at": tr.get("created_at"),
        "last_message_at": tr.get("last_message_at"),
        "bot_name": tr.get("bot_name"),
        "agent_name": tr.get("agent_name"),
        "message_count": tr.get("message_count"),
        "handoff_requested_at": tr.get("handoff_requested_at"),
        "handoff_resolution_type": tr.get("handoff_resolution_type"),
        "assistant_msgs": len(bot),
        "abstentions": len(hits),
        # Every assistant turn was a punt — the citizen got nothing at all from this
        # conversation. The most damning single category in the report.
        "fully_abstained": bool(bot) and len(hits) == len(bot),
        # The bot's LAST word was a punt: whatever else happened, the user left without
        # an answer.
        "dead_end": bool(hits) and hits[-1] == len(bot) - 1,
        # It failed on first contact, before the conversation had a chance to narrow.
        "first_contact": bool(hits) and hits[0] == 0,
        "variants": [norm(bot[i].get("content") or "") for i in hits],
        "triggers": triggers,
        "messages": [{"role": m.get("role"), "content": m.get("content") or "",
                      "created_at": m.get("created_at")} for m in msgs],
    }


def collect(api: StudioAdmin, tid: str, start: str, end: str, phrase: str,
            include_test: bool, matched_only: bool, workers: int,
            limit: int | None) -> dict:
    everything = api.list_conversations(tid, start=start, end=end,
                                        include_test=include_test)
    total_in_window = len(everything)

    if matched_only:
        # Lean on the server filter. Conversation-level rates stay exact because the
        # denominator came from the unfiltered call above; turn-level ones cannot be
        # computed and are reported as unavailable rather than guessed.
        candidates = api.list_conversations(tid, start=start, end=end, q=phrase,
                                            include_test=include_test)
    else:
        candidates = everything

    if limit:
        candidates = candidates[:limit]

    ids = [conv_id(c) for c in candidates if conv_id(c)]
    print(f"found    {total_in_window} conversations in window; "
          f"fetching {len(ids)} transcript(s) with {workers} workers…")

    convs: list[dict] = []
    failures: list[str] = []

    def one(cid: str):
        try:
            return analyse(api.transcript(cid, tid), phrase)
        except Exception as exc:                                  # noqa: BLE001
            failures.append(f"{cid}: {exc}")
            return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for r in pool.map(one, ids):
            if r:
                convs.append(r)

    convs.sort(key=lambda c: (c["created_at"] or ""))
    return {"total_in_window": total_in_window, "conversations": convs,
            "transcript_failures": failures, "scanned": len(ids)}


# ---- statistics --------------------------------------------------------------

def summarise(data: dict, matched_only: bool) -> dict:
    convs = data["conversations"]
    hit = [c for c in convs if c["abstentions"]]
    total = data["total_in_window"]

    # In full mode every conversation in the window was fetched, so the turn-level
    # denominator is real. In matched-only mode we only ever saw the matching
    # conversations, so there is no honest denominator to divide by.
    assistant_total = sum(c["assistant_msgs"] for c in convs) if not matched_only else None
    abst_total = sum(c["abstentions"] for c in convs)

    triggers = Counter()
    for c in hit:
        for t in c["triggers"]:
            triggers[t or "(no preceding user message)"] += 1

    variants = Counter()
    for c in hit:
        for v in c["variants"]:
            variants[v] += 1

    return {
        "total_in_window": total,
        "scanned": data["scanned"],
        "convs_with_abstention": len(hit),
        "conv_rate": (len(hit) / total) if total else 0.0,
        "assistant_msgs_total": assistant_total,
        "abstention_msgs": abst_total,
        "turn_rate": (abst_total / assistant_total) if assistant_total else None,
        "fully_abstained": sum(1 for c in hit if c["fully_abstained"]),
        "dead_end": sum(1 for c in hit if c["dead_end"]),
        "first_contact": sum(1 for c in hit if c["first_contact"]),
        "repeat": sum(1 for c in hit if c["abstentions"] >= 2),
        "escalated_overall": sum(1 for c in convs if c["handoff_requested_at"]),
        "escalated_among_abstained": sum(1 for c in hit if c["handoff_requested_at"]),
        "clean_convs": (sum(1 for c in convs
                            if c["assistant_msgs"] and not c["abstentions"])
                        if not matched_only else None),
        "top_triggers": triggers.most_common(40),
        "variants": variants.most_common(),
    }


# ---- report ------------------------------------------------------------------

CSS = """
:root{color-scheme:light;--surface:#fcfcfb;--plane:#f9f9f7;--ink:#0b0b0b;
--ink2:#52514e;--muted:#898781;--rule:#e1e0d9;--ring:rgba(11,11,11,.10);
--good:#0ca30c;--warning:#fab219;--serious:#ec835a;--critical:#d03b3b;
--accent:#2a78d6;--track:#cde2fb}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){
color-scheme:dark;--surface:#1a1a19;--plane:#0d0d0d;--ink:#fff;--ink2:#c3c2b7;
--muted:#898781;--rule:#2c2c2a;--ring:rgba(255,255,255,.10);--accent:#3987e5;
--track:#184f95}}
:root[data-theme="dark"]{color-scheme:dark;--surface:#1a1a19;--plane:#0d0d0d;
--ink:#fff;--ink2:#c3c2b7;--muted:#898781;--rule:#2c2c2a;
--ring:rgba(255,255,255,.10);--accent:#3987e5;--track:#184f95}
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink);
font:15px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1120px;margin:0 auto;padding:32px 20px 96px}
h1{font-size:22px;margin:0 0 4px}
h2{font-size:15px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);
margin:40px 0 12px;font-weight:600}
.sub{color:var(--ink2);font-size:13px;margin:0 0 24px}
.card{background:var(--surface);border:1px solid var(--ring);border-radius:10px;
padding:20px;margin-bottom:14px}
.hero{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
.hero .fig{font-size:60px;font-weight:600;line-height:1}
.hero .of{color:var(--ink2);font-size:15px}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:600;
padding:3px 10px;border-radius:99px;border:1px solid var(--ring)}
.meter{height:10px;background:var(--track);border-radius:99px;margin-top:16px;
overflow:hidden}
.meter>i{display:block;height:100%;border-radius:99px}
.tiles{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(168px,1fr))}
.tile{background:var(--surface);border:1px solid var(--ring);border-radius:10px;
padding:14px 16px}
.tile .lab{font-size:12px;color:var(--muted);display:block;margin-bottom:6px}
.tile .val{font-size:27px;font-weight:600;line-height:1.1}
.tile .note{font-size:12px;color:var(--ink2);margin-top:4px}
table{width:100%;border-collapse:collapse;font-size:14px;background:var(--surface);
border:1px solid var(--ring);border-radius:10px;overflow:hidden}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--rule);
vertical-align:top}
th{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;
font-weight:600}
tr:last-child td{border-bottom:0}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.rtl{direction:rtl;text-align:right}
details.conv{background:var(--surface);border:1px solid var(--ring);border-radius:10px;
margin-bottom:10px;overflow:hidden}
details.conv>summary{padding:13px 16px;cursor:pointer;display:flex;gap:10px;
align-items:center;flex-wrap:wrap;font-size:14px}
details.conv>summary::-webkit-details-marker{display:none}
details.conv>summary:hover{background:var(--plane)}
.tag{font-size:11px;font-weight:600;padding:2px 8px;border-radius:99px;
border:1px solid var(--ring);color:var(--ink2);white-space:nowrap}
.tag.bad{color:var(--critical);border-color:var(--critical)}
.tag.warn{color:var(--serious);border-color:var(--serious)}
.ttl{flex:1;min-width:200px;color:var(--ink2)}
.body{padding:4px 16px 16px;border-top:1px solid var(--rule)}
.msg{padding:10px 13px;border-radius:8px;margin:9px 0;border:1px solid var(--rule);
font-size:14px;white-space:pre-wrap}
.msg.user{background:var(--plane)}
.msg.bot{background:transparent}
.msg.abst{border-color:var(--critical);
box-shadow:inset 3px 0 0 var(--critical)}
.msg .who{font-size:11px;color:var(--muted);display:block;margin-bottom:5px;
text-transform:uppercase;letter-spacing:.05em;direction:ltr;text-align:left}
/* ---- paged shell -------------------------------------------------------
   Sidebar index + one page per conversation, replacing the single long
   scroll. Built on the same custom properties as everything above, so the
   dark-mode block keeps working without a second set of colours. */
.app{display:flex;min-height:100vh}
.side{width:300px;flex:0 0 300px;border-right:1px solid var(--rule);
background:var(--surface);position:sticky;top:0;height:100vh;
display:flex;flex-direction:column}
.brand{padding:16px 16px 12px;border-bottom:1px solid var(--rule)}
.brand b{display:block;font-size:14px}
.brand span{display:block;font-size:11.5px;color:var(--muted);margin-top:2px;
line-height:1.45}
.sidetools{padding:10px 12px;border-bottom:1px solid var(--rule)}
.sidetools input{width:100%;padding:6px 9px;border:1px solid var(--rule);
border-radius:7px;background:var(--plane);color:var(--ink);font:13px/1.4 inherit}
.fbtns{display:flex;flex-wrap:wrap;gap:5px;margin-top:8px}
.fbtns button{font:11.5px/1 inherit;padding:5px 9px;border-radius:99px;
border:1px solid var(--rule);background:transparent;color:var(--ink2);cursor:pointer}
.fbtns button.on{background:var(--accent);border-color:var(--accent);color:#fff}
nav{overflow-y:auto;flex:1;padding:6px 0 30px}
nav a{display:block;padding:8px 14px;text-decoration:none;color:var(--ink);
border-left:3px solid transparent;font-size:12.5px}
nav a:hover{background:var(--plane)}
nav a.on{background:var(--plane);border-left-color:var(--accent)}
nav .sechead{padding:12px 14px 5px;font-size:10.5px;text-transform:uppercase;
letter-spacing:.09em;color:var(--muted)}
nav .nrow{display:flex;justify-content:space-between;gap:8px;align-items:baseline}
nav .ntitle{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
nav .nsim{color:var(--muted);font-size:11px;white-space:nowrap;
font-variant-numeric:tabular-nums}
nav .nflag{display:block;color:var(--muted);font-size:10.5px;margin-top:2px;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
main{flex:1;min-width:0}
.page{display:none}
.page.on{display:block}
.pagehead{border-bottom:1px solid var(--rule);padding-bottom:14px;margin-bottom:18px}
.counter{font-size:11px;text-transform:uppercase;letter-spacing:.09em;
color:var(--muted)}
.pagehead h3{margin:6px 0 8px;font-size:19px;font-weight:600}
.ids{color:var(--muted);font-size:12px;margin:8px 0 0}
.pagenav{display:flex;justify-content:space-between;margin-top:34px;
padding-top:14px;border-top:1px solid var(--rule)}
.pagenav a{color:var(--accent);text-decoration:none;font-size:13px}
.idx{width:100%}
.idx tbody tr{cursor:pointer}
.idx tbody tr:hover{background:var(--surface)}
.idx td.t{max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* isolate: an Arabic phrase inside an LTR sentence otherwise gets bidi-reordered
   against its surroundings and reads as scrambled. */
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;
background:var(--plane);padding:1px 5px;border-radius:4px;unicode-bidi:isolate}
.note{font-size:13px;color:var(--ink2)}
.caveat{border-left:3px solid var(--warning);padding:2px 0 2px 14px;margin:14px 0}
"""



def dir_of(t: str) -> str:
    return "rtl" if ARABIC.search(t or "") else "ltr"


def esc(t: str) -> str:
    return html.escape(mask(t or ""))


def pct(x: float | None) -> str:
    return "—" if x is None else f"{100 * x:.0f}%"


def conv_flags(c: dict) -> list[str]:
    """Sidebar vocabulary. Mirrors the severity ladder in the report, worst first."""
    f = []
    if c["fully_abstained"]:
        f.append("fully-abstained")
    if c["dead_end"]:
        f.append("ended-on-punt")
    if c["first_contact"]:
        f.append("first-contact")
    if c["abstentions"] >= 2:
        f.append("repeat")
    if c["handoff_requested_at"]:
        f.append("escalated")
    return f


def flag_html(flags: list[str]) -> str:
    out = []
    for t in flags:
        cls = "bad" if t == "fully-abstained" else (
            "warn" if t in ("ended-on-punt", "first-contact", "repeat") else "")
        out.append(f'<span class="tag {cls}">{html.escape(t)}</span>')
    return "".join(out)


def conv_page(idx: int, total: int, c: dict, phrase: str) -> str:
    msgs = []
    for m in c["messages"]:
        body = m["content"]
        is_ab = m["role"] == "assistant" and phrase in norm(body)
        cls = "user" if m["role"] == "user" else "bot"
        who = "user" if m["role"] == "user" else "bot"
        if is_ab:
            cls += " abst"
            who += " · abstained"
        msgs.append(f'<div class="msg {cls} {dir_of(body)}">'
                    f'<span class="who">{who}</span>{esc(body)}</div>')

    prev = (f'<a href="#c{idx-1}">&lsaquo; Previous</a>' if idx > 1 else "<span></span>")
    nxt = (f'<a href="#c{idx+1}">Next &rsaquo;</a>' if idx < total else "<span></span>")

    return f'''<div class="page" id="page-c{idx}">
  <div class="pagehead">
    <span class="counter">Conversation {idx} of {total}</span>
    <h3 class="{dir_of(c["title"])}">{esc(c["title"]) or "—"}</h3>
    <div>{flag_html(conv_flags(c))}
      <span class="tag">{c["abstentions"]}/{c["assistant_msgs"]} bot turns punted</span></div>
    <p class="ids">started <code>{html.escape(str(c["created_at"] or "—"))}</code>
      · status <code>{html.escape(str(c["status"] or "—"))}</code>
      · {c["message_count"]} messages
      · id <code>{html.escape(str(c["conversation_id"] or "—"))}</code></p>
  </div>
  {"".join(msgs)}
  <div class="pagenav">{prev}{nxt}</div>
</div>'''


def overview_page(meta: dict, stats: dict, hit: list[dict], phrase: str,
                  tile_html: str, trig_rows: str, var_rows: str, cfg_rows: str,
                  caveat: str, role: str, icon: str) -> str:
    idx_rows = "".join(
        f'<tr onclick="location.hash=\'c{i}\'">'
        f'<td>{i}</td><td class="t {dir_of(c["title"])}">{esc(c["title"]) or "—"}</td>'
        f'<td class="n">{c["abstentions"]}/{c["assistant_msgs"]}</td>'
        f'<td>{flag_html(conv_flags(c))}</td></tr>'
        for i, c in enumerate(hit, 1)) or \
        '<tr><td colspan="4" class="note">No conversation in this window contained the phrase.</td></tr>'

    return f'''<div class="page" id="page-overview">
<span class="counter">Overview</span>
<h1>Abstention audit — {html.escape(meta["tenant"])}</h1>
<p class="sub">Window <strong>{html.escape(meta["window"]["start"])}</strong> ..
<strong>{html.escape(meta["window"]["end"])}</strong> (UTC) ·
matched on stem <code>{html.escape(phrase)}</code> ·
assistant messages only · generated {html.escape(meta["generated_at"])}<br>
Read-only audit of tenant <code>{html.escape(meta["tenant_id"])}</code> — no messages were sent.</p>

<div class="card">
  <div class="hero">
    <span class="fig" style="color:var(--{role})">{pct(stats["conv_rate"])}</span>
    <span class="of">of conversations contained at least one abstention
      — {stats["convs_with_abstention"]} of {stats["total_in_window"]}</span>
    <span class="pill" style="color:var(--{role});border-color:var(--{role})">
      {icon} {role}</span>
  </div>
  <div class="meter"><i style="width:{100 * stats['conv_rate']:.1f}%;
    background:var(--{role})"></i></div>
</div>
{caveat}

<h2>Breakdown</h2>
<div class="tiles">{tile_html}</div>

<h2>Questions the bot could not answer</h2>
<p class="note">The user turn immediately preceding each abstention — i.e. the
knowledge-base gap list, most frequent first.</p>
<table><thead><tr><th>User asked</th><th class="n">Times punted</th></tr></thead>
<tbody>{trig_rows}</tbody></table>

<h2>Exact wordings that matched</h2>
<p class="note">Every distinct assistant message matching the stem, so widening the
match never hides what was actually said.</p>
<table><thead><tr><th>Assistant said</th><th class="n">Count</th></tr></thead>
<tbody>{var_rows}</tbody></table>

{'<h2>Agent configuration at audit time</h2><table><thead><tr><th>Field</th><th class="n">Value</th></tr></thead><tbody>' + cfg_rows + '</tbody></table>' if cfg_rows else ''}

<h2>Abstaining conversations ({len(hit)})</h2>
<p class="note">Click a row, or pick one from the sidebar. Each opens on its own page.</p>
<table class="idx"><thead><tr><th>#</th><th>Conversation</th>
<th class="n">Punted</th><th>Flags</th></tr></thead><tbody>{idx_rows}</tbody></table>
</div>'''


PAGE_JS = """
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
    const flags=a.dataset.flags||'';
    let ok = (mode==='all') ? true : flags.split(' ').includes(mode);
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


def render(meta: dict, stats: dict, convs: list[dict], phrase: str,
           matched_only: bool) -> str:
    role, icon = band(stats["conv_rate"])
    hit = [c for c in convs if c["abstentions"]]

    tiles = [
        ("Conversations in window", f'{stats["total_in_window"]}',
         "the denominator for every rate here"),
        ("With ≥1 abstention", f'{stats["convs_with_abstention"]}',
         f'{pct(stats["conv_rate"])} of the window'),
        ("Fully abstained", f'{stats["fully_abstained"]}',
         "every bot turn was a punt — citizen got nothing"),
        ("Ended on a punt", f'{stats["dead_end"]}',
         "bot's last word was the canned line"),
        ("Failed on first contact", f'{stats["first_contact"]}',
         "first bot turn already abstained"),
        ("Abstained ≥2 times", f'{stats["repeat"]}',
         "user re-asked and got it again"),
        ("Abstention bot turns", f'{stats["abstention_msgs"]}',
         (f'{pct(stats["turn_rate"])} of all {stats["assistant_msgs_total"]} bot turns'
          if stats["turn_rate"] is not None else "turn-level rate needs a full scan")),
        ("Escalated to a human", f'{stats["escalated_among_abstained"]}',
         f'of {stats["escalated_overall"]} escalations in the whole window'),
    ]
    tile_html = "".join(
        f'<div class="tile"><span class="lab">{html.escape(l)}</span>'
        f'<div class="val">{v}</div><div class="note">{html.escape(n)}</div></div>'
        for l, v, n in tiles)

    trig_rows = "".join(
        f'<tr><td class="{dir_of(q)}">{esc(q)}</td><td class="n">{n}</td></tr>'
        for q, n in stats["top_triggers"]) or \
        '<tr><td colspan="2" class="note">none</td></tr>'

    var_rows = "".join(
        f'<tr><td class="{dir_of(v)}">{esc(v)}</td><td class="n">{n}</td></tr>'
        for v, n in stats["variants"]) or \
        '<tr><td colspan="2" class="note">none</td></tr>'

    agent = meta.get("config", {}).get("agent") or {}
    cfg_rows = "".join(
        f'<tr><td>{html.escape(k)}</td><td class="n">{html.escape(json.dumps(agent[k], ensure_ascii=False)[:80])}</td></tr>'
        for k in ("model", "model_id", "temperature", "top_k", "knowledge_base_ids",
                  "faithfulness_check", "faithfulness_threshold", "min_score_to_answer",
                  "relevance_threshold", "strict_grounding", "require_citations",
                  "clarification_enabled")
        if k in agent)

    caveat = ""
    if matched_only:
        caveat = ('<div class="caveat note"><strong>Matched-only scan.</strong> Only the '
                  'conversations the server filter matched were fetched, so the '
                  'conversation-level rate is exact but there is no turn-level '
                  'denominator — \u201cabstention bot turns\u201d has no percentage. Re-run '
                  'without <code>--matched-only</code> for turn-level rates.')

    # One page per abstaining conversation, plus the overview. Only the routed
    # page is displayed; the sidebar is the index into them.
    pages = "".join(conv_page(i, len(hit), c, phrase) for i, c in enumerate(hit, 1))

    nav = ['<a href="#overview" data-page="overview">Overview</a>',
           '<div class="sechead">Abstaining conversations</div>']
    for i, c in enumerate(hit, 1):
        flags = " ".join(conv_flags(c))
        title = esc(c["title"]) or "—"
        nav.append(
            f'<a href="#c{i}" data-page="c{i}" data-flags="{html.escape(flags)}">'
            f'<span class="nrow"><span class="ntitle {dir_of(c["title"])}">{i}. {title}</span>'
            f'<span class="nsim">{c["abstentions"]}/{c["assistant_msgs"]}</span></span>'
            f'<span class="nflag">{html.escape(flags) or "&nbsp;"}</span></a>')

    filters = [("all", f'All {len(hit)}'),
               ("fully-abstained", f'Fully abstained {stats["fully_abstained"]}'),
               ("ended-on-punt", f'Ended on a punt {stats["dead_end"]}'),
               ("first-contact", f'First contact {stats["first_contact"]}'),
               ("repeat", f'Repeat {stats["repeat"]}'),
               ("escalated", f'Escalated {stats["escalated_among_abstained"]}')]
    fbtns = "".join(
        f'<button data-m="{k}"{" class=\"on\"" if k == "all" else ""}>{html.escape(lbl)}</button>'
        for k, lbl in filters)

    overview = overview_page(meta, stats, hit, phrase, tile_html, trig_rows,
                             var_rows, cfg_rows, caveat, role, icon)

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Abstention audit — {html.escape(meta["tenant"])} — {html.escape(meta["label"])}</title>
<style>{CSS}</style></head><body>
<div class="app">
  <aside class="side">
    <div class="brand"><b>Abstention audit</b><span>{html.escape(meta["tenant"])} ·
      {html.escape(meta["label"])} · <span id="shown">{len(hit)}</span>/{len(hit)} shown</span></div>
    <div class="sidetools"><input id="q" placeholder="Search conversations…">
      <div class="fbtns">{fbtns}</div></div>
    <nav>{"".join(nav)}</nav>
  </aside>
  <main><div class="wrap">{overview}{pages}</div></main>
</div>
<script>{PAGE_JS}</script></body></html>
"""


# ---- cli ---------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Audit a tenant's canned-abstention rate over a time window.")
    ap.add_argument("--tenant", required=True,
                    help=f"name or uuid; known names: {', '.join(TENANTS)}")
    ap.add_argument("--day", help="UTC day, YYYY-MM-DD")
    ap.add_argument("--start", help="explicit ISO lower bound (overrides --day)")
    ap.add_argument("--end", help="explicit ISO upper bound (overrides --day)")
    ap.add_argument("--last-hours", type=float,
                    help="window ending now, e.g. 2 for the last 2 hours")
    ap.add_argument("--phrase", default=DEFAULT_PHRASE,
                    help="text an ASSISTANT message must contain (default: the canned "
                         "abstention stem)")
    ap.add_argument("--limit", type=int, help="cap transcripts fetched")
    ap.add_argument("--matched-only", action="store_true",
                    help="fetch only server-filter matches; faster, no turn-level rate")
    ap.add_argument("--include-test", action="store_true",
                    help="also include playground traffic (default: real users only)")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--count-only", action="store_true",
                    help="report how many conversations match, fetch no transcripts")
    ap.add_argument("--fresh", action="store_true", help="overwrite an existing audit")
    args = ap.parse_args()

    if not args.day and not (args.start and args.end) and not args.last_hours:
        ap.error("give --day, or --start and --end, or --last-hours")

    if args.last_hours:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        start = (now - timedelta(hours=args.last_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        day = None
    elif args.start and args.end:
        start, end, day = args.start, args.end, None
    else:
        start, end = day_bounds(args.day)
        day = args.day

    tid = resolve_tenant(args.tenant)
    tname = tenant_label(tid)
    label = window_label(start, end, day)

    print(f"tenant   {tname} ({tid})")
    print(f"window   {start}  ..  {end}   [UTC]")
    print(f"phrase   {args.phrase}")

    with StudioAdmin() as api:
        if args.count_only:
            total = len(api.list_conversations(tid, start=start, end=end,
                                               include_test=args.include_test))
            matched = len(api.list_conversations(tid, start=start, end=end,
                                                 q=args.phrase,
                                                 include_test=args.include_test))
            print(f"found    {total} conversations in window")
            print(f"match    {matched} contain the phrase "
                  f"({100 * matched / total:.0f}% of the window)" if total
                  else "match    0")
            print("note     server filter is role-blind; a full run re-checks "
                  "role==assistant before counting.")
            return

        out_dir = AUDIT_ROOT / tname / label
        out_file = out_dir / "audit.json"
        if out_file.exists() and not args.fresh:
            sys.exit(f"refusing to clobber {out_file} — pass --fresh to overwrite.")

        data = collect(api, tid, start, end, args.phrase, args.include_test,
                       args.matched_only, args.workers, args.limit)
        cfg = {}
        try:
            cfg = api.config_snapshot(tid)
        except Exception as exc:                                      # noqa: BLE001
            print(f"  (config snapshot unavailable: {exc})")

    stats = summarise(data, args.matched_only)
    meta = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tenant": tname, "tenant_id": tid, "label": label,
        "window": {"start": start, "end": end, "day": day, "timezone": "UTC"},
        "phrase": args.phrase, "matched_only": args.matched_only,
        "include_test": args.include_test, "config": cfg,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(
        {"meta": meta, "stats": stats, "conversations": data["conversations"],
         "transcript_failures": data["transcript_failures"]},
        ensure_ascii=False, indent=2), encoding="utf-8")

    report = out_dir / "report.html"
    report.write_text(render(meta, stats, data["conversations"], args.phrase,
                             args.matched_only), encoding="utf-8")

    print()
    print(f"scanned  {stats['scanned']} transcript(s) of "
          f"{stats['total_in_window']} in window")
    print(f"abstain  {stats['convs_with_abstention']} conversations "
          f"({pct(stats['conv_rate'])}) · {stats['abstention_msgs']} bot turns"
          + (f" ({pct(stats['turn_rate'])} of {stats['assistant_msgs_total']})"
             if stats["turn_rate"] is not None else ""))
    print(f"severe   {stats['fully_abstained']} fully abstained · "
          f"{stats['dead_end']} ended on a punt · "
          f"{stats['first_contact']} failed on first contact")
    if data["transcript_failures"]:
        print(f"!! {len(data['transcript_failures'])} transcript fetch(es) FAILED — "
              f"counts below are incomplete:")
        for f in data["transcript_failures"][:5]:
            print(f"   {f}")
    print()
    print(f"AUDIT FILE: {out_file}")
    print(f"REPORT URL: file://{report}")


if __name__ == "__main__":
    main()
