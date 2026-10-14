#!/usr/bin/env python3
"""Audit human-handoff service levels for a tenant over a UTC window.

Answers four questions about conversations the bot escalated to a human:

  1. How many conversations requested a handoff at all?
  2. How long did the citizen wait before an ops agent first replied?
  3. Once ops engaged, how long did the handoff run?
  4. How many requests **expired** — nobody ever came?

WHERE THE NUMBERS COME FROM, and why they are derived rather than read
---------------------------------------------------------------------
The admin API exposes exactly two handoff fields per conversation:
`handoff_requested_at` and `handoff_resolution_type`. There is NO `claimed_at`,
no `resolved_at`, and no per-conversation duration anywhere in the contract
(verified against the live openapi.json, 2026-09-13). So:

  * **Claim** is inferred from the transcript. Handoff transcripts carry a third
    role besides `user` and `assistant`: **`agent`** — a human ops reply. The
    first `agent` message at or after `handoff_requested_at` is the moment a
    human actually engaged. That is the closest observable proxy for a claim,
    and it is strictly *later* than the real claim: an agent who claims the
    ticket and reads for 30s before typing shows up here as a 30s-longer wait.
    Report it as **time-to-first-human-reply**, never as "claim latency".

  * **Duration** is the last `agent` message minus `handoff_requested_at`. An
    ops agent who resolves a ticket without typing a final message shortens
    this. It is a floor, not an exact handling time.

  * **Expiry** is not derived by us at all — the platform states it.
    `handoff_resolution_type == "timeout"` IS the request that aged out. On
    observed production data, 5 of 6 timeouts had zero `agent` messages, which
    is what "nobody came" looks like; the tool cross-checks that agreement and
    prints a warning if it ever stops holding.

Observed `handoff_resolution_type` values on Tarekat-Restricted:
    close (resolved by ops) · timeout (expired — nobody came) ·
    return_to_ai (handed back to the bot) · abandoned (user left) · null (still open)

`/api/v1/analytics/handoff/stats` reports the platform's own avg_wait_seconds and
avg_resolution_seconds, but takes a rolling `days=N` from *now* — it cannot be
scoped to a named day. When the requested window happens to be the last N whole
days, --cross-check fetches it and prints both side by side. Divergence is
expected (different definitions, different window edges) and is shown, not
reconciled.

LIMITATIONS worth stating in any report built on this
  * `agent_name` is populated only while a handoff is ACTIVE; it is null on every
    resolved conversation. Per-ops-agent breakdown is therefore not available
    historically, and this tool does not pretend to offer one.
  * `handoff_requested_at` is a single timestamp. A conversation escalated twice
    keeps only the latest, so a second cycle is invisible. Conversations whose
    only `agent` messages predate `handoff_requested_at` are counted as
    never-answered and flagged `earlier-cycle`.

Read-only: GET endpoints only. Nothing is written to any tenant.
"""

from __future__ import annotations

import argparse
import html
import json
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from tools.studio_admin import (StudioAdmin, day_bounds,  # noqa: E402
                                resolve_tenant, TENANTS)
from render_replay_html import CSS, direction, mask, rich  # noqa: E402

# The platform's own words for how a handoff ended. `timeout` is the one that
# means a citizen waited and nobody ever came.
EXPIRED = "timeout"
RESOLUTION_MEANING = {
    "close":        "ops resolved it",
    "timeout":      "EXPIRED — nobody came",
    "return_to_ai": "handed back to the bot",
    "abandoned":    "user left",
    "None":         "still open at pull time",
}

OUT_ROOT = ROOT / "reports" / "handoff_audits"


# ---- helpers -----------------------------------------------------------------

