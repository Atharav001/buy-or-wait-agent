# Buy or Wait? — System Design & Architecture

Status: **Active** | Scope: HackerRank Orchestrate Sept'26 submission | Non-negotiable reference doc — every module built for this project must trace back to a section here.

---

## 1. Prime directive

This is a **deterministic financial reasoning pipeline with narrow AI-assisted extraction**, not a chatbot. Numbers, safety checks, and ranking are plain code. LLM/VLM calls are confined to two jobs only: (a) reading a blank-amount ledger event off an image, (b) turning a message into a structured fact. Nothing an LLM outputs is allowed to touch the affordability math directly — it always lands as *data*, re-validated by code, before the decision engine sees it.

If a future contributor (human or agent) is tempted to let the LLM "just decide" `amount_safe_to_pay` — **stop**. That is the one rule this whole document exists to protect.

---

## 2. System context

```
                    ┌─────────────────────────────────────────┐
                    │              dataset/ (input)             │
                    │  financial_profiles.csv                   │
                    │  financial_events.csv                     │
                    │  exchange_rates.csv                        │
                    │  request_payment_options.csv               │
                    │  messages.csv                               │
                    │  images.csv + media/images/*.png            │
                    │  requests.csv / sample_requests.csv         │
                    └───────────────────┬─────────────────────┘
                                         │
                                         ▼
                    ┌─────────────────────────────────────────┐
                    │            INGEST & NORMALIZE              │
                    │  load, type-check, currency convert         │
                    └───────────────────┬─────────────────────┘
                                         ▼
                    ┌─────────────────────────────────────────┐
                    │        EXTRACTION LAYER (LLM/VLM)           │
                    │  blank-amount image reader                  │
                    │  message → structured-fact parser           │
                    │  (quarantined — output is data, not code)   │
                    └───────────────────┬─────────────────────┘
                                         ▼
                    ┌─────────────────────────────────────────┐
                    │          LEDGER RECONCILER (code)           │
                    │  dedupe, classify, apply amendments,        │
                    │  conflict-resolution priority                │
                    └───────────────────┬─────────────────────┘
                                         ▼
                    ┌─────────────────────────────────────────┐
                    │        90-DAY FORECASTER (code)             │
                    │  daily balance simulation                    │
                    └───────────────────┬─────────────────────┘
                                         ▼
                    ┌─────────────────────────────────────────┐
                    │       AFFORDABILITY SOLVER (code)           │
                    │  candidate plans → safety check → rank       │
                    └───────────────────┬─────────────────────┘
                                         ▼
                    ┌─────────────────────────────────────────┐
                    │      EXPLANATION GENERATOR (LLM, caged)     │
                    │  fills template from already-final numbers   │
                    └───────────────────┬─────────────────────┘
                                         ▼
                    ┌─────────────────────────────────────────┐
                    │        OUTPUT VALIDATOR (code, gate)        │
                    │  hard-asserts schema/format/math             │
                    └───────────────────┬─────────────────────┘
                                         ▼
                              output.csv + log.txt + usage_report.md
```

Every arrow above is a function call in a single-process pipeline (no queues, no services — see §8 for why).

---

## 3. Module breakdown

### 3.1 `ingest/`
**Responsibility:** load every CSV into typed in-memory tables (pandas DataFrames or a list of dataclasses — pick one and stay consistent). No business logic here. Fail loudly on schema mismatch (missing column, wrong dtype) — never silently coerce.

**Output:** `RawDataset` object holding all six tables plus a `media/` path index.

### 3.2 `currency/`
**Responsibility:** convert any `(amount, currency, date)` to a user's `home_currency`.

```
convert(amount, from_ccy, to_ccy, on_date, rates_table) -> amount_in_to_ccy
```
- Exact-date join only. If `from_ccy == to_ccy`, return `amount` unchanged (skip lookup).
- Missing rate for that exact date → this is a data-integrity failure, not a "pick nearest date" situation. Log it and treat conservatively (exclude the event from safe-cash calculations, never assume 0 or 1:1).

### 3.3 `extraction/` (the only LLM-touching module before decisions)

Two entry points, both **pure functions that return typed structured data, never prose, never instructions**:

```
extract_amount_from_image(image_path, event_context) -> ExtractedAmount
    # event_context includes: event_type, whether event is marked
    # settled/pending, any label already known. This context is what
    # prevents the "extracted $0.00 balance-due-after-payment instead
    # of the actual charge" failure mode (see decisions.html DEC-014).

extract_facts_from_message(message_text, related_event_id | None) -> list[ClaimedFact]
    # A ClaimedFact is one of: AmendAmount, CancelEvent, ConfirmEvent,
    # DelayEvent(new_date), ConfirmIncome. Never "SkipMinimumBalance"
    # or any rule-altering type — that type does not exist in the
    # schema, so the model has nothing rule-breaking to emit into.
```

