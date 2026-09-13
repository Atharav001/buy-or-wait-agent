# Buy or Wait? — Implementation & Improvement Plan (Post-Audit)

Status: **Active** | Supersedes nothing — extends `implementation-plan.md`'s Phase 11 with the concrete work the external audit surfaced. Companion decision entries: DEC-036 → DEC-040 in `decisions.html`.

**Read this first if you're picking this up cold:** the pipeline is architecturally sound (deterministic, tested, honest about zero model calls) but its core numeric output, `amount_safe_to_pay`, is wrong on 22/25 golden rows. Everything in Phase A exists to fix that one function. Do not let repo-hygiene or formatting work (Phases C/D) happen before Phase A — they're real but low-stakes; Phase A is the only thing that changes your actual score.

> **PHASE A OUTCOME (2026-09-13 — executed, hypothesis falsified):** the DEC-037 "short-cadence projection method" hypothesis was tested to exhaustion and is **wrong**. Evidence gathered before shipping:
> - 4-row grid and full-25 9-strategy sweep over {last, mean, median, trimmed, last3, none} × {detected, mean} projection seams → `amount_safe_to_pay` stays 3/25 under every strategy ("none" gives 4/25 but wrecks `affordability_status` 18 and `earliest` 11).
> - Per-category single-method search (7 cats × 5 methods × 25 rows) → only `request_12` matches gold (drop dining/groceries projection), no other row.
> - Per-category pair search on the 12 worst rows → zero hits. Selective no-projection policies (cuttable/non-protected categories) → 3-4/25.
> - Fixed-horizon (=90d) vs max(90, desired) test → identical results.
> - **Salary double-booking bug found and fixed** in `_book_salary_streams()` (identity (`members is base_evs`) check failed against new list objects → base salary re-added as a secondary stream whenever a scheduled salary row existed; users 21/13/25). Fixed with event-id set comparison. 45 tests pass; no gold-score change.
> - Gold amounts for many rows are clean round numbers while ours carry cents — gold's engine differs structurally (recurrence set/cadence/inclusion), not by amount-method. Chasing exact per-row parity was time-boxed and stopped.
>
> **Shipped as Phase A:** the salary fix + DEC-039 explanation-template parity in `decide()` (`human_date`/`money_comma`/`_change_lead`/`cur_text` in `code/main.py`). Final numbers: `amount_safe_to_pay` 3/25 · `affordability_status` 20/25 · `recommended_payment_method` 21/25 · `payment_plan` 20/25 · `earliest_date_for_full_payment` 19/25 · `spending_changes_needed` 22/25 · `decision_explanation` 13/25 (was 0/25). Gold-check tool moved to `code/evaluation/run_eval.py` (was `tools/gold_check.py`).

---

## Phase A — Root-cause and fix `amount_safe_to_pay` (highest priority, do this first)

**Baseline to beat** (DEC-036): `amount_safe_to_pay` 3/25 exact · `affordability_status` 20/25 · `recommended_payment_method` 21/25 · `payment_plan` 20/25 · `earliest_date_for_full_payment` 19/25 · `spending_changes_needed` 22/25.

**Working hypothesis** (DEC-037): the daily/weekly-cadence discretionary categories — groceries, dining, transport — are projected forward by `reconciler.py` with a frequency or amount that doesn't match whatever method produced the gold answers. Evidence: `request_25`'s simulated trough misses the floor by only ≈29,133 IDR (≈US$1.90) out of a ~24.8M IDR balance — a razor-thin margin, and the pattern across mismatched rows is consistently *close-but-not-exact* in both directions, not wildly random. That signature points at one shared projection mechanism, not 22 unrelated bugs.

