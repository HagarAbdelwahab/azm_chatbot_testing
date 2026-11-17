"""Swap a production user's identifying data for the signed-in ops user's own.

A replay keeps the citizen's phrasing; what must change is everything that is unique to
that citizen (national ids, names, request/deed/division/ticket numbers, phone, IBAN).
On ops those values belong to nobody, so the tools only ever answer "not yours". Each
production conversation is therefore rewritten against ONE consistent ops case: the same
request, deceased, heirs and deeds the signed-in test user really has.

Two parts:

  * build_persona(session)  the ops user's real data:
        self      national id + phone (chat token claims), full name (/user/info/)
        cases     /api/v1/my-requests (the request number the portal SHOWS the user,
                  deceased name + id, heirs names + ids) joined with the chatbot tools
                  (property deeds, CR numbers) by request number
        tickets   tarekat_my_tickets
  * tag_entities(conv)      Claude tags every identifier in the USER turns, with its
                            owner (the user / the deceased / an heir / someone else),
                            as exact substrings. The model never invents replacements;
                            replacements come only from the persona.

Values the ops user has no real counterpart for (division numbers — none of the user's
requests is divided yet —, the inheritance-deed number, invoices, IBANs, people outside
the case) get a SYNTHETIC same-shape value, marked `synthetic` in the run, so no
production identifier is sent to ops.

PRIVACY: tagging sends the production conversation to the Anthropic API (approved by the
user, 2026-11-17). Raw tags are cached in the run directory (reports/, gitignored);
every log line and the report carry masked values only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re

from dotenv import load_dotenv

load_dotenv()

TAG_MODEL = os.environ.get("SWAP_MODEL", "claude-opus-5-5")

TYPES = [
    "self_national_id", "deceased_national_id", "heir_national_id", "other_national_id",
    "self_name", "deceased_name", "heir_name", "other_person_name",
    "request_number", "division_number", "heirs_deed_number", "property_deed_number",
    "invoice_number", "ticket_number", "phone", "iban", "cr_number", "other_identifier",
]

SCHEMA = {
    "type": "object",
    "properties": {
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "turn": {"type": "integer"},
                    "text": {"type": "string"},
                    "type": {"type": "string", "enum": TYPES},
                    "why": {"type": "string"},
                },
                "required": ["turn", "text", "type", "why"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["entities"],
    "additionalProperties": False,
}

SYSTEM = """You tag personal identifiers in Arabic chat messages from citizens using Tarekat
(the Saudi inheritance platform: heirs list, estate list, estate division).

Tag ONLY text inside the USER turns. Use the other messages (bot / human agent) only to
understand what a number or name is — e.g. an agent asking «زودني برقم هوية المورث» makes the
user's next bare number the deceased's national id.

For every identifier return:
- turn: the user turn number shown in [USER n]
- text: the EXACT substring as written (same digits — Arabic-Indic or Latin —, same spelling,
  no surrounding words). It must occur verbatim in that turn.
- type, from:
  self_national_id      the writer's own id / iqama
  deceased_national_id  the deceased's (المتوفى/المورث) id
  heir_national_id      another heir's id
  other_national_id     an id whose owner is unclear or outside the case
  self_name             the writer's own name
  deceased_name         the deceased's name (full or partial)
  heir_name             another heir's / a minor's / a relative's name in the case
  other_person_name     any other person (lawyer, employee, agent...)
  request_number        a Tarekat request (طلب) number
  division_number       an estate-division (قسمة) request number
  heirs_deed_number     an heirs deed (صك حصر الورثة / وثيقة حصر ورثة) number
  property_deed_number  a real-estate deed (صك عقار / صك ملكية) number
  invoice_number        an invoice / SADAD bill number
  ticket_number         a support ticket number
  phone, iban, cr_number (commercial register), other_identifier (any other number unique
  to this person or case: court judgment, guardianship deed, agency/وكالة number, ...)
- why: a few words of evidence.