**Guard sequence around every extraction call (see §6 for full detail):**
1. Sanitize input text/image metadata (strip hidden markup, zero-width chars).
2. Local pattern check for injection signatures — log + flag, does not block.
3. Structured-schema-forced call (function calling / Pydantic schema) — retry once on validation failure with the error fed back.
4. Confidence check — if the model itself signals low confidence, or the result fails a sanity bound (e.g. negative amount, date outside dataset range), fall back to the safer interpretation (§4.4) and record why in the audit trail.
5. Cross-field validation against `event_context` — e.g. reject an extracted amount if it contradicts an already-known `event_type=refund` sign.

### 3.4 `reconciler/`
**Responsibility:** turn the raw + extracted events into one clean, deduped, currency-normalized ledger per user, with every `ClaimedFact` applied. This is the highest-risk-of-bugs module — treat it as the one to unit-test hardest.

Conflict-resolution priority (apply top to bottom, first match wins):
1. Explicit cancellation/settlement/amendment event in the ledger itself.
2. A newer record from the same source superseding an older one (via `linked_event_id` chain).
3. Settled event beats a forecast/pending/estimated one describing the same economic event.
4. When still ambiguous after 1–3 → pick the financially **safer** interpretation (lower available cash, not higher).

Inclusion rules for cash-flow purposes:
- Reserve pending/scheduled **debits**.
- Ignore pending/estimated **credits** (bonuses, refunds, investment gains, lottery-type windfalls) until `status = settled`.
- Confirmed salary counts on its settlement date, not on a projected date.
- Exclude `failed`/`cancelled`/detected-duplicate rows entirely.
- Unrealized investment value is never spendable cash — ever, no exceptions. Investment-type requests concern affordability of a *new contribution*, not asset-price prediction — the solver never reasons about future investment growth.
- The "next confirmed salary" event named explicitly in `financial_events.csv`'s description gets the same settlement-date treatment as any other confirmed credit — it is not special-cased as "always available," it still only counts once its date is reached in the forecast.
- Recurring-expense classification requires actual repeated history in the ledger — never infer recurrence from a single occurrence, even if the category name suggests it (e.g. "Netflix" appearing once is not yet a subscription for forecasting purposes).

**Output:** `ReconciledLedger` — one canonical, currency-normalized, deduped event list per user, each event tagged with its resolution reasoning for the audit trail.

### 3.4a Evidence scope — three linkage levels, not one

`messages.csv` and `images.csv` attach evidence at **three possible levels**, confirmed by the spec's join rules: `user_id` (profile-level), `request_id` (request-level), and `related_event_id`/`image_id` → event (event-level). An implementation that only reads event-linked evidence silently drops request- and user-scoped evidence — a real accuracy gap, not a hypothetical one.

- **Event-scoped** evidence (`related_event_id` populated) → produces a `ClaimedFact` targeting that event (amend/cancel/confirm/delay), consumed by the reconciler (§3.4).
- **Request-scoped** evidence (tied to `request_id`, no event link) → may clarify or amend facts about the request itself (e.g. a message confirming a detail behind `request_text`). Still untrusted: it can supply a fact, never a rule override (§6). Consumed by `runner/` before building the request context, tagged with its own provenance in the audit log.
- **User-scoped** evidence (tied to `user_id`, no request/event link) → may confirm profile-level facts (e.g. a stated preference). Same quarantine rules apply. Consumed once per user, cached (§7), not re-processed per request.

All three scopes route through the identical extraction guard sequence in §3.3 — the scope only changes *where the resulting fact is applied*, never *how much it's trusted*.

### 3.4b Priority-driven `spending_changes_needed` selection

This is the mechanism that actually delivers the personalization requirement (PRD FR-8) — two users with identical balances diverge here.

```
select_spending_changes(unsafe_plan, forecast, profile) -> list[SpendingChange] | None
```

