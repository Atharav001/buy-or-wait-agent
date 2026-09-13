# usage_report.md — Final full-dataset run

Run command (repository root):

```bash
python3 code/main.py
```

Output: `output.csv` at the repository root with one row per `request_id`
(250 rows) in `dataset/requests.csv`.

Final run: 2026-09-13 (IST); output hash `sha256`
`5b5f126b4d058c2a744cf5ed8a73ae57bb53beef`; reproducible from a clean clone.
Optional calibration vs the public gold samples: `python3 code/evaluation/run_eval.py`.

## Model usage

The Buy or Wait? agent is **fully deterministic** (single run, reproducible,
no randomness). It does not call any external model during the prediction
phase: every financial decision is produced by fixed rules over the provided
`dataset/` inputs (`ingest.py`, `messages_rules.py`, `reconciler.py`,
`forecaster.py`, `solver.py`, `main.py`).

| Field | Value |
|---|---|
| Model provider | none |
| Model names | none |
| Total model calls | 0 |
| Total input tokens | 0 |
| Total output tokens | 0 |
| Total tokens | 0 |
| Total estimated cost | 0 |
| Tokens per request (avg) | 0 |
| Estimated cost per request (avg) | 0 |

## Wall-clock / scale

- 250 evaluation requests processed in under ~1 second on a stock machine
  (0.9 s end-to-end including CSV I/O in this environment).
- No API keys, credentials, or sensitive configuration are used or stored.

## Pipeline summary

1. `ingest.load_all()` loads `financial_profiles.csv`, `financial_events.csv`,
   `exchange_rates.csv`, `request_payment_options.csv`, `requests.csv`,
   `messages.csv` and `images.csv`.
2. `messages_rules.parse_messages()` turns free-text messages into typed,
   closed-enum facts (salary changes, stops, rent increases, invoice
   approvals, payout-pending notices, etc.).
3. `reconciler.reconcile()` merges events (settled/scheduled/pending), message
   facts, exchange-rate conversion and conservative recurrence projection into
   a per-request ledger with an audit trail.
4. `forecaster.simulate_balance()` produces the daily balance series;
   `solver` computes `amount_safe_to_pay`, `earliest_date_for_full_payment`,
   and the ranked plan set (full / partial / installments / wait / none),
   applying protected-category and minimum-balance rules.
5. `main.py` maps the best plan to the required output schema and writes
   `output.csv`.

## Opt-in extraction path (not part of this run)

`main.resolve_image_amounts()` can read the 16 blank-amount receipts
(`image_01`..`image_16`, DEC-034) off disk through the caged VLM layer — but
that path activates **only** when an API key and `EXTRACTION_PROVIDER` are
present. On the evaluated run no key was set, so it contributed **0 calls / 0
tokens / 0 cost** and every blank-amount event stayed on the conservative
exclusion path. A future run with a key would report its own usage here; this
report documents exactly the submitted `output.csv`.