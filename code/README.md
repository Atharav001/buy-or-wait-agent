# Buy or Wait? — AI Financial Decision Agent
> **HackerRank Orchestrate (September 2026)**  
> *An AI-powered, deterministic financial decision engine with caged multimodal receipt extraction and hard-gate invariant verification.*

---

## 1. Overview

The **Buy or Wait?** agent evaluates whether an individual can safely afford a purchase without violating essential commitments, recurring bills, or their personalized `minimum_balance_to_keep` across a conservative 90-day forecast horizon.

For every purchase request in `dataset/requests.csv`, the agent decides whether the user should:
- **`full_payment`**: Pay the entire amount immediately.
- **`partial_payment`**: Pay the safe amount today and the remainder on or before the desired completion date.
- **`installments`**: Utilize an available seller financing option that respects the user's installment preferences.
- **`wait`**: Defer payment until the earliest projected date when a full payment is safe.
- **`not_recommended`**: Decline the purchase because it cannot be completed safely without breaching financial safety invariants.

The decision engine reconciles multiple data streams: multi-currency transactions, dated exchange rates, confirmed recurring salaries, flexible vs. non-flexible expenses, and supporting messages or receipt images.

---

## 2. System Architecture & Flow

```mermaid
flowchart TD
    subgraph DataSources ["Multi-Source Ingestion"]
        P[Profiles & Balances] --> Ingest[ingest.py]
        E[Financial Events & FX] --> Ingest
        R[Purchase Requests & Options] --> Ingest
        M[Messages] --> Rules[messages_rules.py]
        I[Receipt Images] --> VLM[extraction.py (VLM)]
    end

    subgraph CoreEngine ["Deterministic Core Engine"]
        Ingest --> Reconcile[reconciler.py: Cash-Flow Ledger]
        Rules -->|Claimed Facts| Reconcile
        VLM -->|Extracted Amounts| Reconcile
        Reconcile --> Forecast[forecaster.py: Daily Balance Forecast]
        Forecast --> Solver[solver.py: Optimization & Plan Ranking]
    end

    subgraph Verification ["Hard-Gate Validation & Output"]
        Solver --> Dec[main.py: Formatter & Validator]
        Dec -->|Schema & Safety Pass| Out[output.csv (250 Rows)]
        Dec -->|Validation Trip Fallback| Out
    end

    style DataSources fill:#f8f9fa,stroke:#ced4da
    style CoreEngine fill:#e8f4f8,stroke:#bee5eb
    style Verification fill:#d4edda,stroke:#c3e6cb
```

---

## 3. Complete Benchmark & Model Performance Stats

### Dual-Run Model Comparison

| Metric | Run 1: Big Pickle (OpenCode) | Run 2: OpenAI `gpt-4o-mini` (Multimodal VLM) [Final] |
|---|---|---|
| **Model Provider / Name** | Big Pickle (OpenCode) | OpenAI (`gpt-4o-mini` Vision) |
| **Model Status / Capability** | Vision unsupported (No model execution) | **16 / 16 receipt images extracted (100%)** |
| **Total API Calls** | 0 | **16 (cached)** |
| **Total Input Tokens** | 0 | **~20,160** |
| **Total Output Tokens** | 0 | **~780** |
| **Total Tokens** | 0 | **~20,940** |
| **Total Estimated Cost** | **$0.00** | **~$0.0035** (< 1 cent) |
| **Cost Per Request** | $0.00 | **<$0.00002** |
| **Pass Rate (51 Unit Tests)** | 51 / 51 | **51 / 51** |
| **Validator Violations** | 0 / 250 | **0 / 250** |
| **End-to-End Latency** | 0.9s | ~12s (initial) / **0.9s (cached)** |
| **Output SHA-256** | `b0d791b689876a23...` | `7a35fd6fe0dc18ee1dd2f375e36ac6cf1b3565823e9bc22600a41a2fe681241c` |

### Calibration Scores (25 Ground-Truth Golden Samples)

| Field | Accuracy | Notes |
|---|---|---|
| `affordability_status` | **80.0%** (20/25) | Exact match on affordability state |
| `recommended_payment_method` | **84.0%** (21/25) | Full payment vs installment vs wait vs decline |
| `payment_plan` | **80.0%** (20/25) | Exact chronological payment leg schedules |
| `earliest_date_for_full_payment`| **76.0%** (19/25) | First conservative date for safe payment |
| `spending_changes_needed` | **88.0%** (22/25) | Stop / reduce flexible spending changes |
| `decision_explanation` | **52.0%** (13/25) | Gold template prose match (`human_date`, `money_comma`) |
| `amount_safe_to_pay` | **12.0%** (3/25) | Conservative floor protection |

---

## 4. Setup & Execution Instructions

### Setup Table

| Requirement | Details |
|---|---|
| **Python Version** | Python 3.10+ (Tested on Python 3.13) |
| **Dependencies** | Listed in `code/requirements.txt` (`pydantic`, `pandas`, `openai`, `pytest`, `python-dotenv`) |
| **Entry Point** | `code/main.py` |
| **API Configuration** | Set `OPENAI_API_KEY` in `.env` (optional: pre-cached extractions ship with `code.zip`) |

### Commands

```bash
# 1. Install dependencies
pip install -r code/requirements.txt

# 2. Run unit test suite (51 tests)
pytest

# 3. Execute the full prediction pipeline
python3 code/main.py

# 4. Run calibration evaluation against sample requests
python3 code/evaluation/run_eval.py

# 5. Check output SHA-256 checksum
shasum -a 256 output.csv
```

---

## 5. Submission Deliverables

Per the challenge contract in `AGENTS.md` and `problem_statement.md`:
1. **`code.zip`**: Complete source code package including `code/`, tests, pre-cached receipt extractions in `.extraction_cache/`, and `evaluation/usage_report.md`.
2. **`output.csv`**: Exactly 250 rows matching the required evaluation format at the repository root.
3. **`chat_transcript`**: Complete interaction history from `log.txt`.

**HackerRank Submission Portal:**  
https://www.hackerrank.com/contests/hackerrank-orchestrate-september26/challenges/buy-or-wait/submission