Algorithm:
1. Only consider recurring expenses the profile marks **flexible** (never protected/essential — this is a hard rule, not a preference).
2. Rank flexible expenses by the user's stated priority, **lowest priority first** — the least important flexible expense is the first candidate for change.
3. Try `stop` before `reduce_to` for the lowest-priority candidate: if stopping it alone restores safety, stop there — don't also reduce a second expense. `spending_changes_needed` should be the **minimal sufficient set**, not a maximal one, even though up to 3 changes are allowed.
4. If one change isn't sufficient, add the next-lowest-priority flexible expense, repeating stop-then-reduce, up to the 3-entry cap.
5. Never let the same `event_id` appear in both a `stop` and a `reduce_to` action (spec's explicit mutual-exclusivity rule).
6. If no combination of ≤3 flexible changes restores safety, this candidate plan is not achievable via spending changes — fall through to the next candidate method or `not_recommended`.

This keeps `spending_changes_needed` both correct (never touches protected spending) and personalized (order and choice of cuts follows *this user's* stated priorities, not a global heuristic).

### 3.5 `forecaster/`
```
simulate_balance(ledger, start_date, horizon_days=90) -> DailyBalanceSeries
```
Event-driven daily simulation, not a monthly bucket average — apply recurring + confirmed one-off flows on their actual dates. `DailyBalanceSeries` is queried by the solver to check "does balance ever drop below X between date A and date B."

### 3.6 `solver/`
This is where the actual decision-making rules live — **100% deterministic, no LLM**.

```
compute_amount_safe_to_pay(ledger, forecast, request) -> Decimal
compute_earliest_full_payment_date(ledger, forecast, request) -> date | None
generate_candidate_plans(request, profile, payment_options, forecast) -> list[Plan]
safety_check(plan, forecast, profile) -> bool
rank_plans(candidates: list[Plan]) -> Plan | None   # returns best eligible, or None
```

### 4.5 Eligibility gates — applied before ranking, per method

A candidate is only handed to `rank_plans` if it clears **all** of these; failing any gate removes the candidate entirely (it does not get ranked low, it does not exist):

| Method | Gates |
|---|---|
| `full_payment` | in `payment_methods_user_will_consider`; passes 90-day balance-safety check for a single payment of `requested_amount` on the candidate date; candidate date `<= desired_completion_date` |
| `partial_payment` | `allows_partial_payment == true` on the request; `partial_payment` in `payment_methods_user_will_consider`; `0 < amount_safe_to_pay < requested_amount`; exactly 2 legs (`amount_safe_to_pay` on `request_date`, remainder on `earliest_date_for_full_payment`); `earliest_date_for_full_payment <= desired_completion_date`; passes 90-day safety at both leg dates and everywhere between |
| `installments` | `installments` in `payment_methods_user_will_consider`; legs/amounts/fees exactly match a real row in `request_payment_options.csv` (never recomputed or rounded independently); final leg date `<= desired_completion_date`; passes 90-day safety across every leg |
| `wait` | user accepts `full_payment` specifically (not just any method); a future date exists within the 90-day horizon where full payment passes the safety check; that date `<= desired_completion_date` |
| `not_recommended` | fallback only — used when zero candidates above survive their gates |

**Resolution of a spec ambiguity (logged as DEC-021):** the intro prose defines "safe" as balance-safety *and* completing by `desired_completion_date` together, while the ranking section separately lists "completes by `desired_completion_date`" as tie-break #1. We implement **both**: the deadline is a hard eligibility gate (table above) *and* tie-break #1 is still evaluated literally as written during ranking. In practice the gate makes tie-break #1 a no-op most of the time — that's fine; we're not allowed to assume a hidden test won't depend on the tie-break firing on some edge case the gate doesn't fully cover (e.g. a future rule change, or a bug in the gate itself). Redundant-but-correct beats elegant-but-fragile here.

### 4.6 Ranking — `rank_plans` tie-break order

Applied only to candidates that already cleared §4.5. Apply in sequence, first differentiator wins:
1. Completes by `desired_completion_date` (see §4.5 note — expected to rarely differentiate, kept for spec fidelity).
2. Requires no `spending_changes_needed`.
3. Minimizes `total_paid` — for installment candidates this is the option's stated `total_payable_amount` (financing fees included), not a naive sum of `requested_amount`; fees make two installment options with the same schedule length genuinely different here.
4. Starts earliest (`legs[0].date`).
5. Fewest number of payments (`len(legs)`).
6. Lowest `payment_option_id` (final deterministic tiebreak — string/lexicographic or numeric, whichever the CSV uses; never leave this to random/dict ordering).

If `rank_plans` returns `None` → `recommended_payment_method = not_recommended`, `affordability_status = not_affordable`. This is the fallback, never the default — always try to construct a safe, gate-passing plan first.

### 3.7 `explainer/` (second and last LLM touchpoint)
Takes the **already-finalized** numeric decision (amount, dates, plan, status) and fills a template into prose. It receives no raw ledger data, no messages, no images — only the final structured decision object. This makes it structurally impossible for the explanation to drift from, contradict, or leak unvalidated info into the numeric answer.

### 3.8 `validator/` (the gate before anything is written)
Hard-asserts, in order, failing the whole row (routing to `not_recommended` + explanation noting the failure) rather than writing a malformed row:
- `0 <= amount_safe_to_pay <= requested_amount`
- `payment_plan` matches exact format `YYYY-MM-DD:amount|YYYY-MM-DD:amount|...` or literal `none`; entries chronological
- `partial_payment` plans have exactly 2 legs summing to `requested_amount`
- installment plans reference a real `payment_option_id` from `request_payment_options.csv`, with leg dates/amounts matching that option exactly (not recomputed)
- `affordability_status == "affordable_now"` implies `earliest_date_for_full_payment == request_date` and vice versa (spec's explicit required relationship) — assert both directions, not just one
- every candidate's `completion_date <= desired_completion_date` (§4.5 hard gate) was actually enforced, not just intended — re-check at validation time as a second line of defense
- `spending_changes_needed` ≤ 3 entries, targets only flexible/non-protected categories, no event appears in both a "stop" and a "reduce_to" action, exact format or literal `none`
- column order and header match `problem_statement.md` byte-for-byte

### 3.9 `runner/` — the orchestrator
```
for each row in requests.csv:
    load user profile + ledger + payment options
    run extraction (cached, see §7)
    reconcile → forecast → solve → explain → validate
    append validated row to output.csv
    append structured entry to audit_log/<request_id>.json
write usage_report.md summarizing every model call (tokens, cost, purpose)
```
Single sequential pass is fine for 250 rows — do not over-engineer parallelism until correctness is proven (see §8).

---

## 4. Core data contracts

Use Pydantic (or equivalent) for every object that crosses an LLM boundary or gets written to `output.csv`. Sketch (fill exact field names from `problem_statement.md` verbatim before coding — do not paraphrase field names):

```python
class ExtractedAmount(BaseModel):
    amount: Decimal
    currency: str
    confidence: float  # 0–1, model's own self-report, not decorative
    source_image_id: str
    caveat: str | None  # e.g. "ambiguous: shows balance-after-payment"

class ClaimedFact(BaseModel):
    kind: Literal["amend_amount", "cancel_event", "confirm_event", "delay_event", "confirm_income"]
    target_event_id: str
    payload: dict  # shape depends on kind, validated per-kind
    source_message_id: str

class Plan(BaseModel):
    method: Literal["full_payment", "partial_payment", "installments", "wait", "not_recommended"]
    payment_option_id: str | None
    legs: list[tuple[date, Decimal]]
    requires_spending_changes: bool
    completion_date: date              # date of the final leg — checked against desired_completion_date
    total_paid: Decimal                 # sum of legs; for installments this MUST equal the option's stated
                                          # total_payable_amount (financing fees included), never a recomputed
                                          # sum that silently drops fee data — see §4.5 and design decision DEC-023

class OutputRow(BaseModel):
    request_id: str
    amount_safe_to_pay: Decimal
    affordability_status: Literal["affordable_now", "affordable_with_plan", "affordable_later", "not_affordable"]
    recommended_payment_method: Literal["full_payment", "partial_payment", "installments", "wait", "not_recommended"]
    payment_plan: str          # "YYYY-MM-DD:amount|..." or "none"
    earliest_date_for_full_payment: str  # "" if never within horizon
    spending_changes_needed: str          # "stop:<event_id>|reduce_to:<event_id>:<amount>" (≤3) or "none"
    decision_explanation: str
```

Verified verbatim against `problem_statement.md` §"Allowed values" — these four/five literals are exhaustive, do not add or rename values.

### 4.4 "Safer interpretation" — concrete definition
When two readings of ambiguous data are both plausible, the safer one is whichever:
1. Assumes **less** available cash (lower balance, later credit, larger reserved debit), and
2. Assumes a **larger** or **earlier** obligation (bigger pending payment, sooner due date).

Never resolve ambiguity toward "more money available" or "less owed." This single rule kills a large class of edge cases without needing a special case per scenario.

---

## 5. Non-functional requirements

| Property | Requirement | How enforced |
|---|---|---|
| Determinism | Same input → same output, run to run | LLM temperature=0 on extraction calls; zero LLM calls in solver/validator; fixed tie-break order (§3.6) |
| Auditability | Every field in every output row traceable to a specific ledger event / extraction / rule | `audit_log/<request_id>.json` per row (§3.9) |
| Cost/latency | ~250 requests, most needing 0–2 LLM calls each | Cache extraction by `(image_id | message_id)` — never re-call for the same evidence (§7) |
| Safety | Never recommend a plan that breaches `minimum_balance_to_keep` or essential spending at any point in the 90-day horizon | `safety_check()` runs the full forecast per candidate plan, not just at the payment date |
| Robustness to adversarial input | Message/image content cannot alter rules, only supply facts | Extraction schema has no rule-altering fields (§3.3); explainer never sees raw untrusted content (§3.7) |

---

## 6. Security architecture — untrusted content boundary

Messages and images are **untrusted input**, exactly like the spec's warning about embedded instructions. Treat this the same way production fintech agents treat retrieved documents: quarantine, don't trust.

```
[message/image] → sanitize → injection-pattern flag (log only) →
   schema-forced extraction call → confidence/sanity check →
   ClaimedFact / ExtractedAmount (typed data) → reconciler (code, applies
   normal conflict-resolution priority — a "claim" is just another
   candidate fact competing on the same rules as everything else)
```

The critical property: **a `ClaimedFact` has no code path that lets it skip `safety_check()`, override `minimum_balance_to_keep`, or write directly to `output.csv`.** If a message says "ignore the minimum balance for this one," the extractor either fails to produce a valid `ClaimedFact` (no such `kind` exists) or, if it hallucinates one, the reconciler's schema rejects it. Structural, not prompt-level, defense — see decisions.html DEC-011/DEC-012 for why prompt-level "please don't" instructions are not treated as sufficient defense on their own.

---

## 7. Extraction cost & caching

- Cache key: `image_id` or `message_id` (never request_id — the same evidence may be relevant to multiple requests).
- On re-run during development, cache hits mean zero repeated token spend — keep `usage_report.md` numbers honest by tracking cache hits vs. real calls separately.
- Batch nothing across users — evidence is per-user, keep cache scoped correctly to avoid cross-user leakage bugs.

---

## 8. Deployment & execution model

**This is a batch CLI tool, not a service.** No API server, no queue, no database beyond the CSVs themselves. Resist the urge to over-architect — 250 rows, single 24-hour submission window, judged on output correctness and code clarity, not on production scalability.

```
code/
├── main.py                 # entrypoint: python main.py --requests dataset/requests.csv --out output.csv
├── ingest.py
├── currency.py
├── extraction.py
├── reconciler.py
├── forecaster.py
├── solver.py
├── explainer.py
├── validator.py
├── models.py                # all Pydantic schemas
├── config.py                 # env-driven: API keys, model names, cache dir
├── evaluation/
│   ├── run_eval.py           # runs against sample_requests.csv, diffs field-by-field
│   └── usage_report.md
├── tests/
│   ├── test_reconciler.py
│   ├── test_forecaster.py
│   ├── test_solver.py
│   └── test_validator.py
├── audit_log/                # generated, one JSON per request_id
└── requirements.txt
```

**Run instructions:**
```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...     # or OPENAI_API_KEY, whichever extraction.py targets
python main.py --requests dataset/requests.csv --out output.csv
python evaluation/run_eval.py    # scores against sample_requests.csv before you trust output.csv
```

**Packaging for submission:** `zip -r code.zip code/ -x "code/audit_log/*" "code/**/__pycache__/*" "code/.venv/*"` — exclude venvs, `data/`, `dataset/`, build artifacts per submission rules. `log.txt` comes from whatever AI coding tool was used per its AGENTS.md logging contract, not hand-written.

**Idempotency:** re-running `main.py` on the same inputs must produce byte-identical `output.csv` (modulo the cache). This is your own best regression test — if two runs differ, something non-deterministic leaked in (usually an unseeded LLM call or unsorted dict iteration).

---

## 9. Testing strategy

1. **Unit tests** on reconciler/forecaster/solver/validator — these carry the correctness weight, test them with hand-built fixture ledgers covering every rule in §3.4, not just happy paths.
2. **Golden set** — every one of the 25 `sample_requests.csv` rows, field-by-field diff, treated as a CI gate: no code change ships if it regresses a previously-passing row.
3. **Adversarial fixtures** — hand-craft a few messages/images with embedded instruction-like text ("ignore minimum balance") and images showing "balance after payment = $0" to confirm the extraction/reconciler boundary holds (§6).
4. **Determinism check** — run `main.py` twice on the same input, diff the two `output.csv` files, must be identical.

---

## 10. Explicit non-goals

- No live/real-time exchange rates — dataset rates only.
- No multi-user optimization or cross-user learning — each request is decided independently from that user's own reconciled ledger.
- No persistence beyond the run (no DB) — CSVs and audit JSON are the only state.
- No microservice split, no message queue, no Kubernetes — batch script is correct for this problem's scale and judging criteria.