Do NOT tag: amounts, dates, durations, counts, the 1950 hotline, generic words
(«الورثة», «المتوفى» without a name), religious phrases, or the platform's own names.
If a name of God or a common word could be read as a name, do not tag it.
Return {"entities": []} when there is nothing to tag."""

DIGITS_IN = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
DIGITS_AR = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
GENERIC_NAMES = ["سالم عبدالله العتيبي", "نورة محمد القحطاني", "خالد سعد الدوسري",
                 "منيرة ناصر الشمري", "فهد علي الغامدي", "هيا عبدالعزيز المطيري"]


def mask(value) -> str:
    v = str(value or "")
    if re.fullmatch(r"[\d٠-٩۰-۹]+", v):
        return "••" + v.translate(DIGITS_IN)[-4:]
    if re.fullmatch(r"SA\d+", v):
        return "SA••" + v[-4:]
    words = v.split()
    return " ".join(w[0] + "…" for w in words) if words else "—"


# ---- persona -----------------------------------------------------------------------

_MY_REQUESTS_JS = """async (api) => {
  const c = Object.fromEntries(document.cookie.split(';').map(x => x.trim().split('=')));
  const r = await fetch(api + '/api/v1/my-requests?sort=asc',
                        {headers: {Authorization: 'Bearer ' + c.access_token}});
  if (!r.ok) return {error: r.status};
  const j = await r.json();
  return Array.isArray(j.result) ? j.result : (Object.values(j.result || {}).find(Array.isArray) || []);
}"""

_USER_INFO_JS = """async (api) => {
  const c = Object.fromEntries(document.cookie.split(';').map(x => x.trim().split('=')));
  const r = await fetch(api + '/api/v1/user/info/', {headers: {Authorization: 'Bearer ' + c.access_token}});
  return r.ok ? (await r.json()).result : {error: r.status};
}"""


def build_persona(session) -> dict:
    """The signed-in ops user's own data, read through the portal and the chatbot tools."""
    import httpx
    from tools.ops_session import jwt_claims

    session.keepalive(every_s=0)
    chat = session._chat_token()
    claims = jwt_claims(chat)
    api = str(claims.get("iss") or "").rstrip("/")
    page = session._page
    info = page.evaluate(_USER_INFO_JS, api) or {}
    rows = page.evaluate(_MY_REQUESTS_JS, api)
    if isinstance(rows, dict):
        raise RuntimeError(f"/api/v1/my-requests failed: {rows}")
    user = claims.get("user") or {}
    nid = str(user.get("nid") or info.get("id_number") or "")
    name = " ".join(p for p in (info.get("first_name"), info.get("father_name"),
                                info.get("grandfather_name"), info.get("family_name")) if p)

    def tool(path, **q):
        r = httpx.get(f"{api}/api/v1/chat/tools/{path}", params=q,
                      headers={"Authorization": f"Bearer {session._chat_token()}"}, timeout=60)
        return (r.json() or {}).get("result") or {}

    cases = []
    for row in rows:
        number = str(row.get("tawtheeq_request_number") or row.get("application_external_id") or "")
        deceased_id = str(row.get("deceased_social_id") or "")
        heirs = [{"name": h.get("full_name"), "id": str(h.get("social_id") or ""),
                  "relationship": h.get("relationship_to_deceased")}
                 for h in row.get("heirs") or [] if h.get("full_name")]
        if not number.isdigit() or not deceased_id or not heirs:
            continue
        cases.append({"request_number": number, "deceased_name": row.get("deceased_full_name"),
                      "deceased_id": deceased_id, "heirs": heirs,
                      "user_is_heir": any(h["id"] == nid for h in heirs),
                      "property_deeds": [], "cr_numbers": []})
    # Richest cases first: the user is an heir, other heirs exist; then enrich the top ones.
    cases.sort(key=lambda c: (not c["user_is_heir"], -len(c["heirs"])))
    for c in cases[:12]:
        session.keepalive()
        assets = tool("request-assets", request_number=c["request_number"])
        for a in assets.get("assets") or []:
            if a.get("deed_number"):
                c["property_deeds"].append(str(a["deed_number"]))
            if a.get("cr_number"):
                c["cr_numbers"].append(str(a["cr_number"]))
    tickets = [str(t.get("ticket_number")) for t in (tool("my-tickets").get("tickets") or [])
               if t.get("ticket_number")]
    if not cases:
        raise RuntimeError("the ops user has no request with a deceased id and heirs to swap in.")
    # Values the portal does not expose, given by the tester (.env); empty = synthetic.
    static = {k: v for k, v in (("heirs_deed_number", os.environ.get("OPS_HEIRS_DEED_NUMBER")),
                                ("iban", os.environ.get("OPS_IBAN"))) if v}
    return {"static": static,
            "self": {"national_id": nid, "name": name,
                     "phone": str(user.get("phone") or info.get("mobile_number") or "")},
            "cases": cases[:12], "tickets": tickets,
            "all_property_deeds": sorted({d for c in cases for d in c["property_deeds"]}),
            "all_cr_numbers": sorted({d for c in cases for d in c["cr_numbers"]})}


# ---- tagging -----------------------------------------------------------------------

