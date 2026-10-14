"""Shared fixtures + the tolerant matchers that make LLM assertions non-flaky."""

import json
import pathlib
import re

import pytest
import yaml

from agent_client import AgentClient, Turn

ROOT = pathlib.Path(__file__).parent
REPORTS = ROOT / "reports"


def pytest_configure(config):
    REPORTS.mkdir(exist_ok=True)
    for m in ("contract", "functional", "robustness", "perf", "slow"):
        config.addinivalue_line("markers", m)


@pytest.fixture(scope="session")
def client():
    with AgentClient(timeout=120) as c:
        yield c


@pytest.fixture
def thread(client):
    """Fresh multi-turn conversation."""
    return client.conversation()


def load_golden(name: str = "golden.yaml") -> list[dict]:
    return yaml.safe_load((ROOT / "data" / name).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Matchers. LLM output is non-deterministic, so never assert on exact strings —
# assert on invariants: did it search, did it abstain, does it contain the facts,
# does it avoid the forbidden.
# --------------------------------------------------------------------------

def _norm(s: str) -> str:
    """Arabic-tolerant normalisation: strip diacritics, unify alef/ya/ta-marbuta."""
    s = re.sub(r"[ً-ٰٟـ]", "", s)      # harakat + tatweel
    s = s.translate(str.maketrans("أإآىة", "ااايه"))
    return re.sub(r"\s+", " ", s).strip().lower()


def contains_any(text: str, needles: list[str]) -> list[str]:
    """Returns the needles that were NOT found (empty list == at least one hit)."""
    n = _norm(text)
    return [] if any(_norm(x) in n for x in needles) else needles


def missing_all(text: str, needles: list[str]) -> list[str]:
    n = _norm(text)
    return [x for x in needles if _norm(x) not in n]


def present_forbidden(text: str, needles: list[str]) -> list[str]:
    n = _norm(text)
    return [x for x in needles if _norm(x) in n]


ARABIC = re.compile(r"[؀-ۿ]")
LATIN = re.compile(r"[A-Za-z]")


def dominant_lang(text: str) -> str:
    ar, en = len(ARABIC.findall(text)), len(LATIN.findall(text))
    if ar == en == 0:
        return "none"
    return "ar" if ar >= en else "en"


def assert_turn(turn: Turn, expect: dict) -> None:
    """Apply a golden-file `expect` block to a Turn. One assert per rule, loud messages."""
    ctx = f"\nQ: {turn.user_input}\nA: {turn.response[:600]}\nrun_id: {turn.run_id}"

    assert turn.response.strip(), f"empty response{ctx}"

    if (want := expect.get("searched_kb")) is not None:
        assert turn.searched_kb is want, (
            f"expected searched_kb={want}, got {turn.searched_kb} "
            f"(tools: {[s.get('tool') for s in turn.steps]}){ctx}")

    if (want := expect.get("abstains")) is not None:
        got = turn.abstention_reason is not None
        assert got is want, f"expected abstains={want}, abstention_reason={turn.abstention_reason}{ctx}"

    if needles := expect.get("any_of"):
        assert not contains_any(turn.response, needles), \
            f"none of the expected terms appeared: {needles}{ctx}"

    if needles := expect.get("all_of"):
        assert not (miss := missing_all(turn.response, needles)), \
            f"missing required terms: {miss}{ctx}"

    if needles := expect.get("none_of"):
        assert not (bad := present_forbidden(turn.response, needles)), \
            f"forbidden terms present: {bad}{ctx}"

    if want := expect.get("lang"):
        assert dominant_lang(turn.response) == want, \
            f"expected reply in '{want}', got '{dominant_lang(turn.response)}'{ctx}"

    if (cap := expect.get("max_ms")) is not None:
        assert turn.total_ms <= cap, f"took {turn.total_ms}ms, budget {cap}ms{ctx}"


# --------------------------------------------------------------------------
# Latency collection — every turn any test makes gets logged for the perf report.
# --------------------------------------------------------------------------

_LATENCIES: list[dict] = []


def record(turn: Turn, tag: str = "") -> Turn:
    _LATENCIES.append({"tag": tag, "q": turn.user_input[:80], "ttfb_ms": turn.ttfb_ms,
                       "first_content_ms": turn.first_content_ms, "total_ms": turn.total_ms,
                       "content_frames": turn.content_frames, "run_id": turn.run_id})
    return turn


def pytest_sessionfinish(session, exitstatus):
    if _LATENCIES:
        (REPORTS / "latency.json").write_text(
            json.dumps(_LATENCIES, ensure_ascii=False, indent=1), encoding="utf-8")
