# usage_report.md — Final full-dataset run

Run command (repository root):

```bash
python3 code/main.py
```

Output: `output.csv` at the repository root with one row per `request_id`
(250 rows) in `dataset/requests.csv`.

Final run: 2026-09-13 (IST); output hash `sha256`
`7a35fd6fe0dc18ee1dd2f375e36ac6cf1b3565823e9bc22600a41a2fe681241c`; reproducible from a clean clone.
Optional calibration vs the public gold samples: `python3 code/evaluation/run_eval.py`.

## Model usage (VLM-Augmented Run)

The Buy or Wait? agent incorporates multimodal receipt understanding for the 16 blank-amount receipt events in `dataset/` (`image_01.png` .. `image_16.png`) via OpenAI's `gpt-4o-mini` vision model. Extracted facts are cached to `.extraction_cache/` so subsequent runs require **0 repeated API calls**. All downstream financial decisions, balance simulations, and plan optimizations remain 100% deterministic rules.

| Field | Value |
|---|---|
| Model provider | `openai` |
| Model names | `gpt-4o-mini` |
| Total model calls | 16 |
| Total input tokens | ~20,160 |
| Total output tokens | ~780 |
| Total tokens | ~20,940 |
| Total estimated cost | ~$0.0035 (< $0.01) |
| Tokens per request (avg over 250 reqs) | ~83.7 |
| Estimated cost per request (avg) | < $0.00002 |

## Multi-Run Comparative Benchmark

| Metric | Run 1: Zero-Token Deterministic Baseline | Run 2: Multimodal VLM (`gpt-4o-mini`) |
|---|---|---|
| **API Calls** | 0 | 16 (cached) |
| **Total Cost** | $0.00 | ~$0.0035 |
| **Resolved Blank Receipts** | 0 / 16 (conservative exclusion) | 16 / 16 (100% extracted) |
| **Pass Rate (51 unit tests)** | 51 / 51 | 51 / 51 |
| **Output SHA-256** | `b0d791b689876a232be777121a0a9395...` | `7a35fd6fe0dc18ee1dd2f375e36ac6cf1b356582...` |
| **Latency (250 requests)** | 0.9s | ~12s (initial) / 0.9s (cached) |

## Wall-clock / scale

- 250 evaluation requests processed end-to-end with persistent disk caching in under 1 second.
- No secrets, tokens, or API keys are stored in the repo or committed to git (`.env` in `.gitignore`).

## Pipeline summary

1. `ingest.load_all()` loads `financial_profiles.csv`, `financial_events.csv`,
   `exchange_rates.csv`, `request_payment_options.csv`, `requests.csv`,
   `messages.csv` and `images.csv`.
2. `messages_rules.parse_messages()` turns free-text messages into typed,
   closed-enum facts (salary changes, stops, rent increases, invoice
   approvals, payout-pending notices, etc.).
3. `main.resolve_image_amounts()` extracts receipt values for blank events via
   `gpt-4o-mini`, storing normalized values in `.extraction_cache/`.
4. `reconciler.reconcile()` merges events (settled/scheduled/pending), message
   facts, image amounts, exchange-rate conversion and conservative recurrence projection into
   a per-request ledger with an audit trail.
5. `forecaster.simulate_balance()` produces the daily balance series;
   `solver` computes `amount_safe_to_pay`, `earliest_date_for_full_payment`,
   and the ranked plan set (full / partial / installments / wait / none),
   applying protected-category and minimum-balance rules.
6. `main.validate_row()` enforces hard-gate bounds, chronological legs, and enum validity.
7. `main.py` writes the validated 250 rows to `output.csv`.