def _transcript(conv: dict) -> str:
    lines, n = [], 0
    for m in conv.get("messages") or []:
        text = (m.get("content") or "").strip()
        if not text:
            continue
        if m.get("role") == "user":
            n += 1
            lines.append(f"[USER {n}]\n{text}")
        else:
            who = "HUMAN AGENT" if m.get("role") == "agent" else "BOT"
            lines.append(f"[{who}]\n{text[:1500]}")
    return "\n\n".join(lines)


def tag_entities(conv: dict, client=None) -> list[dict]:
    """Claude's tags for the user turns; only exact substrings are kept."""
    import anthropic
    client = client or anthropic.Anthropic()
    response = client.beta.messages.create(
        model=TAG_MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": f"<conversation>\n{_transcript(conv)}\n</conversation>"}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"tagging refused for {conv['prod_conversation_id'][:8]}")
    data = json.loads(next(b.text for b in response.content if b.type == "text"))
    turns = conv["user_turns"]
    kept = []
    for e in data.get("entities") or []:
        i = e.get("turn", 0) - 1
        text = (e.get("text") or "").strip()
        if 0 <= i < len(turns) and len(text) >= 2 and text in turns[i]:
            kept.append({"turn": i + 1, "text": text, "type": e["type"], "why": e.get("why", "")})
    return kept


# ---- rule-based tagging (no API key) ----------------------------------------------
#
# Classification by shape, then by exclusion:
#   SA + 22 digits                    iban
#   05xxxxxxxx / 9665xxxxxxxx         phone
#   10 digits starting 1 or 2         national id (deceased if the turn or the bot's
#                                     previous message talks about the deceased, else self)
#   any other 5-20 digit number       deed or request number: «حصر» -> heirs deed,
#                                     «صك» -> property deed, «تذكرة/بلاغ» -> ticket,
#                                     «فاتورة/سداد» -> invoice, otherwise request number
#   amounts (followed by ريال / ر.س) are left alone.
# Names are NOT tagged: production names reach ops unchanged in this mode.

_D = "[\\d٠-٩۰-۹]"
_R_IBAN = re.compile(r"SA(?:\s?" + _D + r"){22}", re.I)
_R_NUM = re.compile(r"(?<![\d٠-٩۰-۹A-Za-z_+-])\+?" + _D + r"{5,20}(?![\d٠-٩۰-۹A-Za-z_-])")
_R_AMOUNT_AFTER = re.compile(r"^\s*(?:ريال|ر\.?\s?س|SAR|﷼)")
_R_DECEASED = re.compile(r"مورث|المتوف|متوف|الميت|والد|المرحوم|رحمه")
_R_HEIRS_DEED = re.compile(r"حصر")
_R_DEED = re.compile(r"صك|وثيق")
_R_TICKET = re.compile(r"تذكر|بلاغ")
_R_INVOICE = re.compile(r"فاتور|سداد")


def _prev_bot(conv: dict, user_index: int) -> str:
    seen, last = -1, ""
    for m in conv.get("messages") or []:
        if m.get("role") == "user" and (m.get("content") or "").strip():
            seen += 1
            if seen == user_index:
                return last
        elif m.get("role") in ("assistant", "agent"):
            last = m.get("content") or ""
    return ""


def tag_entities_rules(conv: dict) -> list[dict]:
    """Same output as tag_entities, from shape + nearby words only."""
    tags = []
    for i, text in enumerate(conv["user_turns"]):
        bot = _prev_bot(conv, i)[-400:]
        taken = []
        for m in _R_IBAN.finditer(text):
            tags.append({"turn": i + 1, "text": m.group(), "type": "iban", "why": "SA+22"})
            taken.append(m.span())
        for m in _R_NUM.finditer(text):
            if any(a <= m.start() < b for a, b in taken):
                continue
            raw = m.group()
            d = raw.translate(DIGITS_IN).lstrip("+")
            ctx = text[max(0, m.start() - 40):m.start()]
            if _R_AMOUNT_AFTER.match(text[m.end():]):
                continue
            if re.fullmatch(r"05\d{8}|(?:00)?9665\d{8}|5\d{8}", d):
                typ, why = "phone", "phone shape"
            elif len(d) == 10 and d[0] in "12":
                near = ctx + " " + bot
                typ = "deceased_national_id" if _R_DECEASED.search(near) else "self_national_id"
                why = "10 digits, starts 1/2"
            else:
                near = ctx or bot              # the word before it, else what the bot asked
                if _R_HEIRS_DEED.search(near):
                    typ = "heirs_deed_number"
                elif _R_DEED.search(near):
                    typ = "property_deed_number"
                elif _R_TICKET.search(near):
                    typ = "ticket_number"
                elif _R_INVOICE.search(near):
                    typ = "invoice_number"
                else:
                    typ = "request_number"
                why = "by exclusion"
            tags.append({"turn": i + 1, "text": raw, "type": typ, "why": why})
    return tags


# ---- substitution ------------------------------------------------------------------

def _synthetic_number(original: str, salt: str) -> str:
    """Same length, same first digit, deterministic, never the original."""
    latin = original.translate(DIGITS_IN)
    h = hashlib.sha256((salt + latin).encode()).hexdigest()
    body = "".join(str(int(c, 16) % 10) for c in h)[: max(0, len(latin) - 1)]
    out = latin[:1] + body
    return out if out != latin else latin[:1] + body[::-1]


def _shape_like(original: str, replacement: str) -> str:
    """Keep the user's digit script; for names keep the user's number of words."""
    if re.fullmatch(r"[\d٠-٩۰-۹]+", original):
        return replacement.translate(DIGITS_AR) if original != original.translate(DIGITS_IN) \
            else replacement
    words = original.split()
    rep = replacement.split()
    return " ".join(rep[: max(1, len(words))]) if rep else replacement


def plan_swaps(conv: dict, tags: list[dict], persona: dict, case_index: int):
    """{original text: (replacement, type, source)} for one conversation, and the case used."""
    case = persona["cases"][case_index % len(persona["cases"])]
    me = persona["self"]
    others = [h for h in case["heirs"] if h["id"] != me["national_id"]]
    pools = {"heir_id": [h["id"] for h in others], "heir_name": [h["name"] for h in others]}
    used = {k: 0 for k in ("heir_id", "heir_name", "deed", "cr", "generic")}
    plan: dict[str, tuple[str, str, str]] = {}

    def nxt(pool_key, pool):
        if not pool:
            return None
        v = pool[used[pool_key] % len(pool)]
        used[pool_key] += 1
        return v

    for t in tags:
        orig, typ = t["text"], t["type"]
        if orig in plan:
            continue
        real = None
        if typ == "self_national_id":
            real = me["national_id"]
        elif typ == "deceased_national_id":
            real = case["deceased_id"]
        elif typ == "heir_national_id":
            real = nxt("heir_id", pools["heir_id"])
        elif typ == "self_name":
            # The name on the user's own heir record in this case: on ops it differs from
            # the /user/info profile name, and the case's heir list is what the tools show.
            real = next((h["name"] for h in case["heirs"] if h["id"] == me["national_id"]),
                        None) or me["name"]
        elif typ == "deceased_name":
            real = case["deceased_name"]
        elif typ == "heir_name":
            real = nxt("heir_name", pools["heir_name"])
        elif typ == "request_number":
            real = case["request_number"]
        elif typ == "property_deed_number":
            real = nxt("deed", case["property_deeds"] or persona["all_property_deeds"])
        elif typ == "cr_number":
            real = nxt("cr", case["cr_numbers"] or persona["all_cr_numbers"])
        elif typ == "ticket_number":
            real = persona["tickets"][0] if persona["tickets"] else None
        elif typ == "phone":
            real = me["phone"] or None
        elif typ in persona.get("static", {}):
            real = persona["static"][typ]
        if real:
            plan[orig] = (_shape_like(orig, real), typ, "ops")
            continue
        # No real ops counterpart: same-shape synthetic, never the production value.
        latin = orig.translate(DIGITS_IN)
        if re.fullmatch(r"\d+", latin):
            plan[orig] = (_shape_like(orig, _synthetic_number(orig, conv["prod_conversation_id"])),
                          typ, "synthetic")
        elif re.fullmatch(r"SA\d{22}", latin.replace(" ", "")):
            plan[orig] = ("SA" + _synthetic_number(latin[2:], "iban"), typ, "synthetic")
        else:
            plan[orig] = (_shape_like(orig, GENERIC_NAMES[used["generic"] % len(GENERIC_NAMES)]),
                          typ, "synthetic")
            used["generic"] += 1
    return plan, case


def apply_swaps(conv: dict, plan: dict) -> tuple[list[str], list[dict]]:
    """The user turns to send, and per-turn masked records of what changed."""
    sent, records = [], []
    if not plan:
        return list(conv["user_turns"]), records
    # One pass, longest original first (a full name before a part of it), so a
    # replacement is never itself rewritten by a later, shorter original.
    pattern = re.compile("|".join(re.escape(o) for o in sorted(plan, key=len, reverse=True)))
    for i, text in enumerate(conv["user_turns"], 1):
        def sub(m, i=i):
            rep, typ, src = plan[m.group()]
            records.append({"turn": i, "type": typ, "original": mask(m.group()),
                            "replacement": mask(rep), "source": src})
            return rep
        sent.append(pattern.sub(sub, text))
    return sent, records
