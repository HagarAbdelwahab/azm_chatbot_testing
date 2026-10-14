"""Authenticated read-side client for the Agent Studio admin API.

`agent_client.py` speaks the *widget* contract with a publishable, origin-checked key
scoped to one tenant. That key cannot read another tenant's history. Pulling production
conversations off Tarekat-Restricted needs the admin JWT instead, which is what this
module gets and nothing else does.

Verified against the live deployment (2026-09-01):
  POST /api/v1/auth/login                        {"email","password"} -> {"access_token"}
  GET  /api/v1/conversation-summaries            ?tenant_id&start_date&end_date&limit&offset
                                                 &bot_id&q&include_test -> {items,total,limit,offset}
  GET  /api/v1/conversation-summaries/{id}/transcript?tenant_id
                                                 -> {..., messages:[{id,role,content,created_at}]}
  GET  /api/v1/bots?tenant_id                    -> [bot]        (model lives on the AGENT, not here)
  GET  /api/v1/agents/{agent_id}?tenant_id       -> agent config (model_ref_id, temperature, top_k…)
  GET  /api/v1/models?tenant_id                  -> [model]      (resolves model_ref_id -> a name)

HARD-WON: `start_date`/`end_date` are compared against a timestamp, not a date. Passing
a bare `2026-08-31` matches NOTHING and returns `total: 0` — an empty day that looks like
a quiet day rather than a bad query. Always send full ISO-8601 bounds; `day_bounds()`
below is the only supported way to build them.

The token is never written to disk. Each process logs in once and holds it in memory.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE = os.environ.get("AGENT_API", "https://api.chatbot-studio.azm-cit.com/api/v1")

# Tenants on this deployment (GET /api/v1/tenants). RESTRICTED is where real citizens
# talk to the bot; TESTING is the sandbox whose model/config we change and re-measure.
TENANTS = {
    "restricted": "96cf3f00-3084-44c1-8b70-820073c8a516",   # Tarekat-Restricted — PRODUCTION
    "testing":    "b00c3019-36b6-4da6-9ce6-fc1519e04f05",   # Tarekat-Testing   — replay target
    "insaf":      "3a26e3f9-8f38-4b75-ae01-0012bc74a54f",
    "test":       "0a8dee86-e643-47c1-b98c-14260ff39abb",
}


def resolve_tenant(name_or_id: str) -> str:
    """Accept a friendly name or a raw uuid, so callers never hard-code a uuid."""
    return TENANTS.get(name_or_id.strip().lower(), name_or_id.strip())


def day_bounds(day: str) -> tuple[str, str]:
    """'2026-08-31' -> inclusive ISO bounds covering that whole UTC day.

    Timestamps from this API carry a trailing 'Z', so the day is a UTC day. Riyadh is
    UTC+3: a conversation at 01:00 Riyadh belongs to the PREVIOUS UTC day. Say which
    convention you used when reporting counts.
    """
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = d + timedelta(days=1) - timedelta(microseconds=1)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class StudioAdmin:
    def __init__(self, timeout: float = 60.0):
        email = os.environ.get("STUDIO_EMAIL")
        password = os.environ.get("STUDIO_PASSWORD")
        if not email or not password:
            raise RuntimeError(
                "STUDIO_EMAIL / STUDIO_PASSWORD missing from .env — the admin API needs "
                "them to read another tenant's history. The widget AGENT_API_KEY cannot."
            )
        self._c = httpx.Client(base_url=BASE, timeout=timeout)
        r = self._c.post("/auth/login", json={"email": email, "password": password})
        if r.status_code == 401:
            raise RuntimeError("Studio login rejected the credentials in .env (401).")
        r.raise_for_status()
        token = r.json().get("access_token")
        if not token:
            raise RuntimeError(f"login returned no access_token: keys={list(r.json())}")
        self._c.headers["authorization"] = f"Bearer {token}"

    def get(self, path: str, **params):
        r = self._c.get(path, params={k: v for k, v in params.items() if v is not None})
        if r.status_code == 401:
            raise RuntimeError(f"401 on {path} — the admin token was rejected or expired.")
        if r.status_code == 403:
            raise RuntimeError(f"403 on {path} — this account cannot read tenant {params.get('tenant_id')}.")
        r.raise_for_status()
        return r.json()

    # ---- conversations -------------------------------------------------------

    def list_conversations(self, tenant_id: str, start: str | None = None,
                           end: str | None = None, page: int = 100,
                           include_test: bool = False, bot_id: str | None = None,
                           q: str | None = None, cap: int | None = None) -> list[dict]:
        """Every conversation summary in the window, following pagination to the end.

        `include_test=False` (the API default) excludes playground traffic, so what comes
        back is real user conversations.
        """
        out: list[dict] = []
        offset = 0
        while True:
            d = self.get("/conversation-summaries", tenant_id=tenant_id,
                         start_date=start, end_date=end, limit=page, offset=offset,
                         include_test=str(include_test).lower(), bot_id=bot_id, q=q)
            items = d.get("items", [])
            out.extend(items)
            total = d.get("total", len(out))
            offset += page
            if cap and len(out) >= cap:
                return out[:cap]
            if not items or offset >= total:
                return out

    def transcript(self, conversation_id: str, tenant_id: str) -> dict:
        return self.get(f"/conversation-summaries/{conversation_id}/transcript",
                        tenant_id=tenant_id)

    # ---- configuration snapshot ---------------------------------------------

    def config_snapshot(self, tenant_id: str, bot_id: str | None = None,
                        agent_id: str | None = None) -> dict:
        """What the tenant was actually running — the thing the reviewer is comparing.

        A replay is meaningless without it: "the answer changed" only means something
        next to "the model went from Claude-Sonnet-4.5 to Gemini 3.1 Pro".
        """
        snap: dict = {"tenant_id": tenant_id}
        try:
            bots = self.get("/bots", tenant_id=tenant_id)
            bots = bots if isinstance(bots, list) else bots.get("items", [])
        except Exception as e:                     # config is context, never fatal
            snap["error"] = f"bots: {e}"
            return snap

        bot = None
        if bot_id:
            bot = next((b for b in bots if b.get("id") == bot_id), None)
        if bot is None:
            bot = next((b for b in bots if b.get("is_default")), bots[0] if bots else None)
        if bot:
            snap["bot"] = {k: bot.get(k) for k in
                           ("id", "name", "channel_type", "agent_id", "flow_id",
                            "agentic_rollout_percentage", "is_default", "is_active")}

        aid = agent_id or (bot or {}).get("agent_id")
        if not aid:
            return snap
        try:
            a = self.get(f"/agents/{aid}", tenant_id=tenant_id)
        except Exception as e:
            snap["error"] = f"agent {aid}: {e}"
            return snap

        models = []
        try:
            m = self.get("/models", tenant_id=tenant_id)
            models = m if isinstance(m, list) else m.get("items", [])
        except Exception:
            pass
        model = next((m for m in models if m.get("id") == a.get("model_ref_id")), None)

        snap["agent"] = {
            "id": a.get("id"), "name": a.get("name"),
            "model": (model or {}).get("name") or a.get("model_ref_id"),
            "model_id": (model or {}).get("model_id"),
            "model_ref_id": a.get("model_ref_id"),
            "temperature": a.get("temperature"),
            "top_k": a.get("top_k"),
            "relevance_threshold": a.get("relevance_threshold"),
            "retrieval_mode": a.get("retrieval_mode"),
            "strict_grounding": a.get("strict_grounding"),
            "faithfulness_check": a.get("faithfulness_check"),
            "faithfulness_threshold": a.get("faithfulness_threshold"),
            "require_citations": a.get("require_citations"),
            "min_score_to_answer": a.get("min_score_to_answer"),
            "max_steps": a.get("max_steps"),
            "allowed_tools": a.get("allowed_tools"),
            "knowledge_base_ids": a.get("knowledge_base_ids"),
            "clarification_enabled": a.get("clarification_enabled"),
            "clarification_max_turns": a.get("clarification_max_turns"),
            "system_prompt_chars": len(a.get("system_prompt") or ""),
            # The prompt itself is long and often the thing that changed; keep a hash so a
            # silent edit is detectable without dumping it into the report.
            "system_prompt_sha": _sha(a.get("system_prompt") or ""),
        }
        return snap

    def close(self) -> None:
        self._c.close()

    def __enter__(self): return self
    def __exit__(self, *_): self.close()


def _sha(s: str) -> str:
    import hashlib
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]


def config_diff(prod: dict, test: dict) -> list[dict]:
    """Field-by-field difference between two config snapshots, agent fields only."""
    pa, ta = prod.get("agent") or {}, test.get("agent") or {}
    rows = []
    for k in sorted(set(pa) | set(ta)):
        pv, tv = pa.get(k), ta.get(k)
        rows.append({"field": k, "prod": pv, "testing": tv, "changed": pv != tv})
    return rows
