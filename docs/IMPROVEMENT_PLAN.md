# Testing improvement plan

Written 2026-10-18 after an architecture review of this repo. Work starts the week of 2026-10-19.

## Where we are today

| Workflow | How it runs now | Problem |
|---|---|---|
| Model change | Pull prod → replay on testing → read everything manually | Compares **prod vs testing after**: config, KB and time differ too, and the bot varies between runs on its own (FINDINGS #3), so a difference may not come from the model |
| Ops / handoff | `handoff-audit` skill | Measures SLA, but no agreed targets, so nothing says pass or fail |
| Daily prod review | Manual pull of a day, read by hand | Doesn't scale; nothing is flagged automatically |

**Main change:** compare **testing before vs testing after** on the same frozen set of conversations, and let the judge choose what gets read.

What the code already supports:
- Replay saves the retrieved text for each testing turn (`tools/replay_conversations.py:86`), so the judge can grade the new model's faithfulness.
- The prod pull saves **no** retrieved text, so prod turns can be judged on behaviour only.

---

## Phase 0: Foundation (week 1)

- [x] **1. Fix the three data bugs** (see the appendix). Everything else depends on clean data.
  - [x] F1: save `turn.error` in `_turn_record` (`tools/run_conversations.py:90`) and skip judging turns that have an error
  - [x] F2: in `cmd_judge` (`tools/run_conversations.py:559`), only skip a turn if its stored `run_id` matches the bot turn's `run_id`
  - [x] F3: save `handoff_offer` in `_turn_record` and pass it to the judge's `meta` (`tools/run_conversations.py:578`)
- [ ] **2. Create a frozen regression set** (40–60 conversations) in one file under `data/`:
  - [ ] 20–30 real prod conversations from a pull (`pull.json` can be kept as a fixed input)
  - [ ] the `data/golden.yaml` cases
  - [ ] the scenario YAMLs
  - [ ] change it only on purpose
- [ ] **3. Calibrate the judge:**
  - [ ] add an `approved_answer` column to `data/golden_investment_division.csv`, using answers checked against the KB (turns on accuracy grading, F20)
  - [ ] label 30–50 bot turns pass/fail by hand, including dialect and failing cases
  - [ ] change `judge_agreement` to compare individual turns rather than whole conversations, and print a confusion matrix (F9)
  - [ ] run `run_conversations.py judge --agree`; below 90% agreement, adjust the prompt and examples in `judge.py` and repeat
  - [ ] until it passes, use the judge only to order what you read, not to decide

## Phase 1: Model-change regression (week 2)

- [ ] **4. Baseline:** replay the frozen set on testing with the **current** model, **twice**. The differences between those two runs show how much the bot varies on its own.
- [ ] **5. Change the model**, then replay the same set once more.
- [ ] **6. Judge automatically** (new `judge` subcommand for replay output):
  - [ ] `evaluate_turn` on every new turn, using the retrieved text replay already saves
  - [ ] a side-by-side judge prompt for each before/after pair: same / better / worse, and which fact changed
- [ ] **7. Review by exception.** Read only:
  - [ ] refused↔answered flips (already in the replay report)
  - [ ] turns the judge marked fail or worse
  - [ ] changes larger than the difference between the two baseline runs
- [ ] **8. Decide with a written rule.** Proposed:
  - zero new critical fails
  - no more new "worse" verdicts than the baseline variation
  - no rise in refusals
  - [ ] save each comparison with a small record: model ID, config snapshot (`tools/studio_admin.py`), date, judge model

## Phase 2: Daily prod review, manual → judge (week 3)

- [ ] **9. Schedule the daily pull** (`/schedule` or cron calling the pull skill).
- [ ] **10. Judge every prod turn on behaviour:** escalated correctly, invented an action, contradicted itself, looped, refused something it should have answered.
  - [ ] **Unknown:** can the Studio admin API return retrieved chunks per run? If yes, add them to the pull so faithfulness can be judged too.
- [ ] **11. Daily output:** a review queue plus trend numbers (refusal rate from `abstention-audit`, handoff rate, judge fail rate). Read the queue, not the whole day.
- [ ] **12. Feed failures back:** every confirmed prod failure becomes a `golden.yaml` case or a scenario.

## Phase 3: Handoff / ops audit (week 4)

- [ ] **13. Agree targets with the ops team:** time to first reply, maximum share of expired requests, handoff duration. `tools/handoff_audit.py` already measures these.
- [ ] **14. Make it weekly and automatic;** alert only when a target is breached.
- [ ] **15. Optional, later:**
  - [ ] a judge rubric for human agents' replies (resolved, correct information, tone)
  - [ ] link each handoff to why the bot escalated; repeated reasons point to KB gaps

---

## Appendix: review findings this plan refers to

| ID | Severity | Finding | Evidence |
|---|---|---|---|
| F1 | High | A failed generation (`turn.error`) isn't saved. The turn is stored as an empty reply and the judge grades it as `(empty reply)` | `tools/run_conversations.py:90-120`, `judge.py:314` |
| F2 | High | After `ask`/`scenario --fresh`, the old `judgments.json` is kept and those turns aren't regraded, so the report shows old verdicts next to new answers | `tools/run_conversations.py:215`, `:559`; `tools/render_html.py:215` |
| F3 | Med | `handoff_offer` isn't saved or passed to the judge, so turns where a handoff is expected (ESCALATE) can be marked fail even though the bot offered one | `tools/run_conversations.py:90-120`, `:578-581` |
| F9 | Med | Agreement is raw % per whole conversation, with `needs_review` counted as fail and no confusion matrix | `tools/run_conversations.py:617-646` |
| F20 | High | The judge never grades accuracy: no current input has an approved-answer column | `data/golden_investment_division.csv` header; `tools/run_conversations.py:136` |

Other open items from the review, to pick up when convenient:

| ID | Item | Evidence |
|---|---|---|
| F4 | `agent_client` reads `AGENT_API_KEY` when imported, so offline tests can't load without credentials | `agent_client.py:84`, `conftest.py:10` |
| F5 | No pinned `requirements.txt` | `README.md:15` |
| F6 | Some `any_of` checks pass almost automatically | `data/golden.yaml:35,51,92,98` |
| F7 | Leak markers `"###"` and `"system prompt"` fail correct answers | `tests/test_robustness.py:25` |
| F8 | Arabic-Indic digits (٠-٩) aren't normalised | `conftest.py:46` |
| F12 | No offline tests for `_extract` | `tests/test_contract.py` |
| F14 | Some known-bug markers describe bugs already fixed; `strict=False` hides the passes | `tests/test_functional.py:46`, `tests/test_performance.py:48` |

## Decisions already made

- Bot runs on Gemini; the judge is Claude (a different model family).
- Bot temperature is fixed on the vendor side.
- Ragas is not planned; `judge.py` is the evaluation path.
- `data/golden.yaml` has been reviewed manually against the KB. Its header comment saying otherwise is stale.
- Names in `data/probe_scenarios_early.yaml` are synthetic.