def T(ts: str | None) -> datetime | None:
    """Parse an API timestamp. They carry a trailing Z; make it tz-aware UTC."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def tenant_label(tid: str) -> str:
    for name, uuid in TENANTS.items():
        if uuid == tid:
            return name
    return tid


def window_label(start: str, end: str, day: str | None,
                 whole_days: bool = False) -> str:
    if day:
        return day
    s, e = T(start), T(end)
    if not s or not e:
        return "window"
    # A --days N window ends at "now", so a minute-resolution label would mint a
    # fresh directory on every run and the audits would sprawl. Name it by the
    # dates it covers instead, so re-running today overwrites today's audit.
    if whole_days:
        return f"last{(e.date() - s.date()).days + 1}d_{s:%Y-%m-%d}_to_{e:%Y-%m-%d}"
    return f"{s:%Y-%m-%dT%H%M}_to_{e:%Y-%m-%dT%H%M}Z"


def dur(seconds: float | None) -> str:
    """Human-readable duration. None means 'never happened', not 'zero'."""
    if seconds is None:
        return "—"
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def median(xs: list[float]) -> float | None:
    return statistics.median(xs) if xs else None


def pctile(xs: list[float], p: float) -> float | None:
    """Nearest-rank percentile. Small samples make p90 noisy; report n alongside."""
    if not xs:
        return None
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(round(p / 100 * len(s) + 0.5)) - 1))
    return s[k]


# ---- analysis ----------------------------------------------------------------

def analyse(tr: dict) -> dict:
    """Derive one conversation's handoff service levels from its transcript."""
    msgs = tr.get("messages") or []
    req_raw = tr.get("handoff_requested_at")
    req = T(req_raw)

    agent_msgs = [m for m in msgs if m.get("role") == "agent"]
    agent_times = [t for t in (T(m.get("created_at")) for m in agent_msgs) if t]
    after = sorted(t for t in agent_times if req and t >= req)

    wait = (after[0] - req).total_seconds() if after and req else None
    span = (after[-1] - req).total_seconds() if after and req else None

    # An agent spoke, but only BEFORE this handoff was requested — an earlier
    # cycle in the same conversation. Not an answer to this request.
    earlier_cycle = bool(agent_times) and not after

    res = tr.get("handoff_resolution_type")
    last = T(tr.get("last_message_at"))

    return {
        "conversation_id": tr.get("id"),
        "title": tr.get("title") or "",
        "status": tr.get("status"),
        "created_at": tr.get("created_at"),
        "last_message_at": tr.get("last_message_at"),
        "handoff_requested_at": req_raw,
        "resolution": res,
        "agent_name": tr.get("agent_name"),
        "message_count": tr.get("message_count"),
        "agent_msgs": len(agent_msgs),
        "agent_msgs_after_request": len(after),
        "wait_seconds": wait,
        "span_seconds": span,
        "earlier_cycle": earlier_cycle,
        "answered": bool(after),
        "expired": res == EXPIRED,
        # For a request nobody answered there is no waiting period to measure:
        # the transcript ends, and nothing records how long the citizen sat with
        # the widget open. The gap to the last message is the closest thing, and
        # it is only meaningful when POSITIVE. It frequently is not — the handoff
        # request is often written *after* the final bot message, which yields a
        # small negative number. Clamping that to zero would invent a measurement,
        # so it becomes None and the conversation is counted, not timed.
        "unanswered_seconds": (
            (last - req).total_seconds()
            if req and last and not after and (last - req).total_seconds() > 0
            else None),
        "messages": [{"role": m.get("role"), "content": m.get("content") or "",
                      "created_at": m.get("created_at")} for m in msgs],
    }


def collect(api: StudioAdmin, tid: str, start: str, end: str,
            include_test: bool, workers: int) -> dict:
    everything = api.list_conversations(tid, start=start, end=end,
                                        include_test=include_test)
    total = len(everything)

    # EVERY transcript has to be fetched. The summaries list does NOT carry
    # `handoff_requested_at` (verified 2026-09-13: its keys are id, title, status,
    # preview, counts and timestamps only), and `status` cannot stand in for it —
    # on observed production data 15 of 26 handoff conversations reported
    # `status: bot`, because a handoff that returned to the bot ends up back in
    # that state. Prefiltering on either one silently reports zero handoffs.
    # The fetch is GET-only and costs no LLM calls, so this is slow, not expensive.
    ids = [c.get("id") or c.get("conversation_id") for c in everything]
    ids = [i for i in ids if i]

    print(f"found    {total} conversations in window; fetching all "
          f"{len(ids)} transcripts with {workers} workers "
          f"(handoff state lives only in the transcript)…")

    convs: list[dict] = []
    failures: list[str] = []

    def one(cid: str):
        try:
            tr = api.transcript(cid, tid)
            return analyse(tr) if tr.get("handoff_requested_at") else None
        except Exception as exc:                                   # noqa: BLE001
            failures.append(f"{cid}: {exc}")
            return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for r in pool.map(one, ids):
            if r:
                convs.append(r)

    convs.sort(key=lambda c: (c["handoff_requested_at"] or ""))
    return {"total_in_window": total, "conversations": convs,
            "transcript_failures": failures}


