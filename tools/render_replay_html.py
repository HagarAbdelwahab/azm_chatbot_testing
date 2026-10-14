"""Render a production-vs-testing replay run as a reviewable, PAGED HTML report.

    .venv/bin/python tools/render_replay_html.py reports/replay/restricted-to-testing/2026-08-31

Reads the run's state.json + meta.json, re-reads the pull it ran on, aligns each
production turn with its replayed counterpart, and writes report.html next to them.

Structure: one HTML file, many pages. A sidebar indexes every conversation; the run
metadata and the configuration diff live on their own Overview page; each conversation
gets a page of its own. A single scrolling wall made consecutive turns bleed into each
other and buried the start of every conversation — with 130 conversations in a full day
that is unreviewable, so navigation is part of the artefact, not a nicety.

Identifiers (national ID, IBAN, phone) are MASKED here. The replay itself sent the
original text — masking at render time keeps the bot's behaviour faithful while keeping
real citizens' identifiers out of the artefact that gets shared around.

What the production side can and cannot show: the transcript API returns role, content
and timestamp only. There are no retrieved sources, no tool trace and no timings for
production turns. Sources/Tools panels therefore appear on the REPLAY side only — an
empty production panel would be a rendering artefact, not evidence, so none is drawn.
"""

from __future__ import annotations

import html
import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

ARABIC = re.compile(r"[؀-ۿ]")

# Masked for display only. Order matters: the specific patterns run before the generic
# long-digit rule, or that rule eats their numeric tails first.
#
# DO NOT use \b around these. Arabic letters and digits are BOTH word characters, so
# there is no word boundary in `الهوية1001520665` — a \b-anchored rule silently misses
# every identifier typed flush against Arabic text, which is how users actually write
# them. Anchor on digit lookarounds instead, which do not care what precedes the number.
MASKS: list[tuple[str, str]] = [
    (r"(?<![A-Za-z0-9])SA\d{22}(?!\d)", "[IBAN]"),
    (r"(?<!\d)(?:\+?966|00966)5\d{8}(?!\d)|(?<!\d)05\d{8}(?!\d)", "[PHONE]"),
    (r"(?<!\d)[12]\d{9}(?!\d)", "[NATIONAL_ID]"),
    (r"[\w.+-]+@[\w-]+\.[\w.]+", "[EMAIL]"),
]
# Case/request numbers are kept partially visible — a reviewer needs to see that turn 4
# refers to the same request as turn 1, and the tail alone is enough to tell them apart.
PARTIAL = [(r"(?<![A-Za-z0-9])ED-(\d)-(\d+)(?!\d)",
            lambda m: f"ED-{m.group(1)}-••••{m.group(2)[-4:]}")]


def mask(text: str) -> str:
    for pat, repl in PARTIAL:
        text = re.sub(pat, repl, text)
    for pat, token in MASKS:
        text = re.sub(pat, token, text)
    return text


def rich(text: str) -> str:
    esc = html.escape(mask(text or ""))
    esc = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", esc, flags=re.S)
    paras = [p.strip() for p in re.split(r"\n\s*\n", esc) if p.strip()]
    return "".join(f"<p>{p.replace(chr(10), '<br>')}</p>" for p in paras) or \
        '<p class="empty">— no text —</p>'


def direction(text: str) -> str:
    return "rtl" if ARABIC.search(text or "") else "ltr"


def norm(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "")).strip()


