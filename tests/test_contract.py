"""
Layer 1 — API contract. Deterministic, fast, no LLM judgement.
These are the tests that must stay green after every vendor deploy; when one
goes red the client is out of date, not the bot.
"""

import os
import uuid

import httpx
import pytest

from agent_client import BASE, KEY, AgentClient
from conftest import record

pytestmark = pytest.mark.contract


def test_new_conversation_returns_uuid(client):
    cid = client.new_conversation()
    uuid.UUID(cid)          # raises if not a uuid


def test_response_frame_schema_is_stable(client):
    """Drift detector. If the vendor adds an SSE field, this fails before the
    data silently disappears from every downstream metric."""
    turn = record(client.ask("ما هي منصة تركات؟"), "contract")
    assert not turn.unknown_keys, (
        f"unrecognised SSE keys {turn.unknown_keys} — update KNOWN_FRAME_KEYS "
        f"and _extract() in agent_client.py, then re-run probe()")


def test_done_frame_carries_ids(client):
    turn = record(client.ask("كيف اقدم طلب حصر ورثة؟"), "contract")
    uuid.UUID(turn.message_id)
    uuid.UUID(turn.run_id)   # the handle for Studio → Runs & Traces


def test_tool_calls_are_paired(client):
    """Every tool_call must get a tool_result — an orphan call means a hung tool."""
    turn = record(client.ask("ما المستندات المطلوبة لحصر الورثة؟"), "contract")
    calls = [s["seq"] for s in turn.steps if s["kind"] == "tool_call"]
    results = [s["seq"] for s in turn.steps if s["kind"] == "tool_result"]
    assert sorted(calls) == sorted(results), f"unpaired tool steps: {turn.steps}"
    assert all(s.get("ok") for s in turn.steps if s["kind"] == "tool_result"), \
        f"a tool reported failure: {turn.steps}"


def test_rejects_missing_api_key():
    with httpx.Client(base_url=BASE, timeout=30) as c:
        r = c.post("/conversations", content=b"")
    assert r.status_code in (401, 403), f"unauthenticated request got {r.status_code}"


def test_rejects_invalid_api_key():
    with httpx.Client(base_url=BASE, timeout=30,
                      headers={"x-api-key": "cgw--not-a-real-key"}) as c:
        r = c.post("/conversations", content=b"")
    assert r.status_code in (401, 403), f"bogus key got {r.status_code}"


def test_enforces_allowed_origin():
    """Settings → Allowed origins is *.web.azm-dev.com + localhost:4203.
    A widget key presented from another site must be refused."""
    with httpx.Client(base_url=BASE, timeout=30, headers={
            "x-api-key": KEY, "origin": "https://evil.example.com",
            "referer": "https://evil.example.com/"}) as c:
        r = c.post("/conversations", content=b"")
    assert r.status_code in (401, 403), \
        f"widget key accepted from a disallowed origin ({r.status_code})"


def test_origin_check_is_browser_only_by_design():
    """Documents the real boundary: with NO Origin header the key works. An origin
    allow-list can only stop another *website* embedding the widget — it is not an
    access control. Anything reachable with this key must be safe to expose publicly.
    This test exists so the assumption is explicit, not discovered later."""
    with httpx.Client(base_url=BASE, timeout=30,
                      headers={"x-api-key": KEY}) as c:
        r = c.post("/conversations", content=b"")
    assert r.status_code < 300, \
        "server now rejects origin-less calls — good news, but the test harness needs an origin"


def test_unknown_conversation_id_is_rejected(client):
    """A guessed/forged conversation id must not be writable."""
    with pytest.raises(httpx.HTTPStatusError) as e:
        client.ask("مرحبا", conversation_id=str(uuid.uuid4()))
    assert e.value.response.status_code in (400, 403, 404)


def test_oversized_message_is_handled(client):
    """64 KB of text — expect a clean 4xx or a normal answer, never a 5xx."""
    try:
        turn = record(client.ask("حصر ورثة " * 8000), "contract-oversize")
        assert turn.response.strip()
    except httpx.HTTPStatusError as e:
        assert 400 <= e.response.status_code < 500, \
            f"oversized input caused {e.response.status_code}"


@pytest.mark.parametrize("body", [{}, {"message": ""}, {"message": None}, {"msg": "x"}])
def test_malformed_body_returns_4xx_not_5xx(client, body):
    cid = client.new_conversation()
    r = client._c.post("/chat/completions/stream", params={"conversation_id": cid}, json=body)
    assert r.status_code < 500, f"body {body} caused {r.status_code}"