def summarise(data: dict) -> dict:
    convs = data["conversations"]
    total = data["total_in_window"]
    n = len(convs)

    waits = [c["wait_seconds"] for c in convs if c["wait_seconds"] is not None]
    spans = [c["span_seconds"] for c in convs if c["span_seconds"] is not None]
    expired = [c for c in convs if c["expired"]]
    answered = [c for c in convs if c["answered"]]
    never = [c for c in convs if not c["answered"]]
    waited_for_nothing = [c["unanswered_seconds"] for c in never
                          if c["unanswered_seconds"] is not None]

    by_res: dict[str, int] = {}
    for c in convs:
        by_res[str(c["resolution"])] = by_res.get(str(c["resolution"]), 0) + 1

    # The consistency check behind the expiry metric: if the platform's
    # `timeout` stopped meaning "nobody came", every derived number below it
    # would quietly change meaning. Surface it rather than assume it holds.
    expired_with_reply = sum(1 for c in expired if c["answered"])

    return {
        "total_in_window": total,
        "handoff_conversations": n,
        "handoff_rate": (n / total) if total else 0.0,
        "answered": len(answered),
        "never_answered": len(never),
        "expired": len(expired),
        "expired_rate": (len(expired) / n) if n else 0.0,
        "expired_with_reply": expired_with_reply,
        "wait_n": len(waits),
        "wait_median": median(waits),
        "wait_avg": (sum(waits) / len(waits)) if waits else None,
        "wait_p90": pctile(waits, 90),
        "wait_max": max(waits) if waits else None,
        "span_n": len(spans),
        "span_median": median(spans),
        "span_avg": (sum(spans) / len(spans)) if spans else None,
        "span_max": max(spans) if spans else None,
        "abandoned_wait_median": median(waited_for_nothing),
        # Deliberately smaller than never_answered: the rest had no measurable
        # gap at all (the request was the last recorded event).
        "abandoned_wait_n": len(waited_for_nothing),
        "by_resolution": by_res,
    }


# ---- report ------------------------------------------------------------------
#
# Layout is the paged sidebar used by render_pull_html / render_replay_html: a
# nav of every handoff conversation on the left, one `.page` per conversation on
# the right, and only the routed page visible. The stylesheet is IMPORTED from
# render_replay_html rather than restated, so the three reports stay one family;
# EXTRA_CSS below adds only what a handoff report needs and no page styling that
# already exists there.

EXTRA_CSS = """
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
 gap:10px;margin:0 0 18px}
.card{background:#fff;border:1px solid #e3e6ea;border-radius:10px;padding:12px 14px}
.card .n{font-size:24px;font-weight:650;letter-spacing:-.5px}
.card .l{font-size:11.5px;color:#616a75;margin-top:2px;line-height:1.35}
.card.bad .n{color:#b3261e}
.card.warn .n{color:#9a6700}
.card.good .n{color:#1a7f37}
.timing{display:flex;gap:18px;flex-wrap:wrap;margin:10px 0 0;padding:10px 12px;
 background:#fff;border:1px solid #e3e6ea;border-radius:8px;font-size:12.5px}
.timing div{display:flex;flex-direction:column}
.timing .k{color:#616a75;font-size:11px;text-transform:uppercase;letter-spacing:.4px}
.timing .v{font-weight:600;font-variant-numeric:tabular-nums}
.timing .v.none{color:#b3261e}
.msg{border-radius:8px;padding:8px 12px;margin:7px 0;border:1px solid #e9ecf0;background:#fff}
.msg .who{font-size:10.5px;color:#616a75;text-transform:uppercase;letter-spacing:.5px;
 margin-bottom:3px;display:flex;justify-content:space-between}
.msg.user{background:#fafbfc}
.msg.assistant{background:#fff}
.msg.agent{background:#f1f8f3;border-color:#cfe6d6}
.msg.agent .who{color:#1a7f37;font-weight:600}
.reqline{display:flex;align-items:center;gap:10px;margin:14px 0 6px;
 color:#9a6700;font-size:12px;font-weight:600}
.reqline:before,.reqline:after{content:"";flex:1;height:1px;background:#e8d9b0}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
"""