def similarity(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


# Phrases the agent uses when it declines or falls back. Used to detect a refusal that
# appeared or vanished between the two configs — the single most reviewable change.
REFUSAL = re.compile(
    r"لا أستطيع|لا يمكنني|لا أملك|لست قادر|عذرًا|عذراً|لا تتوفر لدي|"
    r"أنصحك بالتواصل|التواصل مع الدعم|خارج نطاق|I (?:cannot|can't|am unable)|I don't have",
    re.I)


def is_refusal(t: str) -> bool:
    t = norm(t)
    return bool(t) and bool(REFUSAL.search(t))


CLARIFY = re.compile(r"[؟?]\s*$|هل تقصد|هل تحتاج|أي من|يرجى تزويدي|ما هو رقم|من فضلك زودني",
                     re.M)


def asked_question(text: str) -> bool:
    """Did this answer put the ball back in the user's court?"""
    t = norm(text)
    return bool(t) and bool(CLARIFY.search(t))


def mark_misalignment(rows: list[dict]) -> None:
    """Flag turns where the user's next message no longer fits what testing just said.

    THE LIMIT OF A FAITHFUL REPLAY. The user's turns are a fixed script recorded against
    the production bot. The moment testing answers turn N differently — asks a question
    production didn't, or answers one production asked — turn N+1 stops being a
    like-for-like probe: the user is replying to something testing never said.

    That is not a reason to rewrite the turn (then you are testing your own improvisation,
    not the config). It IS a reason to stop reading the rest of that thread as a clean
    comparison, so mark it instead of letting a reviewer draw a false conclusion.
    """
    for i in range(1, len(rows)):
        prev = rows[i - 1]
        if prev["prod"] is None or prev["replay"] is None:
            continue
        p_asked = asked_question(prev["prod"])
        t_asked = asked_question((prev["replay"] or {}).get("bot"))
        if p_asked and not t_asked:
            rows[i]["misaligned"] = ("production asked the user a question here and testing "
                                     "did not — this reply answers a question testing never asked")
        elif t_asked and not p_asked:
            rows[i]["misaligned"] = ("testing asked the user a question here and production "
                                     "did not — testing's question goes unanswered from now on")
        elif similarity(prev["prod"], (prev["replay"] or {}).get("bot")) < 0.25:
            rows[i]["misaligned"] = ("the two answers above diverged almost completely — "
                                     "from here the threads are on different subjects")


def align(prod_conv: dict, rec: dict) -> list[dict]:
    """Pair each user turn with the production answer and the replayed answer.

    Production messages are a flat role-tagged list; the answer to user turn i is every
    assistant message after it and before the next user message (the bot occasionally
    emits more than one). Replay turns are already 1:1 with user turns.
    """
    msgs = prod_conv.get("messages") or []
    prod_pairs: list[tuple[str, str]] = []
    i = 0
    while i < len(msgs):
        if msgs[i].get("role") != "user":
            i += 1
            continue
        user = msgs[i].get("content") or ""
        j, replies = i + 1, []
        while j < len(msgs) and msgs[j].get("role") != "user":
            if msgs[j].get("role") in ("assistant", "bot"):
                replies.append(msgs[j].get("content") or "")
            j += 1
        if user.strip():
            prod_pairs.append((user, "\n\n".join(r for r in replies if r)))
        i = j

    replay_turns = rec.get("turns") or []
    rows = []
    for n in range(max(len(prod_pairs), len(replay_turns))):
        user_p, prod_bot = prod_pairs[n] if n < len(prod_pairs) else ("", None)
        rt = replay_turns[n] if n < len(replay_turns) else None
        rows.append({
            "n": n + 1,
            "user": (rt or {}).get("user") or user_p,
            "prod": prod_bot,
            "replay": rt,
            "misaligned": None,
        })
    mark_misalignment(rows)
    return rows


def classify(rows: list[dict], rec: dict, prod_conv: dict) -> dict:
    """Per-conversation change signals, so a reviewer can triage 100 of these."""
    sims, flips = [], []
    for r in rows:
        p, t = r["prod"], (r["replay"] or {}).get("bot")
        if p is None or t is None:
            continue
        sims.append(similarity(p, t))
        pr, tr = is_refusal(p), is_refusal(t)
        if pr != tr:
            flips.append("refused→answered" if pr else "answered→refused")
    avg = sum(sims) / len(sims) if sims else None

    tags = []
    if rec.get("error"):
        tags.append("replay-error")
    if len(rec.get("turns") or []) < len(prod_conv.get("user_turns") or []):
        tags.append("incomplete")
    if "answered→refused" in flips:
        tags.append("answered→refused")
    if "refused→answered" in flips:
        tags.append("refused→answered")
    if any((r["replay"] or {}).get("confirmation_request") for r in rows):
        tags.append("confirmation-gate")
    if any((r["replay"] or {}).get("tool_failures") for r in rows):
        tags.append("tool-failed")
    if any(isinstance((r["replay"] or {}).get("retrieval_score"), (int, float))
           and (r["replay"] or {})["retrieval_score"] < 0.5 for r in rows):
        tags.append("weak-retrieval")
    # Only a SUBSTANTIVE answer with no retrieval is a finding. Clarification questions
    # legitimately retrieve nothing, and flagging those buries the real cases.
    if any(not (r["replay"] or {}).get("sources")
           and len(norm((r["replay"] or {}).get("bot") or "")) > 220
           and not (r["replay"] or {}).get("confirmation_request")
           for r in rows if r["replay"]):
        tags.append("ungrounded-answer")
    if any(((r["replay"] or {}).get("bot") or "").strip() == "" and
           not (r["replay"] or {}).get("confirmation_request") for r in rows if r["replay"]):
        tags.append("empty-reply")
    if prod_conv.get("handoff_requested_at"):
        tags.append("prod-handoff")
    if avg is not None and avg < 0.35:
        tags.append("diverged")
    n_mis = sum(1 for r in rows if r.get("misaligned"))
    if n_mis:
        tags.append(f"context-diverged×{n_mis}")

    prod_len = sum(len(norm(r["prod"] or "")) for r in rows)
    rep_len = sum(len(norm((r["replay"] or {}).get("bot") or "")) for r in rows)
    return {"similarity": avg, "tags": tags, "prod_chars": prod_len, "replay_chars": rep_len,
            "len_delta": rep_len - prod_len, "misaligned": n_mis}


def simcls(v: float | None) -> str:
    if v is None:
        return "na"
    return "good" if v >= 0.7 else ("mid" if v >= 0.35 else "bad")


def panels(t: dict) -> str:
    """Sources + tool trace + scores — replay side only; production exposes none of it."""
    out = ""

    sc = [(k, t.get(k)) for k in ("answer_quality_score", "retrieval_score", "faithfulness_score")]
    sc = [(k, v) for k, v in sc if isinstance(v, (int, float))]
    if sc:
        chips = "".join(
            f'<span class="score {"good" if v >= 0.75 else "mid" if v >= 0.5 else "bad"}">'
            f'{k.replace("_score", "").replace("_", " ")} {v:.2f}</span>' for k, v in sc)
        ver = t.get("scoring_version")
        out += (f'<p class="scores">{chips}'
                f'{f"<span class=note>{html.escape(str(ver))}</span>" if ver else ""}</p>')

    srcs = t.get("sources") or []
    if srcs:
        items = "".join(f"<li>📄 {html.escape(mask(str(s)))[:400]}</li>" for s in srcs)
        out += (f'<details class="pan"><summary>Retrieved context ({len(srcs)})</summary>'
                f'<ul class="srcs">{items}</ul></details>')
    else:
        out += ('<details class="pan"><summary class="warn">Retrieved context (0)</summary>'
                '<p class="note">nothing retrieved for this turn — any specific fact in the '
                'answer is ungrounded.</p></details>')

    steps = t.get("steps") or []
    if steps:
        results = {s.get("seq"): s for s in steps if s.get("kind") == "tool_result"}
        rows = []
        for s in steps:
            if s.get("kind") != "tool_call":
                continue
            res = results.get(s.get("seq"))
            m = "…" if res is None else ("✓" if res.get("ok") else "✗ failed")
            c = "pend" if res is None else ("ok" if res.get("ok") else "bad")
            rows.append(f'<li><span class="{c}">{m}</span> {html.escape(str(s.get("tool")))}</li>')
        if rows:
            out += (f'<details class="pan"><summary>Tools used ({len(rows)})</summary>'
                    f'<ul class="srcs">{"".join(rows)}</ul></details>')

    cr = t.get("confirmation_request")
    if cr:
        args = cr.get("arguments") or {}
        argrows = "".join(
            f"<tr><td>{html.escape(str(k))}</td><td>{html.escape(mask(str(v)))}</td></tr>"
            for k, v in args.items())
        out += (f'<details class="pan gate" open><summary>⏸ Confirmation gate — '
                f'{html.escape(str(cr.get("tool")))}</summary>'
                f'<p class="note">{html.escape(mask(str(cr.get("question") or "")))}</p>'
                f'<table class="args">{argrows}</table>'
                f'<p class="note">target: {html.escape(str(cr.get("target") or ""))}</p></details>')

    bits = []
    if t.get("abstention_reason"):
        bits.append(f'abstention: {html.escape(str(t["abstention_reason"]))}')
    if t.get("handoff_status"):
        bits.append(f'handoff: {html.escape(str(t["handoff_status"]))}')
    if t.get("handoff_offer"):
        bits.append('handoff offered to user')
    if t.get("pii_intercept_count"):
        bits.append(f'PII intercepts: {t["pii_intercept_count"]}')
    if t.get("unknown_keys"):
        bits.append(f'⚠ unparsed frame keys: {", ".join(t["unknown_keys"])}')
    if t.get("total_ms"):
        bits.append(f'{t["total_ms"]} ms')
    if bits:
        out += f'<p class="meta-line">{" · ".join(bits)}</p>'
    return out


def tag_html(tags: list[str]) -> str:
    def cls(t: str) -> str:
        if t.startswith("answered→refused") or t in ("replay-error", "empty-reply",
                                                     "tool-failed", "ungrounded-answer"):
            return "bad"
        if t.startswith("context-diverged") or t in ("diverged", "weak-retrieval", "incomplete"):
            return "warn"
        return ""
    return "".join(f'<span class="tag {cls(t)}">{html.escape(t)}</span>' for t in tags)


def conv_page(idx: int, prod_conv: dict, rec: dict, rows: list[dict], cls: dict,
              total: int) -> str:
    sim = cls["similarity"]
    simtxt = f"{sim:.0%}" if sim is not None else "—"
    delta = cls["len_delta"]
    title = html.escape(mask(str(prod_conv.get("title") or "(untitled)")))[:160]

    turns = ""
    for r in rows:
        u, rt = r["user"], r["replay"]
        warn = (f'<p class="misalign">⚠ <strong>context diverged</strong> — '
                f'{html.escape(r["misaligned"])}. The user wording below is the original '
                f'production script, sent unchanged; read the comparison with that in mind.</p>'
                if r.get("misaligned") else "")

        prod_html = (f'<div class="ans" dir="{direction(r["prod"])}">{rich(r["prod"])}</div>'
                     if r["prod"] is not None
                     else '<div class="ans missing">— no production reply recorded —</div>')
        if rt is None:
            rep_html = '<div class="ans missing">— not replayed (run stopped or errored) —</div>'
        else:
            rep_html = (f'<div class="ans" dir="{direction(rt.get("bot"))}">{rich(rt.get("bot"))}</div>'
                        f'{panels(rt)}')

        tsim = (similarity(r["prod"], (rt or {}).get("bot"))
                if r["prod"] is not None and rt is not None else None)
        badge = (f'<span class="pill {simcls(tsim)}">{tsim:.0%} alike</span>'
                 if tsim is not None else "")

        turns += (
            f'<article class="turn">'
            f'  <div class="turnbar"><span class="turnno">Turn {r["n"]}<span class="of">'
            f'      / {len(rows)}</span></span>{badge}</div>'
            f'  {warn}'
            f'  <div class="user" dir="{direction(u)}">'
            f'      <span class="lbl">User asked</span>{rich(u)}</div>'
            f'  <div class="cols">'
            f'    <div class="col prod"><div class="colhead">'
            f'        <span class="dot p"></span>Production · restricted</div>{prod_html}</div>'
            f'    <div class="col test"><div class="colhead">'
            f'        <span class="dot t"></span>Replay · testing</div>{rep_html}</div>'
            f'  </div>'
            f'</article>')

    err = (f'<p class="err">replay error: {html.escape(str(rec["error"]))}</p>'
           if rec.get("error") else "")
    prev_a = (f'<a class="nav prev" href="#c{idx - 1}">← #{idx - 1}</a>' if idx > 1
              else '<a class="nav prev" href="#overview">← Overview</a>')
    next_a = (f'<a class="nav next" href="#c{idx + 1}">#{idx + 1} →</a>' if idx < total
              else '<span class="nav done">end of run</span>')

    return (
        f'<section class="page" id="page-c{idx}">'
        f'  <div class="pagehead">'
        f'    <div class="ph-top"><span class="counter">Conversation {idx} of {total}</span>'
        f'         <span class="pill {simcls(sim)}">{simtxt} alike</span>'
        f'         <span class="pill quiet">{"+" if delta > 0 else ""}{delta} chars</span></div>'
        f'    <h2 dir="{direction(title)}">{title}</h2>'
        f'    <div class="tags">{tag_html(cls["tags"])}</div>'
        f'    <p class="ids">production <code>{prod_conv["prod_conversation_id"][:8]}</code>'
        f'       · {html.escape(str(prod_conv.get("created_at") or ""))}'
        f'       · replay <code>{html.escape(str(rec.get("replay_conversation_id", "-"))[:8])}</code>'
        f'       · {len(rows)} turns</p>'
        f'    {err}'
        f'  </div>'
        f'  {turns}'
        f'  <div class="pagenav">{prev_a}{next_a}</div>'
        f'</section>')


def config_table(meta: dict) -> str:
    pa = ((meta.get("prod_config") or {}).get("agent")) or {}
    ta = ((meta.get("replay_config") or {}).get("agent")) or {}
    if not pa and not ta:
        return '<p class="note">no configuration snapshot captured for this run.</p>'
    changed, same = "", ""
    for k in sorted(set(pa) | set(ta)):
        pv, tv = pa.get(k), ta.get(k)
        row = (f'<tr class="{"chg" if pv != tv else ""}"><td>{html.escape(k)}</td>'
               f'<td>{html.escape(str(pv))}</td><td>{html.escape(str(tv))}</td></tr>')
        if pv != tv:
            changed += row
        else:
            same += row
    n = changed.count("<tr")
    head = ('<thead><tr><th>field</th><th>production (restricted)</th>'
            '<th>replay (testing)</th></tr></thead>')
    out = (f'<h3>Configuration — {n} field(s) differ</h3>'
           f'<p class="note">These differences are the reason the answers differ. Read any '
           f'finding against them: a change in knowledge base is not a change in model.</p>'
           f'<table class="cfgt">{head}<tbody>{changed}</tbody></table>')
    if same:
        out += (f'<details class="pan"><summary>Identical fields ({same.count("<tr")})</summary>'
                f'<table class="cfgt">{head}<tbody>{same}</tbody></table></details>')
    return out


def overview_page(meta: dict, index_rows: list[dict], stats: dict, pull_path: Path) -> str:
    win = meta.get("pull_window", {})
    day = win.get("day") or (win.get("start") or "")[:10]
    rows = ""
    for r in index_rows:
        rows += (f'<tr onclick="location.hash=\'c{r["idx"]}\'">'
                 f'<td>{r["idx"]}</td>'
                 f'<td dir="{direction(r["title"])}" class="t">{html.escape(r["title"])[:90]}</td>'
                 f'<td>{r["turns"]}</td>'
                 f'<td><span class="pill {simcls(r["sim"])}">'
                 f'{f"{r['sim']:.0%}" if r["sim"] is not None else "—"}</span></td>'
                 f'<td class="tg">{tag_html(r["tags"])}</td></tr>')

    return (
        f'<section class="page" id="page-overview">'
        f'  <div class="pagehead">'
        f'    <div class="ph-top"><span class="counter">Overview</span></div>'
        f'    <h2>Production replay — {html.escape(day)}</h2>'
        f'    <p class="lede">Real conversations from '
        f'      <strong>{html.escape(str(meta.get("prod_tenant", "?")))}</strong> (production), '
        f'      re-sent verbatim to <strong>{html.escape(str(meta.get("replay_tenant", "?")))}</strong>. '
        f'      Surface: {html.escape(str(meta.get("surface", "widget API")))}. '
        f'      Replayed {html.escape(str(meta.get("replayed_at", "")))}. '
        f'      The window is a <strong>UTC</strong> day. Identifiers are masked for display; '
        f'      the replay sent the original text.</p>'
        f'    <p class="banner">↳ <strong>{html.escape(str(meta.get("prod_tenant", "?")))} '
        f'      was read only</strong> — no message was ever sent to it. Every replayed '
        f'      conversation was created on '
        f'      <strong>{html.escape(str(meta.get("replay_tenant", "?")))}</strong>, verified '
        f'      before sending by probing which tenant the widget key writes to.</p>'
        f'  </div>'
        f'  <ul class="stats">'
        f'    <li><b>{stats["convs"]}</b>conversations</li>'
        f'    <li><b>{stats["turns"]}</b>replayed turns</li>'
        f'    <li><b>{stats["avg"]}</b>mean similarity</li>'
        f'    <li><b>{stats["low"]}</b>heavily diverged</li>'
        f'    <li><b>{stats["qavg"]}</b>mean answer-quality</li>'
        f'    <li><b>{html.escape(str(stats["pmodel"]))}</b>production model</li>'
        f'    <li><b>{html.escape(str(stats["tmodel"]))}</b>testing model</li>'
        f'  </ul>'
        f'  <p class="note flags">flags — {html.escape(stats["tagline"])}</p>'
        f'  {config_table(meta)}'
        f'  <h3>All conversations</h3>'
        f'  <table class="idx"><thead><tr><th>#</th><th>opening message</th><th>turns</th>'
        f'    <th>alike</th><th>flags</th></tr></thead><tbody>{rows}</tbody></table>'
        f'  <p class="note">Similarity is a character-level diff ratio — a reading order, not '
        f'    a verdict. A 90%-alike answer can still be wrong, and a 20%-alike one can be an '
        f'    improvement. Production turns carry no sources, tool trace or timings because the '
        f'    transcript API does not expose them; those panels reflect the replay only.</p>'
        f'  <p class="note">pull: {html.escape(str(pull_path))}</p>'
        f'</section>')


CSS = """
:root{--bg:#f4f6f8;--card:#fff;--ink:#16191d;--mut:#6b7280;--line:#e4e7eb;
--user:#eef1fb;--prod:#fffaf1;--test:#f0faf4;--bad:#c0392b;--good:#1e8449;--mid:#b7791f;
--side:#1f2430;--sideink:#c9d1de;--accent:#3b6fd4;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI",Tahoma,Arial,sans-serif;}
.app{display:flex;min-height:100vh}

/* ---- sidebar ---- */
.side{width:290px;flex:0 0 290px;background:var(--side);color:var(--sideink);
position:sticky;top:0;height:100vh;display:flex;flex-direction:column}
.brand{padding:16px 16px 10px;border-bottom:1px solid #2c3342}
.brand b{display:block;color:#fff;font-size:14px}
.brand span{font-size:11.5px;color:#8b95a7}
.sidetools{padding:10px 12px;border-bottom:1px solid #2c3342}
.sidetools input{width:100%;padding:7px 10px;border-radius:7px;border:1px solid #39414f;
background:#151922;color:#e6eaf1;font-size:12.5px}
.fbtns{display:flex;flex-wrap:wrap;gap:4px;margin-top:8px}
.fbtns button{border:1px solid #39414f;background:#232936;color:#b9c2d0;border-radius:999px;
padding:3px 9px;font-size:11px;cursor:pointer}
.fbtns button.on{background:var(--accent);border-color:var(--accent);color:#fff}
nav{overflow-y:auto;flex:1;padding:8px 0 20px}
nav a{display:block;padding:8px 14px;color:var(--sideink);text-decoration:none;
border-left:3px solid transparent;font-size:12.5px}
nav a:hover{background:#272e3b}
nav a.on{background:#2b3446;border-left-color:var(--accent);color:#fff}
nav a .nrow{display:flex;justify-content:space-between;gap:8px;align-items:center}
nav a .ntitle{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
nav a .nsim{font-size:10.5px;font-weight:700;opacity:.9}
nav a .nflag{display:block;font-size:10px;color:#8b95a7;margin-top:2px;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
nav .sechead{padding:12px 14px 4px;font-size:10.5px;letter-spacing:.08em;color:#7b8697;
text-transform:uppercase}

/* ---- pages ---- */
main{flex:1;min-width:0;padding:26px 30px 60px;max-width:1250px}
.page{display:none}
.page.on{display:block;animation:fade .16s ease-out}
@keyframes fade{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:none}}
.pagehead{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:18px 20px;margin-bottom:22px;border-top:4px solid var(--accent)}
.ph-top{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:6px}
.counter{font-size:11px;letter-spacing:.09em;text-transform:uppercase;color:var(--mut);
font-weight:700}
.pagehead h2{margin:2px 0 8px;font-size:20px;line-height:1.45}
.lede{color:var(--mut);font-size:13px;margin:0 0 10px}
.banner{background:#eefaf2;border-left:3px solid var(--good);padding:9px 13px;
border-radius:0 8px 8px 0;margin:0;color:#2c4a3a;font-size:12.5px}
.tags{display:flex;gap:5px;flex-wrap:wrap;margin-bottom:6px}
.pill{font-size:11.5px;padding:2px 9px;border-radius:999px;font-weight:700;
font-variant-numeric:tabular-nums;white-space:nowrap}
.pill.good{background:#e6f6ec;color:var(--good)}
.pill.mid{background:#fdf3e0;color:var(--mid)}
.pill.bad{background:#fdeceb;color:var(--bad)}
.pill.na,.pill.quiet{background:#eef0f3;color:#555}
.tag{font-size:11px;padding:2px 8px;border-radius:999px;background:#eef0f3;color:#445}
.tag.bad{background:#fdeceb;color:var(--bad)}
.tag.warn{background:#fdf3e0;color:var(--mid)}
.ids{color:var(--mut);font-size:12px;margin:6px 0 0}
.ids code{background:#f0f2f5;padding:1px 5px;border-radius:4px}

/* ---- turns: each one a distinct card, clearly numbered ---- */
.turn{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:0 18px 18px;margin-bottom:26px;box-shadow:0 1px 2px rgba(20,25,35,.05)}
.turnbar{display:flex;justify-content:space-between;align-items:center;gap:10px;
margin:0 -18px 14px;padding:9px 18px;background:#f7f9fb;border-bottom:1px solid var(--line);
border-radius:12px 12px 0 0}
.turnno{font-size:12px;font-weight:800;letter-spacing:.05em;text-transform:uppercase;
color:#3c4450}
.turnno .of{font-weight:600;color:var(--mut);letter-spacing:0;text-transform:none}
.misalign{background:#fff7e6;border-left:3px solid #d99a0b;border-radius:0 6px 6px 0;
padding:8px 12px;margin:0 0 12px;font-size:12.5px;color:#7a5a06}
.user{background:var(--user);border:1px solid #dde3f5;border-radius:10px;
padding:11px 14px;margin-bottom:14px}
.lbl{display:block;font-size:10.5px;letter-spacing:.07em;text-transform:uppercase;
color:#5b6478;margin-bottom:4px;font-weight:700}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:1000px){.cols{grid-template-columns:1fr}}
.col{border-radius:10px;padding:12px 14px;min-width:0;border:1px solid var(--line)}
.col.prod{background:var(--prod)}
.col.test{background:var(--test)}
.colhead{font-size:10.5px;letter-spacing:.07em;text-transform:uppercase;color:#5b6478;
margin-bottom:8px;font-weight:700;display:flex;align-items:center;gap:6px}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block}
.dot.p{background:#d99a0b}.dot.t{background:var(--good)}
.ans p{margin:0 0 9px}
.ans p:last-child{margin-bottom:0}
.ans.missing,.empty{color:var(--mut);font-style:italic}

/* ---- panels ---- */
.scores{margin:10px 0 4px;display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.score{font-size:11px;padding:2px 8px;border-radius:999px;font-weight:700;
font-variant-numeric:tabular-nums}
.score.good{background:#e6f6ec;color:var(--good)}
.score.mid{background:#fdf3e0;color:var(--mid)}
.score.bad{background:#fdeceb;color:var(--bad)}
.pan{margin-top:9px;font-size:12.5px}
.pan summary{cursor:pointer;color:var(--mut)}
.pan summary.warn{color:var(--bad)}
.pan.gate{border:1px solid #f0c36d;background:#fffaf0;border-radius:8px;padding:8px}
.srcs{margin:6px 0 0;padding-left:18px}
.srcs li{margin-bottom:4px}
.srcs .ok{color:var(--good)}.srcs .bad{color:var(--bad)}.srcs .pend{color:var(--mut)}
.args{border-collapse:collapse;margin:6px 0;font-size:12px}
.args td{border:1px solid var(--line);padding:3px 7px}
.note{color:var(--mut);font-size:12.5px;margin:8px 0}
.meta-line{color:var(--mut);font-size:11.5px;margin:8px 0 0}
.err{color:var(--bad);font-size:13px}

/* ---- overview ---- */
.stats{display:flex;flex-wrap:wrap;gap:26px;margin:0 0 10px;padding:16px 20px;list-style:none;
background:var(--card);border:1px solid var(--line);border-radius:12px}
.stats li{font-size:12px;color:var(--mut)}
.stats b{display:block;font-size:21px;color:var(--ink);line-height:1.3}
.flags{margin-bottom:20px}
h3{font-size:15px;margin:26px 0 8px}
.cfgt,.idx{border-collapse:collapse;width:100%;font-size:12.5px;background:var(--card)}
.cfgt th,.cfgt td,.idx th,.idx td{border:1px solid var(--line);padding:6px 9px;
text-align:left;vertical-align:top;word-break:break-word}
.cfgt th,.idx th{background:#eef1f4;font-size:11.5px;text-transform:uppercase;
letter-spacing:.05em}
.cfgt tr.chg td{background:#fff5f5;font-weight:600}
.idx tbody tr{cursor:pointer}
.idx tbody tr:hover{background:#f2f6fd}
.idx td.t{max-width:420px}
.idx td.tg{max-width:300px}

/* ---- page nav ---- */
.pagenav{display:flex;justify-content:space-between;margin-top:26px;padding-top:16px;
border-top:1px solid var(--line)}
.nav{text-decoration:none;color:var(--accent);font-size:13px;font-weight:600;
background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 14px}
.nav:hover{background:#eef3fd}
.nav.done{color:var(--mut);font-weight:400}
@media(max-width:820px){
  .app{flex-direction:column}
  .side{width:100%;flex:none;height:auto;position:static}
  nav{max-height:240px}
  main{padding:18px}
}
"""

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
    if(mode==='changed') ok=(parseFloat(a.dataset.sim)<0.7)||tags.length>0;
    else if(mode==='flips') ok=tags.includes('refused');
    else if(mode==='context') ok=tags.includes('context-diverged');
    else if(mode==='problems') ok=/replay-error|incomplete|empty-reply|tool-failed|ungrounded/.test(tags);
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

// j / k step through conversations without reaching for the mouse
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
    if len(sys.argv) < 2:
        sys.exit("usage: render_replay_html.py <run-dir>   "
                 "(e.g. reports/replay/restricted-to-testing/2026-08-31)")
    run_dir = Path(sys.argv[1]).resolve()
    state_f, meta_f = run_dir / "state.json", run_dir / "meta.json"
    if not state_f.exists():
        sys.exit(f"no state.json in {run_dir} — run replay_conversations.py first.")
    state = json.loads(state_f.read_text(encoding="utf-8"))
    meta = json.loads(meta_f.read_text(encoding="utf-8")) if meta_f.exists() else {}

    pull_path = Path(meta.get("pull_file", ""))
    if not pull_path.exists():
        sys.exit(f"pull file {pull_path} is missing — it holds the production side, "
                 f"without which there is nothing to compare against.")
    pull = json.loads(pull_path.read_text(encoding="utf-8"))
    by_id = {c["prod_conversation_id"]: c for c in pull["conversations"]}

    ordered = [(pid, rec) for pid, rec in
               sorted(state.items(), key=lambda kv: (by_id.get(kv[0], {}).get("created_at") or ""))
               if pid in by_id]

    pages, index_rows, sims, tagcount = [], [], [], {}
    for i, (pid, rec) in enumerate(ordered, 1):
        conv = by_id[pid]
        rows = align(conv, rec)
        cls = classify(rows, rec, conv)
        if cls["similarity"] is not None:
            sims.append(cls["similarity"])
        for t in cls["tags"]:
            tagcount[t] = tagcount.get(t, 0) + 1
        pages.append(conv_page(i, conv, rec, rows, cls, len(ordered)))
        opening = norm(conv["user_turns"][0]) if conv.get("user_turns") else \
            str(conv.get("title") or "(untitled)")
        index_rows.append({"idx": i, "title": mask(opening), "turns": len(rows),
                           "sim": cls["similarity"], "tags": cls["tags"]})

    n_turns = sum(len(r.get("turns") or []) for r in state.values())
    q = [t["answer_quality_score"] for r in state.values() for t in (r.get("turns") or [])
         if isinstance(t.get("answer_quality_score"), (int, float))]
    stats = {
        "convs": len(pages), "turns": n_turns,
        "avg": f"{sum(sims) / len(sims):.0%}" if sims else "—",
        "low": sum(1 for s in sims if s < 0.35),
        "qavg": f"{sum(q) / len(q):.2f}" if q else "—",
        "pmodel": (((meta.get("prod_config") or {}).get("agent")) or {}).get("model", "?"),
        "tmodel": (((meta.get("replay_config") or {}).get("agent")) or {}).get("model", "?"),
        "tagline": " · ".join(f"{k}: {v}" for k, v in
                              sorted(tagcount.items(), key=lambda x: -x[1])) or "none",
    }

    nav = ('<a href="#overview" data-page="overview"><span class="nrow">'
           '<span class="ntitle">Overview &amp; configuration</span></span></a>'
           '<div class="sechead">Conversations</div>')
    for r in index_rows:
        simtxt = f"{r['sim']:.0%}" if r["sim"] is not None else "—"
        flags = ", ".join(r["tags"][:3]) if r["tags"] else "no flags"
        nav += (f'<a href="#c{r["idx"]}" data-page="c{r["idx"]}" '
                f'data-sim="{r["sim"] if r["sim"] is not None else -1}" '
                f'data-tags="{html.escape(" ".join(r["tags"]))}">'
                f'<span class="nrow"><span class="ntitle">#{r["idx"]} '
                f'{html.escape(r["title"])[:60]}</span>'
                f'<span class="nsim">{simtxt}</span></span>'
                f'<span class="nflag">{html.escape(flags)}</span></a>')

    win = meta.get("pull_window", {})
    day = win.get("day") or (win.get("start") or "")[:10]

    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Replay {html.escape(day)} — production vs testing</title><style>{CSS}</style></head>
<body><div class="app">
<aside class="side">
  <div class="brand"><b>Production replay</b><span>{html.escape(day)} ·
    {html.escape(str(meta.get('prod_tenant', '?')))} → {html.escape(str(meta.get('replay_tenant', '?')))}</span></div>
  <div class="sidetools">
    <input id="q" placeholder="search all conversations…">
    <div class="fbtns">
      <button class="on" data-m="all">All</button>
      <button data-m="changed">Changed</button>
      <button data-m="flips">Refusal flips</button>
      <button data-m="context">Context diverged</button>
      <button data-m="problems">Problems</button>
    </div>
    <p class="note" style="color:#8b95a7;font-size:11px;margin:8px 0 0">
      <span id="shown">0</span> shown · press <b>j</b>/<b>k</b> to step</p>
  </div>
  <nav>{nav}</nav>
</aside>
<main>
{overview_page(meta, index_rows, stats, pull_path)}
{''.join(pages)}
</main></div>
<script>{JS}</script></body></html>"""

    out = run_dir / "report.html"
    out.write_text(page, encoding="utf-8")
    print(f"pages: {len(pages)} conversations + 1 overview | turns: {n_turns} | "
          f"mean similarity: {stats['avg']}")
    if tagcount:
        print("flags:", stats["tagline"])
    print(f"REPORT URL: {out.as_uri()}")


if __name__ == "__main__":
    main()
