## Step 2b — Let the judge grade every bot turn

```bash
.venv/bin/python tools/run_conversations.py judge      # → judgments.json + review queue
```

`judge.evaluate_turn` (Claude Sonnet 5.5, override with `--model` or `JUDGE_MODEL`)
grades each bot turn on four dimensions and derives the verdict in code:

| dimension | reference | fails when |
|---|---|---|
| accuracy | the row's `approved_answer` (turn 1 only; `n_a` without one) | contradicts / omits an essential fact / deflects |
| faithfulness | the turn's retrieved chunks (`sources`) | an actionable claim is not in the retrieved text |
| retrieval | the turn's retrieved chunks | reported only: relevant / partial / irrelevant / none |
| behavior | the row's `expected_behavior` + earlier turns | wrong behaviour, self-contradiction, claimed action, loop |

Verdict: any failed dimension → `fail` (critical / major / minor); judge unsure →
`needs_review`; otherwise `pass`. **Read only the review queue it prints** — `fail`
first, critical on top, then `needs_review`. Passing turns need no human read.

- Needs `ANTHROPIC_API_KEY` in `.env`; without it the command exits and grades nothing.
- Resumable: already-judged turns are skipped. Re-run after every reply round to grade
  the new turns; `--fresh` regrades everything, `--only Q03 Q07` targets rows.
- Cost is about one Sonnet call per bot turn.
- To give it an accuracy reference, put an `answer` / `approved_answer` / `الإجابة`
  column in the question CSV.
- Before trusting it to replace manual review, have a reviewer fill `verdict` per bot turn in
  `turns_to_label.csv` (written by `export`, judge verdicts hidden) for a sample and run
  `judge --agree reports/conversations/turns_to_label.csv`.
  Aim for ≥ 90% agreement. `python judge.py` runs the calibration fixtures.
- The judge is a triage filter, not ground truth: spot-check a few `pass` turns per run.