ROLE_LABEL = {"user": "Citizen", "assistant": "Bot", "agent": "Human ops"}


def esc(t) -> str:
    return html.escape(str(t or ""))


def res_tag(r) -> str:
    r = str(r)
    cls = {"timeout": "bad", "abandoned": "bad", "close": "", "return_to_ai": "warn"}.get(r, "")
    return f'<span class="tag {cls}">{esc(r)}</span>'


def conv_tags(c: dict) -> list[str]:
    """Sidebar/flag vocabulary. Kept small — these are a reading order, not verdicts."""
    tags = [str(c["resolution"])]
    if not c["answered"]:
        tags.append("no-reply")
    if c["wait_seconds"] is not None and c["wait_seconds"] > 300:
        tags.append("slow")
    if c["earlier_cycle"]:
        tags.append("earlier-cycle")
    return tags


def tag_html(tags: list[str]) -> str:
    out = []
    for t in tags:
        cls = "bad" if t in ("timeout", "no-reply", "abandoned") else (
            "warn" if t in ("slow", "return_to_ai", "earlier-cycle") else "")
        out.append(f'<span class="tag {cls}">{esc(t)}</span>')
    return "".join(out)


def conv_page(idx: int, total: int, c: dict) -> str:
    req = T(c["handoff_requested_at"])
    shown = False
    parts = []
    for m in c["messages"]:
        at = T(m.get("created_at"))
        # Mark where the escalation happened, so the reader can see what the bot
        # had already said before a human was asked for.
        if req and at and at >= req and not shown:
            parts.append(f'<div class="reqline">handoff requested '
                         f'{esc((c["handoff_requested_at"] or "")[11:19])}Z</div>')
            shown = True
        role = m.get("role") or "?"
        body = mask(m.get("content") or "")
        parts.append(
            f'<div class="msg {esc(role)}"><div class="who">'
            f'<span>{esc(ROLE_LABEL.get(role, role))}</span>'
            f'<span>{esc((m.get("created_at") or "")[11:19])}</span></div>'
            f'<div dir="{direction(body)}">{rich(body)}</div></div>')
    if req and not shown:
        parts.append(f'<div class="reqline">handoff requested '
                     f'{esc((c["handoff_requested_at"] or "")[11:19])}Z — '
                     f'nothing followed it</div>')

    waited = (dur(c["wait_seconds"]) if c["answered"]
              else ('<span class="none">never</span>'))
    unans = ("" if c["answered"] else
             f'<div><span class="k">request → last activity</span>'
             f'<span class="v">{dur(c["unanswered_seconds"])}</span></div>')

    prev = f'<a class="nav" href="#c{idx-1}">&lsaquo; Previous</a>' if idx > 1 else "<span></span>"
    nxt = f'<a class="nav" href="#c{idx+1}">Next &rsaquo;</a>' if idx < total else "<span></span>"
    title = mask(c["title"] or "(untitled)")

    return f'''<div class="page" id="page-c{idx}">
  <div class="pagehead">
    <div class="ph-top"><span class="counter">Handoff {idx} of {total}</span></div>
    <h2 dir="{direction(title)}">{esc(title)}</h2>
    <div class="tags">{tag_html(conv_tags(c))}</div>
    <p class="ids">requested <code>{esc((c["handoff_requested_at"] or "")[:19])}Z</code>
      · status <code>{esc(c["status"])}</code>
      · {c["message_count"]} messages · id <code>{esc(c["conversation_id"])}</code></p>
    <div class="timing">
      <div><span class="k">wait for a human</span><span class="v">{waited}</span></div>
      <div><span class="k">handoff duration</span><span class="v">{dur(c["span_seconds"])}</span></div>
      <div><span class="k">ops messages</span><span class="v">{c["agent_msgs_after_request"]}</span></div>
      <div><span class="k">ended as</span><span class="v">{esc(c["resolution"])}</span></div>
      {unans}
    </div>
  </div>
  {"".join(parts)}
  <div class="pagenav">{prev}{nxt}</div>
</div>'''