**Tasks:**
- [ ] In `reconciler.py`, find the function that projects discretionary/daily-cadence categories forward past the last historical occurrence (search for whatever builds flow entries between `request_date` and `horizon_end` for categories like `groceries`/`dining`/`transport` — these are the ones showing up as small negative flows on non-fixed dates in the forecast, as opposed to `rent`/`insurance`/`utilities`, which land on fixed monthly dates).
- [ ] Print, for `user_25`, exactly which projected instances of these categories land between `request_date` (2024-03-06) and the next salary settlement (2024-03-15), with their projected amount and the historical amounts they were derived from.
- [ ] Compute what 3-4 alternative projection methods would produce for the same window: (a) most-recent-observed amount repeated at the most-recent-observed interval, (b) trailing mean amount at trailing mean interval, (c) trailing median amount, (d) no projection at all past the last *settled* occurrence (i.e. only count what's already in the ledger, never invent future discretionary spend). Compare each against gold's implied trough (≈24,804,100 minimum, i.e. 1,425,000 above the floor) for `request_25`.
- [ ] Repeat the same comparison for 2-3 more of the "close but not exact" rows (`request_07`, `request_17`, `request_22` are good candidates — all installment cases where the gap is a few hundred to a few thousand units, not orders of magnitude) to confirm whichever method wins for `request_25` also wins generally, not just by coincidence on one row.
- [ ] Implement the winning method in `reconciler.py`, replacing whatever's there now. Keep the change isolated to the discretionary-category projection function — do not touch fixed-category (rent/insurance/utilities) handling, salary continuation (DEC-029), or FX conversion (DEC-034), none of which are implicated by this hypothesis.
- [ ] If none of the four candidate methods closes the gap on all four test rows, the hypothesis is wrong or incomplete — fall back to full manual tracing of a second scenario (pick the largest remaining absolute-value miss) before touching more code speculatively.

**Verification:**
- [ ] Re-run `python3 tools/gold_check.py ../dataset` after the fix. Target: `amount_safe_to_pay` materially above 3/25 — treat anything below 15/25 as "hypothesis partially right, keep digging," not as done.
- [ ] Confirm the fix doesn't regress the fields already passing (20/21/20/19/22) — a projection-density change could plausibly shift `earliest_date_for_full_payment` or `payment_plan` too, since they depend on the same forecast. Re-check all seven fields, not just the one you targeted.
- [ ] Re-run `pytest` — if `test_reconciler.py` had a fixture asserting the old (wrong) projection behavior, that test needs updating, not silencing. If no test covered this function's projection amounts at all, that's itself a gap — add one now that pins down the corrected method with a hand-built fixture, so a future change can't silently revert it.

**Definition of Done:** documented before/after `amount_safe_to_pay` agreement rate, root cause named in a code comment at the fixed function (not just in this doc), all 45+ tests still passing, other six fields not regressed.

---

## Phase B — Full re-verification of every mismatched row (not just the ones behind the hypothesis)

Phase A targets the dominant pattern. Some mismatches are probably unrelated:

- [ ] `request_04`/`request_08`/`request_13`/`request_18`/`request_23` show `wait` vs `pay-now` (or vice versa) status/method inversions. After Phase A's fix, re-check these specifically — some may resolve automatically (if driven by the same trough-margin bug), others may need direct tracing the same way `request_25` was traced in Phase A.
- [ ] `request_06`/`request_11`/`request_21` show `spending_changes_needed` disagreeing with gold (gold proposes a stop/reduce, current code proposes `none`, or vice versa). Trace whether `select_spending_changes()` (design.md §3.4b, DEC-025/DEC-028) is even being invoked for these — if the code's own `amount_safe_to_pay`/plan-safety numbers differ from gold's due to Phase A's bug, the spending-changes step may simply never trigger because the plan looked safe without it. Re-check after Phase A before assuming this needs a separate fix.
- [ ] For any row still wrong after Phase A with no shared explanation, write up the specific mechanism (new finding, new decision entry — see Phase F) rather than patching silently.

**Definition of Done:** every one of the 25 sample rows has a one-line note (in a scratch file, doesn't need to ship) stating either "fixed by Phase A" or "distinct cause: ___, tracked separately."

---

## Phase C — Reinstate the hard-gate validator (DEC-038)

**Tasks:**
- [ ] Write `validate_row(row: dict, request: PurchaseRequest) -> tuple[bool, str | None]` in `main.py` (or a new `validator.py` if you want the module split design.md originally specified — correctness matters more than the file boundary at this point).
- [ ] Implement every check from design.md §3.8: bounds (`0 <= amount_safe_to_pay <= requested_amount`), `payment_plan` format and chronological order, `partial_payment` exactly-2-legs-summing-to-requested, installment legs matching a real `payment_option_id`, `affordability_status == "affordable_now"` ⟺ `earliest_date_for_full_payment == request_date` (both directions), `spending_changes_needed` ≤3 entries / correct format / no event in both stop and reduce.
- [ ] Wire it into `run()`: call `validate_row()` immediately before `writer.writerow(...)`; on failure, overwrite the row with a safe `not_affordable`/`not_recommended`/`none` fallback, log the specific rule that tripped (to stdout or a small `audit_log/` file), and count failures for the run summary printed at the end.
- [ ] Add `tests/test_validator.py`: one deliberately-broken fixture per rule (a row with `amount_safe_to_pay > requested_amount`, a malformed `payment_plan` string, an installment not matching any option, etc.) confirming each is caught with the right reason — not silently passed, and not caught by the wrong rule.

**Verification:**
- [ ] Run the full pipeline; confirm zero validator trips on the real 250-row dataset (if there are trips, that's a second bug Phase A/B didn't catch — investigate before assuming the validator itself is wrong).
- [ ] Confirm `pytest` count goes up by however many new validator tests were added, and all still pass.

**Definition of Done:** a deliberately-corrupted row (temporarily hand-edit a solver return value in a debug script) is caught and safely replaced, with a specific reason logged — not a crash, not a silent pass-through.

---

## Phase D — Explanation formatting parity (DEC-039)

**Tasks:**
- [ ] Add comma thousands-grouping to a prose-only money formatter (keep the existing `money()` used for the structured `amount_safe_to_pay` field untouched — that one must stay a plain decimal per the schema).
- [ ] Add `human_date(d: date) -> str` rendering `"15 April 2025"` style, used only inside `decision_explanation` strings.
- [ ] Update every f-string in `decide()` that builds `reason`/explanation text to use the new formatters instead of raw `money()`/`.isoformat()`.
- [ ] Do **not** touch `payment_plan` or `earliest_date_for_full_payment` — those remain `YYYY-MM-DD` and plain numbers exactly as the schema requires; only the free-text explanation field changes.

**Verification:**
- [ ] Re-run `gold_check.py` — `decision_explanation` won't hit 25/25 exact (paraphrasing differences will remain), but every number and date inside it should now visually match gold's convention. Spot-check 5 rows by eye.
- [ ] Confirm `payment_plan`/`earliest_date_for_full_payment` are byte-identical to before this change (formatting change should be explanation-only — diff the full `output.csv` against the pre-Phase-D version restricted to those two columns to prove it).

**Definition of Done:** explanation strings read in gold's house style; structured fields provably unchanged by this phase.

---

## Phase E — Repo hygiene: consolidate the evaluation entrypoint (DEC-040)

**Tasks:**
- [ ] Move `tools/gold_check.py`'s logic to `evaluation/run_eval.py` (a thin wrapper importing the existing logic is fine — don't rewrite working code for the sake of the move).
- [ ] Delete the empty `evaluation/main.py`.
- [ ] Update `design.md`'s repository-layout section to point at the real path, and add a one-line note next to it: "moved from `tools/gold_check.py`, see DEC-040."
- [ ] Grep the repo for any other reference to the old path (`tools/gold_check.py`, `evaluation/main.py`) — READMEs, comments, CI-adjacent scripts — and update them together, not piecemeal.

**Definition of Done:** `find code -size 0` returns nothing; `evaluation/run_eval.py` runs and produces the same output as the old `tools/gold_check.py` did.

---

## Phase F — Full regression, final run, and decision-log discipline going forward

**Tasks:**
- [ ] Re-run the complete test suite (`pytest`) — every phase above should have kept it green throughout; this is the final confirmation.
- [ ] Re-run `evaluation/run_eval.py` (post-Phase-E rename) one more time, record the final seven-field agreement rate.
- [ ] Regenerate `output.csv` at the repo root from the fixed code; run it twice to reconfirm byte-identical determinism (DEC-018) still holds after all these changes.
- [ ] Update `usage_report.md` only if any phase changed the token/cost story — Phases A-E are all pure-code changes, so it should still correctly read 0 calls/0 tokens unless the opt-in image path was separately exercised.
- [ ] Append **one new decision entry** (DEC-041, following the logging protocol at the top of `decisions.html` — never edit DEC-036 through DEC-040 after the fact) recording the final post-fix seven-field agreement rate, explicitly including `amount_safe_to_pay` and `decision_explanation` this time, per DEC-036's own rule that all seven always get reported together.
- [ ] Going forward, treat "did I report all seven fields" as a literal checklist item every time a gold-check number gets written into the decision log — this is the one process fix that prevents DEC-036's gap from recurring.

**Definition of Done:** green test suite, documented final field-agreement rate (all seven fields, no omissions), fresh deterministic `output.csv`, DEC-041 logged, repo tree matches what `design.md` claims it contains.

---

## Priority order if time runs out before all phases complete

1. **Phase A** — everything else is secondary to this. A correct `amount_safe_to_pay` with an ugly file tree beats a tidy file tree with a wrong core number.
2. **Phase C** (validator) — cheap, fast, and directly protects against Phase A's fix having its own bugs reaching the grader unguarded.
3. **Phase B** (remaining mismatch triage) — do as much as time allows; a documented, understood remaining gap is far better than an undocumented one.
4. **Phase D, E, F** — real, worth doing, but genuinely lower stakes than the numeric correctness above them. If the deadline is close, a one-paragraph note in the README acknowledging the dead file and formatting gap is an acceptable substitute for fully executing D/E under time pressure — do not sacrifice Phase A time to chase these.
