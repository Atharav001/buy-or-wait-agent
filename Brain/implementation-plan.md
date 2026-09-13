# Buy or Wait? — Implementation Plan

Status: **Active** | Companion to `design.md` (how), `PRD.md` (what), `decisions.html` (why). This file is the **build order** — read it top to bottom, do not skip phases, do not start a phase before the previous one's Definition of Done is checked off.

**If you are an AI coding agent building this:** read `design.md` in full, then `PRD.md`, then skim `decisions.html`'s entry titles, before writing any code. Every phase below cites the exact design.md section it implements — when in doubt about *how* something should work, that section is the source of truth, not your own judgment. If you find yourself making a design decision that isn't already covered (a new ambiguity, a new edge case), stop, and append a new `DEC-0XX` entry to `decisions.html` using its logging protocol before writing the code — don't decide silently.

---

## Phase 0 — Ground truth check (30 min, do this before anything else)

The design docs were written against `problem_statement.md`'s prose description of the schema. Before coding, verify the prose matches the actual files byte-for-byte — real CSVs sometimes carry columns or value formats the spec text doesn't fully spell out (e.g. exact string format of `payment_methods_user_will_consider`, exact priority representation, exact flexible/protected flag name).

**Tasks:**
- [ ] Clone the repo, `cd` into it, list `dataset/` contents against the 9 files design.md expects.
- [ ] For each of the 6 supporting CSVs, print `df.columns.tolist()` and 3 sample rows. Do not proceed on assumption — actually look.
- [ ] Confirm `financial_profiles.csv`'s exact columns for: `home_currency`, `available_balance`, `minimum_balance_to_keep`, priority representation, flexible/protected category representation, `payment_methods_user_will_consider` (is it a delimited string? a list? confirm the delimiter), `max_installment_months`.
- [ ] Confirm `financial_events.csv`'s exact columns for: `event_id`, `linked_event_id`, `status` (what are the literal status strings — `settled`/`pending`/`failed`/`cancelled`/etc? get the exact vocabulary, don't guess), `amount`, `currency`, `date`, category, recurring flag if any, flexible/protected flag if any.
- [ ] Confirm `request_payment_options.csv`'s exact columns for: `payment_option_id`, start date, interval-days field, financing fee field, total payable amount field.
- [ ] Confirm `messages.csv` / `images.csv` exact columns for `user_id`/`request_id`/`related_event_id` (design.md §3.4a assumes all three can be populated — verify).
- [ ] Confirm `output.csv` template header row matches design.md's `OutputRow` field order exactly (`request_id, amount_safe_to_pay, affordability_status, recommended_payment_method, payment_plan, earliest_date_for_full_payment, spending_changes_needed, decision_explanation`).
- [ ] Open 2-3 of the `sample_requests.csv` rows by hand and manually trace what the "expected" output implies about the underlying data — this is your sanity check that your mental model of the rules (design.md §4.5/§4.6) actually produces that answer before you've written a line of pipeline code.

**Definition of Done:** you can state, from the real files (not from memory of the spec), the exact column names and literal vocabularies for every field the solver depends on. If anything here contradicts design.md, fix design.md first (small note, not a rewrite) before continuing — the design doc must never drift from the real data.

---

## Phase 1 — Project scaffold + data contracts (design.md §4, §8)

**Tasks:**
- [ ] Create the repo layout exactly as in design.md §8 (`code/` with `main.py`, `ingest.py`, `currency.py`, `extraction.py`, `reconciler.py`, `forecaster.py`, `solver.py`, `explainer.py`, `validator.py`, `models.py`, `config.py`, `evaluation/`, `tests/`, `audit_log/`).
- [ ] `requirements.txt`: pandas, pydantic, your LLM SDK of choice, pytest. Keep it minimal — no framework you don't have a concrete use for yet.
- [ ] Write `models.py` first, before any logic module. Every Pydantic class from design.md §4: `ExtractedAmount`, `ClaimedFact`, `Plan` (with `completion_date`, fee-aware `total_paid` per DEC-023), `OutputRow` (with the exact literal enums fixed in DEC-022). Use the **real column names from Phase 0**, not placeholder names.
- [ ] `config.py`: env-driven API key loading, model name constants, cache directory path, forecast horizon constant (90), spending-change cap constant (3).