def overview_page(meta: dict, stats: dict, convs: list[dict], cross: dict | None) -> str:
    n = stats["handoff_conversations"]

    cards = [
        ("", f'{n}', "conversations with a handoff"),
        ("", f'{stats["handoff_rate"]*100:.1f}%', f'of {stats["total_in_window"]} in window'),
        ("bad" if stats["expired"] else "good", f'{stats["expired"]}',
         f'EXPIRED — nobody came ({stats["expired_rate"]*100:.0f}% of handoffs)'),
        ("warn" if stats["never_answered"] else "good", f'{stats["never_answered"]}',
         "never got any human reply"),
        ("", dur(stats["wait_median"]), f'median wait for a human (n={stats["wait_n"]})'),
        ("", dur(stats["span_median"]), f'median handoff duration (n={stats["span_n"]})'),
    ]
    card_html = "".join(
        f'<div class="card {c}"><div class="n">{v}</div><div class="l">{l}</div></div>'
        for c, v, l in cards)

    res_rows = "".join(
        f'<tr><td>{res_tag(k)}</td><td>{esc(RESOLUTION_MEANING.get(k, "—"))}</td>'
        f'<td class="num">{v}</td><td class="num">{v/n*100:.0f}%</td></tr>'
        for k, v in sorted(stats["by_resolution"].items(), key=lambda kv: -kv[1])) or \
        '<tr><td colspan="4">no handoffs in window</td></tr>'

    lat_rows = "".join(
        f'<tr><td>{lbl}</td><td class="num">{dur(med)}</td><td class="num">{dur(avg)}</td>'
        f'<td class="num">{dur(p90)}</td><td class="num">{dur(mx)}</td>'
        f'<td class="num">{cnt}</td></tr>'
        for lbl, med, avg, p90, mx, cnt in [
            ("Wait until a human first replied", stats["wait_median"], stats["wait_avg"],
             stats["wait_p90"], stats["wait_max"], stats["wait_n"]),
            ("Handoff duration (request → last ops msg)", stats["span_median"],
             stats["span_avg"], None, stats["span_max"], stats["span_n"]),
            ("Request → last activity, where no reply ever came",
             stats["abandoned_wait_median"], None, None, None, stats["abandoned_wait_n"]),
        ])

    idx_rows = "".join(
        f'<tr onclick="location.hash=\'c{i}\'">'
        f'<td>{i}</td>'
        f'<td class="t" dir="{direction(c["title"])}">{esc(mask(c["title"])[:80])}</td>'
        f'<td class="num">{dur(c["wait_seconds"]) if c["answered"] else "never"}</td>'
        f'<td class="num">{dur(c["span_seconds"])}</td>'
        f'<td class="tg">{tag_html(conv_tags(c))}</td></tr>'
        for i, c in enumerate(convs, 1))

    cross_html = ""
    if cross:
        cross_html = f'''
  <h3>Cross-check — the platform's own analytics</h3>
  <p class="note"><b>Different window, different definitions.</b>
    <code>/analytics/handoff/stats</code> takes a rolling <code>days={cross.get("days")}</code>
    counted back from now, so it cannot be scoped to the window above, and it measures a
    claim event this tool cannot see. Treat a gap as two measurements, not as one being wrong.</p>
  <table class="cfgt"><thead><tr><th>Platform metric</th><th>Value</th></tr></thead><tbody>
    <tr><td>total_handoffs</td><td>{cross.get("total_handoffs", "—")}</td></tr>
    <tr><td>pending / active / resolved</td><td>{cross.get("pending_count", "—")} /
      {cross.get("active_count", "—")} / {cross.get("resolved_count", "—")}</td></tr>
    <tr><td>avg_wait_seconds</td><td>{dur(cross.get("avg_wait_seconds"))}</td></tr>
    <tr><td>avg_resolution_seconds</td><td>{dur(cross.get("avg_resolution_seconds"))}</td></tr>
    <tr><td>resolution_rate</td><td>{(cross.get("resolution_rate") or 0)*100:.0f}%</td></tr>
    <tr><td>return_to_ai_rate</td><td>{(cross.get("return_to_ai_rate") or 0)*100:.0f}%</td></tr>
  </tbody></table>'''

    warn = ""
    if stats["expired_with_reply"]:
        warn = (f'<p class="banner"><b>Check before quoting the expiry number.</b> '
                f'{stats["expired_with_reply"]} conversation(s) marked <code>timeout</code> '
                f'DO have an ops reply after the request — "timeout" may no longer mean '
                f'"nobody came" on this deployment.</p>')

    return f'''<div class="page" id="page-overview">
  <div class="pagehead">
    <div class="ph-top"><span class="counter">Overview</span></div>
    <h2>Handoff audit — {esc(meta["tenant"])}</h2>
    <p class="lede">{esc(meta["start"])} to {esc(meta["end"])} <b>(UTC)</b> ·
      generated {esc(meta["generated_at"])}</p>
    <p class="banner">Read-only audit: GET endpoints only, no LLM calls. Nothing was
      written to any tenant and no queue ticket was claimed, resolved or rerouted.</p>
    {warn}
  </div>

  <div class="cards">{card_html}</div>

  <p class="note"><b>What these timings are.</b> The API exposes no claim or resolve
    timestamp, so "wait" is the gap from <code>handoff_requested_at</code> to the
    <b>first ops reply</b> in the transcript — an agent who claims a ticket and reads
    before typing shows up as a longer wait. Duration runs to the <b>last</b> ops message,
    so a ticket closed without a parting message reads short. Both are honest proxies, not
    platform-recorded SLAs. Expiry is the platform's own <code>timeout</code> label, not
    our inference. The medians count only requests somebody answered — read them next to
    the expired and never-answered counts, not instead of them.</p>

  <h3>How handoffs ended</h3>
  <table class="cfgt"><thead><tr><th>resolution</th><th>meaning</th><th>n</th>
    <th>share</th></tr></thead><tbody>{res_rows}</tbody></table>

  <h3>Service levels</h3>
  <table class="cfgt"><thead><tr><th>metric</th><th>median</th><th>mean</th><th>p90</th>
    <th>max</th><th>n</th></tr></thead><tbody>{lat_rows}</tbody></table>
  {cross_html}

  <h3>All handoff conversations</h3>
  <table class="idx"><thead><tr><th>#</th><th>Conversation</th><th>Wait</th>
    <th>Duration</th><th>Flags</th></tr></thead><tbody>{idx_rows}</tbody></table>

  <p class="note"><b>Not available:</b> per-ops-agent breakdown.
    <code>agent_name</code> is populated only while a handoff is still active and is null
    on every resolved conversation, so "who claimed it" cannot be reconstructed
    historically. A conversation escalated more than once keeps only its latest
    <code>handoff_requested_at</code>; those carry an <code>earlier-cycle</code> tag.</p>
  <p class="meta-line">Identifiers are masked for display. Quote from this report, never
    from audit.json.</p>
</div>'''


