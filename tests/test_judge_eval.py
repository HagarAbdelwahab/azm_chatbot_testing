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


def _label_sheet(run_dir, rows: str):
    sheet = run_dir / "turns_to_label.csv"
    sheet.write_text("key,run_id,verdict\n" + rows, encoding="utf-8")
    return sheet


def test_agreement_is_per_turn_with_confusion_matrix(run_dir, capsys):      # F9
    (run_dir / "judgments.json").write_text(json.dumps({
        "T01/1": {"verdict": "pass", "run_id": "r1", "reason": "looked fine"},
        "T01/2": {"verdict": "needs_review", "run_id": "r2", "reason": "unsure"}}),
        encoding="utf-8")
    rc.judge_agreement(_label_sheet(run_dir, "T01/1,r1,fail\nT01/2,r2,fail\n"))
    out = capsys.readouterr().out
    # needs_review is its own column, not a fail, and not in the agreement %
    assert "agreement 0/1 = 0%" in out
    assert "missed fails (human fail, judge pass): 1" in out
    assert "T01/1" in out and "looked fine" in out


def test_agreement_skips_labels_on_a_reasked_answer(run_dir, capsys):
    (run_dir / "judgments.json").write_text(json.dumps({
        "T01/1": {"verdict": "pass", "run_id": "r1", "reason": ""},
        "T01/2": {"verdict": "pass", "run_id": "r2", "reason": ""}}), encoding="utf-8")
    rc.judge_agreement(_label_sheet(run_dir, "T01/1,r1,pass\nT01/2,OLD,fail\n"))
    out = capsys.readouterr().out
    assert "agreement 1/1 = 100%" in out and "skipped 1 label(s)" in out


def test_label_sheet_has_no_judge_verdict_and_keeps_labels(run_dir, monkeypatch):
    monkeypatch.setattr(rc, "LABELS", run_dir / "turns_to_label.csv")
    state = rc.load_state()
    path = rc.write_label_sheet(state)
    import csv
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    assert [r["key"] for r in rows] == ["T01/1", "T01/2"]
    assert not any(c.startswith("judge") for c in rows[0])
    assert rows[0]["approved_answer"] == "approved" and rows[1]["approved_answer"] == ""

    rows[0]["verdict"], rows[1]["verdict"] = "pass", "fail"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rc.LABEL_COLS)
        w.writeheader(); w.writerows(rows)
    state["T01"]["transcript"][3]["run_id"] = "r2-new"      # turn 2 re-asked
    rows = list(csv.DictReader(rc.write_label_sheet(state).open(encoding="utf-8-sig")))
    assert [r["verdict"] for r in rows] == ["pass", ""]      # stale label dropped


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
