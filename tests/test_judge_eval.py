"""
Offline tests for the turn judge (`judge.evaluate_turn`) and `run_conversations.py judge`.

No network, no API key: a fake Anthropic client returns canned JSON, so these check the
plumbing — the fixed verdict rules, what the judge is shown, resumability — not the
model's judgement. The model's judgement is calibrated with `python judge.py`.
"""

import json
from types import SimpleNamespace

import pytest

import judge
from tools import run_conversations as rc


def _dims(**over) -> dict:
    d = {"accuracy": "pass", "faithfulness": "pass", "retrieval": "relevant",
         "behavior": "pass", "severity": "none", "needs_human_review": False,
         "contradicted_facts": [], "missing_facts": [], "unsupported_claims": [],
         "reason": "ok"}
    return d | over


class FakeClient:
    def __init__(self, data: dict, stop_reason: str = "end_turn"):
        self.data, self.stop_reason, self.calls = data, stop_reason, []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(stop_reason=self.stop_reason,
                               content=[SimpleNamespace(type="text", text=json.dumps(self.data))])


# ---------------------------------------------------------------- verdict rules

def test_no_approved_answer_forces_accuracy_na():
    j = judge.finalize(_dims(accuracy="fail"), has_approved=False, has_chunks=True, abstained=False)
    assert j.accuracy == "n_a" and j.verdict == "pass"


def test_abstention_with_nothing_retrieved_is_not_unfaithful():
    j = judge.finalize(_dims(faithfulness="fail", retrieval="relevant"),
                       has_approved=False, has_chunks=False, abstained=True)
    assert (j.faithfulness, j.retrieval, j.verdict) == ("n_a", "none", "pass")


def test_any_failed_dimension_fails_the_turn_with_a_severity():
    j = judge.finalize(_dims(behavior="fail"), has_approved=True, has_chunks=True, abstained=False)
    assert j.verdict == "fail" and j.severity == "minor"


def test_unsure_judge_routes_to_human():
    j = judge.finalize(_dims(needs_human_review=True, severity="major"),
                       has_approved=True, has_chunks=True, abstained=False)
    assert j.verdict == "needs_review" and j.severity == "none"


def test_fail_beats_needs_review():
    j = judge.finalize(_dims(faithfulness="fail", needs_human_review=True, severity="critical"),
                       has_approved=False, has_chunks=True, abstained=False)
    assert j.verdict == "fail" and j.severity == "critical"


# ---------------------------------------------------------------- evaluate_turn

def test_judge_sees_chunks_approved_answer_and_history():
    fake = FakeClient(_dims())
    judge.evaluate_turn(question="كم الرسوم؟", answer="لا توجد رسوم.",
                        history=[{"role": "user", "text": "مرحبا"}, {"role": "bot", "text": "أهلاً"}],
                        chunks=["لا توجد رسوم على الخدمة."], approved="الخدمة مجانية.",
                        expected_behavior="KB_ANSWER", client=fake, model="m")
    call = fake.calls[0]
    prompt = call["messages"][0]["content"]
    assert "لا توجد رسوم على الخدمة." in prompt and "الخدمة مجانية." in prompt
    assert "[user] مرحبا" in prompt and "KB_ANSWER" in prompt
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert call["output_config"]["format"]["schema"] is judge.TURN_SCHEMA


def test_judge_refusal_becomes_needs_review_not_a_crash():
    j = judge.evaluate_turn(question="q", answer="a", client=FakeClient({}, "refusal"), model="m")
    assert j.verdict == "needs_review" and j.needs_human_review