FILTERS = [("all", "All"), ("timeout", "Expired"), ("no-reply", "No reply"),
           ("slow", "Slow (&gt;5m)"), ("close", "Resolved")]

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
    let ok = (mode==='all') ? true : tags.split(' ').includes(mode);
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


def render(meta: dict, stats: dict, convs: list[dict], cross: dict | None) -> str:
    total = len(convs)
    pages = "".join(conv_page(i, total, c) for i, c in enumerate(convs, 1))

    nav = ['<a href="#overview" data-page="overview">Overview</a>',
           '<div class="sechead">Handoff conversations</div>']
    for i, c in enumerate(convs, 1):
        tags = " ".join(conv_tags(c))
        title = mask(c["title"] or "(untitled)")
        waited = dur(c["wait_seconds"]) if c["answered"] else "never"
        nav.append(
            f'<a href="#c{i}" data-page="c{i}" data-tags="{esc(tags)}">'
            f'<span class="nrow"><span class="ntitle" dir="{direction(title)}">'
            f'{i}. {esc(title)}</span>'
            f'<span class="nsim">{esc(waited)}</span></span>'
            f'<span class="nflag">{esc(tags)}</span></a>')

    fbtns = "".join(
        f'<button data-m="{k}"{" class=\"on\"" if k == "all" else ""}>{lbl}</button>'
        for k, lbl in FILTERS)

    return f'''<!doctype html><html lang="ar" dir="ltr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Handoff audit {esc(meta["window"])} — {esc(meta["tenant"])}</title>
<style>{CSS}{EXTRA_CSS}</style></head><body>
<div class="app">
  <aside class="side">
    <div class="brand"><b>Handoff audit</b><span>{esc(meta["window"])} ·
      {esc(meta["tenant"])} · <span id="shown">{total}</span>/{total} shown</span></div>
    <div class="sidetools"><input id="q" placeholder="Search handoffs…">
      <div class="fbtns">{fbtns}</div></div>
    <nav>{"".join(nav)}</nav>
  </aside>
  <main>{overview_page(meta, stats, convs, cross)}{pages}</main>
</div>
<script>{JS}</script></body></html>'''


