# Buy or Wait? — Product Requirements Document

Status: **Active** | Companion to `design.md` and `decisions.html` — this file defines *what* must be true; design.md defines *how*.

---

## 1. Problem statement

Build an AI-powered financial agent that decides, for a given user and a given requested expense, whether and how they can safely pay for it — using their reconstructed financial state (balance, recurring/pending obligations, essential spending, confirmed income, payment preferences) plus any relevant evidence found in messages or images. Output must be a structured, machine-scoreable CSV row per request.

## 2. Goals

- G1 — Correctly reconstruct each user's true financial position from deliberately messy, multi-source, sometimes-conflicting data.
- G2 — Produce a personalized recommendation: two users with identical balances can get different answers based on their own commitments, priorities, and preferences.
- G3 — Never recommend a plan that would breach the user's `minimum_balance_to_keep` or essential spending at any point across the 90-day forecast.
- G4 — Match the exact output schema so the row is machine-gradable.
- G5 — Be explainable — every recommendation ships with a short, accurate, non-generic justification.
- G6 — Be deterministic and defensible under adversarial follow-up questioning (the AI Judge interview).

## 3. Non-goals

- Not a general personal-finance chatbot or investment advisor.
- Not required to handle live data feeds, bank integrations, or real-time rates.
- Not optimizing for scale/concurrency beyond the given 250-row batch.
- Not required to produce a UI — CSV output only, though an internal audit view is encouraged for debugging/judge defense.

## 4. Users / stakeholders

| Stakeholder | What they need from this system |
|---|---|
| Hidden test harness | Exact-schema `output.csv`, correct on both disclosed (sample) and hidden rows |
| AI Judge (interview) | A defensible, explainable, non-hardcoded approach the builder can walk through live |
| The 25 sample-labeled users | Correctness signal — the only ground truth available pre-submission |
| Future maintainer (or "lower-intelligence agent") reading this repo | Enough specificity in design.md + this PRD to extend/debug without guessing intent |

## 5. Functional requirements

Each FR maps to one output field. IDs are stable — reference them in code comments and commit messages.

**FR-1 (`amount_safe_to_pay`).** System must compute the maximum amount payable today without ever breaching `minimum_balance_to_keep` or essential spending across the 90-day forecast, capped at `requested_amount`. Must be `0` when even a partial safe payment isn't possible today.

**FR-2 (`affordability_status`).** Must be exactly one of the four spec-defined literals — `affordable_now`, `affordable_with_plan`, `affordable_later`, `not_affordable` — consistent with FR-1 and FR-4. `affordable_now` ⟺ `earliest_date_for_full_payment == request_date`, enforced in both directions.

**FR-3 (`recommended_payment_method`).** Exactly one of `full_payment`, `partial_payment`, `installments`, `wait`, `not_recommended`. `full_payment`/`partial_payment`/`installments` require membership in `payment_methods_user_will_consider`; `wait` requires the user specifically accepts `full_payment` (not just any method); `not_recommended` is the fallback when no candidate clears its eligibility gates (see design.md §4.5).

**FR-3a (partial-payment gating).** `partial_payment` is only ever proposed when `allows_partial_payment` is true on the request row, `partial_payment` is in the user's accepted methods, `0 < amount_safe_to_pay < requested_amount`, and `earliest_date_for_full_payment <= desired_completion_date`. The plan is always exactly 2 legs summing to `requested_amount`.

**FR-3b (installment fidelity).** Recommended installment legs must exactly match a real `payment_option_id` row's dates and amounts, including any stated financing fee — never a recomputed or rounded schedule.

**FR-4 (`earliest_date_for_full_payment`).** Earliest date, within the 90-day horizon, at which paying the *full* `requested_amount` passes the safety check with zero `spending_changes_needed`. Empty/null if no such date exists within the horizon.

**FR-5 (`payment_plan`).** Dated amounts for whichever plan is recommended, in the exact required string format, chronologically ordered, mathematically consistent with `amount_safe_to_pay`/`requested_amount`.

**FR-6 (`spending_changes_needed`).** At most 3 flexible, non-protected expense adjustments (stop or reduce), only proposed when required to make an otherwise-unsafe plan safe — never proposed decoratively when a plan is already safe without them. Selection order follows the user's stated priorities (lowest-priority flexible expense cut first) per design.md §3.4b — this is the primary mechanism delivering FR-8 personalization, not an afterthought formatting rule. The same `event_id` never appears in both a `stop` and a `reduce_to` entry.

**FR-7 (`decision_explanation`).** Short, specific, grounded exclusively in the already-computed numeric decision (see design.md §3.7) — never generic filler, never referencing data that didn't make it into the final decision.

**FR-8 (personalization).** Two requests with identical `requested_amount` and identical `available_balance` must be able to receive different `amount_safe_to_pay`/`affordability_status` if their recurring obligations, priorities, or payment preferences differ — this must be demonstrable, not incidental.

**FR-9 (evidence use).** When an event's amount is blank, or a message amends/cancels/confirms an event, the system must actually incorporate that evidence (not ignore images/messages as a shortcut) — but always through the extraction → reconciliation pipeline (design.md §3.3–3.4), never by hand-reading files during development and hardcoding results.

**FR-10 (multi-currency).** All monetary comparisons happen in the user's `home_currency`, converted via the exact-date rate in `exchange_rates.csv` — no live rates, no same-currency lookups performed unnecessarily.