def test_missing_key_raises_without_client(monkeypatch):
    monkeypatch.setattr(judge, "HAVE_KEY", False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        judge.evaluate_turn(question="q", answer="a")


# ---------------------------------------------------------------- run_conversations judge

STATE = {"T01": {
    "id": "T01", "question": "q1", "category": "", "expected_behavior": "KB_ANSWER",
    "risk": "", "approved_answer": "approved", "conversation_id": "c",
    "transcript": [
        {"role": "user", "text": "q1"},
        {"role": "bot", "text": "a1", "sources": ["chunk"], "run_id": "r1",
         "abstention_reason": None, "handoff_status": None, "confirmation_request": None},
        {"role": "user", "text": "q2"},
        {"role": "bot", "text": "a2", "sources": [], "run_id": "r2",
         "abstention_reason": None, "handoff_status": None, "confirmation_request": None},
    ]}}


@pytest.fixture
def run_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "OUT", tmp_path)
    monkeypatch.setattr(rc, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(rc, "JUDGMENTS", tmp_path / "judgments.json")
    monkeypatch.setattr(rc, "ROOT", tmp_path)
    (tmp_path / "state.json").write_text(json.dumps(STATE), encoding="utf-8")
    return tmp_path


def _args(**kw):
    return SimpleNamespace(**({"workers": 1, "fresh": False, "only": None, "model": "m",
                               "agree": None} | kw))


def test_judge_grades_every_bot_turn_and_resumes(run_dir, monkeypatch, capsys):
    seen = []

    def fake_eval(**kw):
        seen.append(kw)
        return judge.finalize(_dims(), has_approved=bool(kw["approved"]),
                              has_chunks=bool(kw["chunks"]), abstained=False, model="m")

    monkeypatch.setattr(judge, "HAVE_KEY", True)
    monkeypatch.setattr(judge, "evaluate_turn", fake_eval)
    rc.cmd_judge(_args())

    saved = json.loads((run_dir / "judgments.json").read_text(encoding="utf-8"))
    assert set(saved) == {"T01/1", "T01/2"}
    assert saved["T01/1"]["run_id"] == "r1"
    # approved answer only grades the opening turn; turn 2 sees turn 1 as history
    assert seen[0]["approved"] == "approved" and seen[1]["approved"] is None
    assert [h["text"] for h in seen[1]["history"]] == ["q1", "a1"]

    rc.cmd_judge(_args())                        # second run: nothing left to grade
    assert len(seen) == 2
    assert "already judged" in capsys.readouterr().out


def test_judge_without_key_exits_cleanly(run_dir, monkeypatch):
    monkeypatch.setattr(judge, "HAVE_KEY", False)
    with pytest.raises(SystemExit, match="ANTHROPIC_API_KEY"):
        rc.cmd_judge(_args())
    assert not (run_dir / "judgments.json").exists()


def test_agreement_counts_needs_review_as_flagged(run_dir, capsys):
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    state["T02"] = dict(state["T01"], id="T02", transcript=[
        {"role": "user", "text": "q"}, {"role": "bot", "text": "a", "run_id": "r3"}])
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "judgments.json").write_text(json.dumps({
        "T01/1": {"verdict": "pass", "run_id": "r1"},
        "T01/2": {"verdict": "needs_review", "run_id": "r2"},
        "T02/1": {"verdict": "pass", "run_id": "r3"}}), encoding="utf-8")
    sheet = run_dir / "reviewed.csv"
    sheet.write_text("id,verdict\nT01,fail\nT02,fail\n", encoding="utf-8")
    rc.judge_agreement(sheet)
    out = capsys.readouterr().out
    assert "agreement 1/2 = 50%" in out and "T02" in out


# ---------------------------------------------------------------- data bugs F1-F3

def _fake_eval(seen: list):
    def fake(**kw):
        seen.append(kw)
        return judge.finalize(_dims(), has_approved=bool(kw["approved"]),
                              has_chunks=bool(kw["chunks"]), abstained=False, model="m")
    return fake


def _edit_state(run_dir, fn):
    path = run_dir / "state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    fn(state)
    path.write_text(json.dumps(state), encoding="utf-8")


def test_turn_record_keeps_error_and_handoff_offer():
    turn = SimpleNamespace(
        user_input="q", response="", run_id="r", message_id="m", steps=[],
        retrieved_contexts=[], searched_kb=False, abstention_reason=None,
        confirmation_request=None, handoff_status=None, handoff_offer=True,
        error="stream_failed", pii_intercept_count=0, total_ms=1, content_frames=0,
        unknown_keys=set(), answer_quality_score=None, retrieval_score=None,
        faithfulness_score=None)
    bot = rc._turn_record(turn)[1]
    assert bot["error"] == "stream_failed" and bot["handoff_offer"] is True


def test_errored_turn_is_not_judged(run_dir, monkeypatch, capsys):          # F1
    _edit_state(run_dir, lambda s: s["T01"]["transcript"][3].update(
        text="", error="stream_failed"))
    seen = []
    monkeypatch.setattr(judge, "HAVE_KEY", True)
    monkeypatch.setattr(judge, "evaluate_turn", _fake_eval(seen))
    rc.cmd_judge(_args())
    assert [kw["answer"] for kw in seen] == ["a1"]
    saved = json.loads((run_dir / "judgments.json").read_text(encoding="utf-8"))
    assert "T01/2" not in saved
    assert "skipped 1 errored turn(s)" in capsys.readouterr().out


def test_reasked_turn_is_regraded_and_stale_verdict_hidden(run_dir, monkeypatch):  # F2
    seen = []
    monkeypatch.setattr(judge, "HAVE_KEY", True)
    monkeypatch.setattr(judge, "evaluate_turn", _fake_eval(seen))
    rc.cmd_judge(_args())
    assert len(seen) == 2

    _edit_state(run_dir, lambda s: s["T01"]["transcript"][3].update(
        text="a2-new", run_id="r2-new"))
    state = rc.load_state()
    current = rc.current_judgments(state, rc.load_judgments())
    assert set(current) == {"T01/1"}             # old verdict for T01/2 no longer shown

    rc.cmd_judge(_args())
    assert [kw["answer"] for kw in seen[2:]] == ["a2-new"]
    assert rc.load_judgments()["T01/2"]["run_id"] == "r2-new"


def test_handoff_offer_reaches_the_judge(run_dir, monkeypatch):            # F3
    _edit_state(run_dir, lambda s: s["T01"]["transcript"][1].update(handoff_offer=True))
    seen = []
    monkeypatch.setattr(judge, "HAVE_KEY", True)
    monkeypatch.setattr(judge, "evaluate_turn", _fake_eval(seen))
    rc.cmd_judge(_args())
    assert seen[0]["meta"]["handoff_offer"] is True