# ---- main --------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Audit handoff wait, duration and expiry for a tenant/window.")
    ap.add_argument("--tenant", default="restricted",
                    help="name or uuid (default: restricted — production)")
    ap.add_argument("--day", help="UTC day, YYYY-MM-DD")
    ap.add_argument("--days", type=int,
                    help="the last N whole UTC days, ending today")
    ap.add_argument("--start", help="explicit ISO lower bound (overrides --day)")
    ap.add_argument("--end", help="explicit ISO upper bound (overrides --day)")
    ap.add_argument("--include-test", action="store_true",
                    help="also count playground traffic (default: production only)")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--count-only", action="store_true",
                    help="report how many handoffs the window holds, fetch no transcripts")
    ap.add_argument("--cross-check", action="store_true",
                    help="also fetch /analytics/handoff/stats (rolling days=N from now)")
    ap.add_argument("--json", action="store_true", help="dump the raw audit as JSON too")
    args = ap.parse_args()

    whole_days = False
    if args.start and args.end:
        start, end, day = args.start, args.end, None
    elif args.day:
        start, end = day_bounds(args.day)
        day = args.day
    elif args.days:
        now = datetime.now(timezone.utc)
        first = (now - timedelta(days=args.days - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        start = first.strftime("%Y-%m-%dT%H:%M:%SZ")
        end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        day = None
        whole_days = True
    else:
        ap.error("give --day, --days N, or --start/--end. "
                 "Bare dates are never inferred: a date-only bound returns 0 silently.")

    tid = resolve_tenant(args.tenant)
    label = tenant_label(tid)

    with StudioAdmin() as api:
        print(f"tenant   {label} ({tid})")
        print(f"window   {start}  ..  {end}   [UTC]")

        if args.count_only:
            # Sizes the run only. The handoff count itself is NOT available here:
            # it lives in the transcripts, and the summaries list has no handoff
            # field to filter on — see the note in collect().
            everything = api.list_conversations(tid, start=start, end=end,
                                                include_test=args.include_test)
            print(f"found    {len(everything)} conversations in window")
            print(f"         a full audit fetches all {len(everything)} transcripts "
                  f"(GET-only, no LLM calls) — how many escalated is only knowable after that")
            return

        data = collect(api, tid, start, end, args.include_test, args.workers)
        stats = summarise(data)

        cross = None
        if args.cross_check:
            days = args.days or 1
            try:
                cross = api.get("/analytics/handoff/stats", tenant_id=tid, days=days)
                print(f"cross    /analytics/handoff/stats?days={days} fetched")
            except Exception as exc:                               # noqa: BLE001
                print(f"cross    unavailable: {exc}")

    meta = {"tenant": label, "tenant_id": tid, "start": start, "end": end,
            "window": window_label(start, end, day, whole_days),
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "include_test": args.include_test}

    out = OUT_ROOT / meta["window"]
    out.mkdir(parents=True, exist_ok=True)
    report = out / "report.html"
    report.write_text(render(meta, stats, data["conversations"], cross),
                      encoding="utf-8")

    if args.json:
        (out / "audit.json").write_text(
            json.dumps({"meta": meta, "stats": stats,
                        "conversations": data["conversations"]},
                       ensure_ascii=False, indent=1), encoding="utf-8")

    if data["transcript_failures"]:
        print(f"WARN     {len(data['transcript_failures'])} transcript(s) failed to fetch")
    if stats["expired_with_reply"]:
        print(f"WARN     {stats['expired_with_reply']} 'timeout' conversation(s) DO have "
              f"an ops reply — re-check what timeout means before quoting expiries")

    print(f"handoffs {stats['handoff_conversations']} of {stats['total_in_window']} "
          f"conversations ({stats['handoff_rate']*100:.1f}%)")
    print(f"wait     median {dur(stats['wait_median'])} · "
          f"p90 {dur(stats['wait_p90'])} · max {dur(stats['wait_max'])} "
          f"(n={stats['wait_n']})")
    print(f"duration median {dur(stats['span_median'])} (n={stats['span_n']})")
    print(f"expired  {stats['expired']} ({stats['expired_rate']*100:.0f}% of handoffs) · "
          f"never answered at all {stats['never_answered']}")
    print(f"REPORT URL: file://{report}")


if __name__ == "__main__":
    main()