**FR-9a (evidence scope).** Messages and images attach at three distinct levels — `user_id`, `request_id`, or `related_event_id` — confirmed by the spec's own join rules. All three must be retrieved and considered, not only event-linked evidence; missing request- or user-scoped evidence is a silent accuracy gap (design.md §3.4a).

**FR-11 (evaluation workflow).** Repository must include a runnable evaluation script that scores predictions against `sample_requests.csv` and reports field-level agreement, runnable independently of the main pipeline.

**FR-12 (usage report).** Repository must include `usage_report.md` logging every model call's purpose, token usage, and approximate cost.

## 6. Data requirements

All six input files (`financial_profiles.csv`, `financial_events.csv`, `exchange_rates.csv`, `request_payment_options.csv`, `messages.csv`, `images.csv` + `media/images/`) must be read directly from `dataset/` — no manual pre-processing, no hand-curated overrides, no file-specific special-casing (explicitly disallowed by the challenge rules).

## 7. Success metrics

| Metric | Target | How measured |
|---|---|---|
| Field-level accuracy on `sample_requests.csv` | As close to 100% as achievable before submission | `evaluation/run_eval.py` diff |
| Output schema compliance | 100% of 250 rows pass `validator/` with zero hard-fails | validator pass rate |
| Determinism | Two runs on identical input produce byte-identical `output.csv` | design.md §8 idempotency check |
| Safety violations | 0 recommended plans that breach `minimum_balance_to_keep` in the forecast window | adversarial + golden-set tests |
| Judge-interview defensibility | Builder can explain any single row's reasoning from `audit_log/<request_id>.json` alone, live, without re-deriving it | manual dry-run before interview |

## 8. Constraints

- 24-hour build window (contest opened ~Sept 12 evening IST, closes Sept 13 18:00 IST per AGENTS.md).
- Submission = code.zip + predictions CSV + `log.txt` transcript.
- AI Judge interview: 30 minutes, camera mandatory, opens after successful submission, window closes 12 hours after submission or by Sept 14 06:00 IST.
- No hardcoded test labels or file-specific answers — grounds for disqualification, not just a quality issue.
- code.zip must exclude venvs, `node_modules`, build artifacts, `data/`, `dataset/`.

## 9. Acceptance criteria (definition of done)

- [ ] All FR-1 through FR-12 implemented and covered by at least one test.
- [ ] `evaluation/run_eval.py` run on `sample_requests.csv` with a documented pass rate before final submission.
- [ ] Determinism check passes (two identical runs, byte-identical output).
- [ ] `output.csv` generated for the full `requests.csv`, header/column order verified against `problem_statement.md` byte-for-byte.
- [ ] `usage_report.md` present and populated, not a stub.
- [ ] `code.zip` built with exclusions verified (`unzip -l code.zip` manually checked before upload).
- [ ] Builder has done one full dry-run explaining 3 arbitrary rows from `audit_log/` out loud, unaided, before the AI Judge interview.

## 10. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Vision extraction hallucinates amount (e.g. reads "balance after payment = $0" as the charge) | design.md §3.3 event-context-aware extraction + sanity bounds |
| Prompt injection via message/image content altering the decision | design.md §6 structural quarantine — no rule-altering fact types exist in the schema |
| Reconciliation double-counts or drops linked/duplicate events | Dedicated reconciler unit tests per conflict-resolution rule (design.md §3.4) |
| Running out of time and shipping a monolithic, unexplainable script | Module boundaries in design.md §3 are mandatory, not optional — each is independently testable |
| Judge asks "why did the model decide X" and builder can't answer | `audit_log/<request_id>.json` + FR-7's grounding requirement makes every row traceable |
| Spec states "must complete by `desired_completion_date`" both as part of the definition of "safe" and again as ranking tie-break #1 — genuinely ambiguous whether it's a hard filter, a soft preference, or both | Implemented as both: hard eligibility gate at candidate-generation time AND the literal tie-break rule still evaluated during ranking (design.md §4.5, logged as DEC-021) — redundant-but-spec-faithful beats a clever shortcut that might diverge from a hidden test |
| Installment `total_paid` computed as a naive sum instead of the option's real fee-inclusive total, silently breaking tie-break #3 | `Plan.total_paid` sourced directly from `request_payment_options.csv`'s stated total payable amount for installment candidates (design.md §4.6) |
| Request- or user-scoped messages/images ignored because extraction only checks `related_event_id` | Three-scope evidence handling is an explicit module requirement, not incidental (FR-9a, design.md §3.4a) |

## 11. Milestones (24h budget — see decisions.html DEC-020 for rationale)

| Window | Deliverable |
|---|---|
| Hr 0–2 | Ingest + currency module, schema for all six inputs confirmed |
| Hr 2–5 | Extraction layer (image + message), cached, guarded |
| Hr 5–9 | Reconciler + forecaster, unit-tested |
| Hr 9–14 | Solver (candidate generation, safety check, ranking) |
| Hr 14–17 | Explainer + validator |
| Hr 17–19 | Run against `sample_requests.csv`, fix systematic errors |
| Hr 19–21 | Full run on `requests.csv`, `usage_report.md` finalized |
| Hr 21–23 | Packaging, README, exclusions check |
| Hr 23–24 | Buffer, dry-run judge defense |
