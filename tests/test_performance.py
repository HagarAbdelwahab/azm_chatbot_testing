"""
Layer 4 — latency and load. Marked `slow`; excluded from the default run.
    .venv/bin/pytest -m perf
"""

import concurrent.futures as cf
import json
import pathlib
import statistics

import pytest

from agent_client import AgentClient
from conftest import REPORTS, record

pytestmark = [pytest.mark.perf, pytest.mark.slow]

# Budgets. Tune these to the SLA you actually agree with the vendor — they are
# currently set to what a chat UX can tolerate, and the bot does NOT meet them.
BUDGET_P50_MS = 5_000
BUDGET_P95_MS = 12_000

LOAD_QUESTIONS = [
    "ما هي منصة تركات؟",
    "كيف اقدم طلب حصر ورثة؟",
    "ما المستندات المطلوبة؟",
    "كم تستغرق مدة المعالجة؟",
    "ما هو التخارج؟",
    "What is estate division?",
]


def _pct(values: list[int], p: float) -> int:
    s = sorted(values)
    return s[min(int(len(s) * p), len(s) - 1)]


@pytest.mark.xfail(reason="KNOWN: p50 latency is ~10s — see FINDINGS.md #6", strict=False)
def test_latency_budget(client):
    lat = [record(client.ask(q), "perf-serial").total_ms for q in LOAD_QUESTIONS]
    p50, p95 = _pct(lat, 0.5), _pct(lat, 0.95)
    (REPORTS / "latency_summary.json").write_text(json.dumps(
        {"n": len(lat), "p50": p50, "p95": p95, "max": max(lat), "all": lat}, indent=1))
    assert p50 <= BUDGET_P50_MS, f"p50 {p50}ms > {BUDGET_P50_MS}ms (all: {lat})"
    assert p95 <= BUDGET_P95_MS, f"p95 {p95}ms > {BUDGET_P95_MS}ms (all: {lat})"


@pytest.mark.xfail(reason="KNOWN: answers arrive as one frame — see FINDINGS.md #7", strict=False)
def test_response_is_actually_streamed(client):
    """The endpoint is an SSE stream, but the answer arrives as a single frame,
    so the user stares at a spinner for the full generation time. Real streaming
    means many content frames and first_content_ms << total_ms."""
    turn = record(client.ask("اشرح بالتفصيل جميع خطوات قسمة التركة"), "perf-stream")
    assert turn.content_frames > 1, \
        f"only {turn.content_frames} content frame — no token streaming"
    assert turn.first_content_ms < turn.total_ms * 0.5, \
        f"first text at {turn.first_content_ms}ms of {turn.total_ms}ms total"


@pytest.mark.parametrize("concurrency", [5, 10])
def test_concurrent_users(concurrency):
    """Each virtual user gets its own client + conversation, like real widget traffic."""
    def one(i):
        with AgentClient(timeout=180) as c:
            return c.ask(LOAD_QUESTIONS[i % len(LOAD_QUESTIONS)])

    with cf.ThreadPoolExecutor(concurrency) as ex:
        turns = list(ex.map(one, range(concurrency)))

    for t in turns:
        record(t, f"perf-load-{concurrency}")

    failures = [t for t in turns if not t.response.strip()]
    assert not failures, f"{len(failures)}/{concurrency} concurrent requests returned nothing"

    lat = [t.total_ms for t in turns]
    print(f"\nconcurrency={concurrency} p50={_pct(lat,0.5)}ms p95={_pct(lat,0.95)}ms "
          f"max={max(lat)}ms mean={int(statistics.mean(lat))}ms")


def test_conversation_creation_is_cheap(client):
    """new_conversation() runs before every widget session — it must not be slow."""
    import time
    ms = []
    for _ in range(5):
        t0 = time.perf_counter()
        client.new_conversation()
        ms.append(int((time.perf_counter() - t0) * 1000))
    assert _pct(ms, 0.95) < 2_000, f"conversation creation p95 {_pct(ms,0.95)}ms (all: {ms})"