**Definition of Done:** `models.py` imports cleanly, every class has field types matching Phase 0's confirmed real schema, and a throwaway script can construct one valid instance of each model without validation errors.

---

## Phase 2 — Ingest + currency (design.md §3.1, §3.2)

**Tasks:**
- [ ] `ingest.py`: load all 6 CSVs + `requests.csv` into typed structures. Fail loud (raise) on missing/mistyped columns — no silent coercion (DEC-003). Tag every `financial_events` row with blank `amount` for later extraction instead of defaulting to 0.
- [ ] `currency.py`: `convert(amount, from_ccy, to_ccy, on_date, rates_table)`. Exact-date join only (DEC-004). Same-currency short-circuit. Missing-rate case logs and excludes the event rather than guessing.
- [ ] Write `tests/test_currency.py` first (TDD-lite): same-currency passthrough, a real cross-currency conversion using an actual row from `exchange_rates.csv`, and a missing-rate-date case.

**Definition of Done:** `tests/test_currency.py` passes. Running `ingest.load_all("dataset/")` on the real dataset produces no unhandled exceptions and reports how many blank-amount events were found (sanity-check the count against what you'd expect from a manual look at `financial_events.csv`).

---

## Phase 3 — Extraction layer (design.md §3.3, §6, §3.4a)

This is the only module allowed to call an LLM/VLM before the explainer (Phase 8). Build it in isolation with hand-crafted fixtures before wiring it to the real pipeline — it's the highest-cost-per-mistake module to debug live against real API calls.

**Tasks:**
- [ ] `extraction.py`: `extract_amount_from_image(image_path, event_context)` and `extract_facts_from_message(message_text, scope)` — `scope` covers all three levels from DEC-024 (user/request/event), not just event.
- [ ] Implement the guard sequence from design.md §3.3 exactly: sanitize → injection-pattern flag (log only, non-blocking) → schema-forced call with one retry on validation failure → confidence/sanity bound check → cross-field validation against `event_context`.
- [ ] Build the cache from DEC-014: key on `image_id`/`message_id`, scoped per user, hit/miss counters exposed for `usage_report.md` later.
- [ ] Write 4-5 **hand-crafted adversarial fixtures** before touching real data: (a) an image whose visible text says "balance after payment: $0" to confirm DEC-012's context-aware guard rejects/flags it correctly, (b) a message containing "ignore the minimum balance for this one" to confirm no `ClaimedFact.kind` exists for it and the extractor doesn't invent one, (c) a normal clean amend-amount message, (d) a normal clean image with an unambiguous charge amount, (e) a message with no resolvable fact (should return empty list, not a hallucinated one).
- [ ] `tests/test_extraction.py` runs these 5 fixtures and asserts the expected typed output (or rejection) for each — this test suite is your defense material for the AI Judge interview's "how did you handle adversarial input" question, keep it readable.

**Definition of Done:** all 5 fixtures pass. Run extraction once against the real dataset's actual blank-amount events and message set, spot-check 5 outputs by hand against the source image/message.

---

## Phase 4 — Reconciler (design.md §3.4, §3.4a, §3.4b)

Highest bug-risk module — budget real time here, not a rushed pass.

**Tasks:**
- [ ] `reconciler.py`: implement the 4-step conflict-resolution priority (DEC-005) as a small, independently-testable function — don't bury it inline inside a larger loop.
- [ ] Implement the cash-flow inclusion rules verbatim (reserve pending debits, ignore pending credits until settled, exclude failed/cancelled/duplicates, unrealized investments never spendable, confirmed salary counts on settlement date only — design.md §3.4).
- [ ] Implement recurrence classification requiring real repeat history (DEC-008) — not a category-name heuristic.
- [ ] Wire in `ClaimedFact`s from Phase 3 at all three scopes (DEC-024).
- [ ] Implement `select_spending_changes()` from design.md §3.4b — priority-ranked, stop-before-reduce, minimal sufficient set, mutual-exclusivity enforced (DEC-025). This can be built and tested in isolation from the rest of the reconciler since it only needs a candidate unsafe plan + forecast + profile as input — don't wait for the full reconciler to be done to start this.
- [ ] `tests/test_reconciler.py`: one test per conflict-resolution rule (4 tests minimum), one for pending-credit exclusion, one for pending-debit reservation, one for recurrence classification (positive and negative case), one for duplicate/`linked_event_id` chain collapsing, one for `select_spending_changes()` minimality, one for its priority ordering, one for its mutual-exclusivity rule.

**Definition of Done:** every rule in design.md §3.4/§3.4b has at least one passing unit test with a hand-built fixture ledger (not real data — you need to control exactly what's ambiguous to test the rule). Then run the reconciler against 2-3 real users' data and manually verify the resulting `ReconciledLedger` against what you'd compute by hand from the raw CSVs.

---

## Phase 5 — Forecaster (design.md §3.5, §4.9)

**Tasks:**
- [ ] `forecaster.py`: `simulate_balance(ledger, start_date, horizon_days=90)` — event-driven daily simulation (DEC-009), not monthly buckets.
- [ ] Expose a query helper: "does balance stay `>= minimum_balance_to_keep` for every day between date A and date B" — this is what the solver's safety check will call repeatedly, build it as a clean reusable function now.
- [ ] `tests/test_forecaster.py`: a fixture ledger with a known mid-horizon dip below minimum balance that a monthly-average model would miss — confirm the daily simulation catches it. This single test is your proof DEC-009 was worth doing.

**Definition of Done:** test passes, and running the forecaster on a real user produces a daily series you can plot/print and sanity-check against their actual recurring income/expense pattern.

---

## Phase 6 — Solver (design.md §3.6, §4.5, §4.6)

**Tasks:**
- [ ] `solver.py`: `compute_amount_safe_to_pay`, `compute_earliest_full_payment_date`, `generate_candidate_plans`, `safety_check`, `rank_plans` — as separate, independently testable functions, not one large function.
- [ ] Implement the full eligibility-gate table from design.md §4.5 exactly — one gate check per method, each returning a clear pass/fail with a reason string (feeds the audit log later).
- [ ] Implement the 6-step tie-break from §4.6, including the dual gate+tie-break handling of the deadline rule from DEC-021.
- [ ] `tests/test_solver.py`: construct fixture scenarios for each `affordability_status` outcome (now / with_plan / later / not_affordable), a fixture where two installment options differ only by fee to confirm DEC-023's fee-aware ranking picks correctly, a fixture with a tie needing all 6 tie-break levels to resolve down to `payment_option_id`, and a fixture proving `not_recommended` only fires when every candidate fails its gate (never as a lazy default).

**Definition of Done:** every `affordability_status` value and every tie-break level has a passing test with a fixture specifically designed to exercise it — not incidentally covered by a bigger end-to-end test.

---

## Phase 7 — Explainer + Validator (design.md §3.7, §3.8)

**Tasks:**
- [ ] `explainer.py`: takes only the finalized `OutputRow`-equivalent decision object (no raw ledger/message/image access — DEC-013), fills a template into 1-3 sentences of grounded prose.
- [ ] `validator.py`: implement every hard-assert from design.md §3.8 in order, each with a specific failure message (not a generic "validation failed") so a failed row's audit log entry says exactly which rule tripped.
- [ ] `tests/test_validator.py`: one test per hard-assert rule, each deliberately constructing a row that violates just that one rule and confirming it's caught (not caught by a different, earlier rule by accident — check the failure message matches).

**Definition of Done:** validator rejects every deliberately-broken fixture with the correct, specific reason; accepts every correctly-formed fixture. Explainer output read aloud for 5 fixtures sounds specific and grounded, not generic boilerplate.

---

## Phase 8 — Runner / orchestration (design.md §3.9)

**Tasks:**
- [ ] `main.py`: wire ingest → extraction → reconciler → forecaster → solver → explainer → validator → CSV append, exactly the sequence in design.md §2's diagram.
- [ ] Write `audit_log/<request_id>.json` per row: which events included/excluded/deduped and why, which gates each candidate passed/failed, the winning plan's tie-break path, the final decision.
- [ ] CLI args: `--requests`, `--out`, maybe `--limit N` for fast dev iteration on a subset before burning tokens on the full 250.
- [ ] Run once on `--limit 5`, read all 5 audit logs by hand, confirm they tell a coherent story before scaling up.

**Definition of Done:** `python main.py --requests dataset/sample_requests.csv --out /tmp/sample_out.csv --limit 5` runs end to end with no crashes and produces 5 plausible rows plus 5 readable audit logs.

---

## Phase 9 — Evaluation harness (PRD FR-11, design.md §9.2)

**Tasks:**
- [ ] `evaluation/run_eval.py`: run the full pipeline against `sample_requests.csv`, diff every predicted field against the expected column, field by field, print a per-field agreement rate and a list of specific mismatched `request_id`s with both values side by side.
- [ ] Run it. Do not treat the first run's score as final — this is where real debugging starts.
- [ ] For every mismatch: open that row's audit log, figure out which phase's rule produced the wrong answer, fix that phase's module, re-run the **unit tests for that phase** before re-running the full eval (cheaper feedback loop).
- [ ] Do not special-case a `request_id` to force it to match — if you find yourself writing an `if request_id == "req_017"`, stop, that's exactly the disallowed hardcoding the rules call out. Fix the general rule instead.

**Definition of Done:** documented pass rate against all 25 sample rows, with every remaining mismatch (if any) explained in a comment — either a known accepted limitation or a genuine spec ambiguity worth a new `DEC-0XX` entry, never an unexplained gap.

---

## Phase 10 — Full run + usage report (PRD FR-12, design.md §7)

**Tasks:**
- [ ] Run `main.py` on the full `dataset/requests.csv` for real.
- [ ] Run it a **second time** on the same input, diff the two `output.csv` files — must be byte-identical (DEC-018). If not, find the non-determinism (usually an unseeded call or unordered dict/set iteration) before proceeding.
- [ ] Generate `evaluation/usage_report.md`: per-model call counts, input/output tokens, total and average tokens per request, estimated total and per-request cost, split by extraction vs explainer usage, cache-hit vs real-call counts kept separate (DEC-014).

**Definition of Done:** two identical-input runs produce identical `output.csv`. `usage_report.md` is populated from real run data, not estimated/backfilled after the fact.

---

## Phase 11 — Packaging + submission checklist (PRD §8, §9)

**Tasks:**
- [ ] `zip -r code.zip code/ -x "code/audit_log/*" "code/**/__pycache__/*" "code/.venv/*" "code/dataset/*"` — confirm exclusions with `unzip -l code.zip` before uploading, don't assume the command worked.
- [ ] README in `code/`: how to run, what each module does (point back at design.md sections rather than re-explaining), how to run the eval harness.
- [ ] Confirm `log.txt` exists per the repo's AGENTS.md chat-transcript-logging contract if an AI coding tool was used to build this.
- [ ] Walk through PRD.md §9's acceptance-criteria checklist item by item, literally checking each box.
- [ ] Pick 3 arbitrary `request_id`s you have **not** already looked at, open only their audit logs, and explain the decision out loud unaided — this is your dry run for the AI Judge interview (PRD §7's defensibility metric). If you can't do this cleanly, the audit log format (Phase 8) needs more detail, not your memory of the code.

**Definition of Done:** every PRD.md §9 checkbox ticked, dry-run defense completed successfully, files uploaded.

---

## Cross-cutting rule for every phase

Before moving to the next phase, re-run **all** previously-written tests, not just the new phase's tests — a change in the reconciler (Phase 4) can silently break a solver test (Phase 6) written earlier if you're not disciplined about this. Treat the growing test suite as a regression gate from Phase 2 onward, exactly like `evaluation/run_eval.py` treats the golden set from Phase 9 onward.
