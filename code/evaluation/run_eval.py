"""run_eval: reproduce the submitted output.csv from the code + dataset and
verify it against the public gold sample answers (sample_requests.csv).

This is the evaluator entry point referenced by README. It re-runs the exact
reconcile/forecast/decide path used to produce the submission output.csv, then
reports exact match counts per decision field over the 25 public samples.
The real eval requests (request_100+) never overlap the sample ids, so the
sample feed is the only calibration surface available.

Run:  python3 evaluation/run_eval.py [dataset_dir]
"""
from __future__ import annotations

import csv
import sys
import tempfile
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "code"))

import config  # noqa: E402
import forecaster as FC  # noqa: E402
import ingest as ING  # noqa: E402
import main as MAIN  # noqa: E402
import messages_rules as MR  # noqa: E402
import reconciler as RC  # noqa: E402

OUT_FIELDS = (
    "amount_safe_to_pay",
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
    "decision_explanation",
)


def build_sample_requests_csv(sample_rows: list[dict], path: Path) -> None:
    cols = [
        "request_id",
        "user_id",
        "request_date",
        "request_type",
        "requested_amount",
        "desired_completion_date",
        "allows_partial_payment",
        "request_text",
    ]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in sample_rows:
            w.writerow({c: r[c] for c in cols})


def money_norm(v: str) -> str:
    """Normalize gold-style numbers in a value: floats lean on '.00' trim."""
    if v in ("", "none"):
        return v

    def one(s: str) -> str:
        if ":" in s and s.partition(":")[0] == "reduce_to":
            head, _, tail = s.partition("reduce_to:")
            _id, _, amt = tail.rpartition(":")
            num = f"{float(amt):.2f}"
            return f"reduce_to:{_id}:{num[:-3] if num.endswith('.00') else num}"
        try:
            num = f"{float(s):.2f}"
        except ValueError:
            return s
        return num[:-3] if num.endswith(".00") else num

    return "|".join(one(s) for s in v.split("|"))


def main() -> int:
    dataset_dir = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "dataset")
    with open(dataset_dir / "sample_requests.csv") as f:
        gold_rows = list(csv.DictReader(f))

    tmp = Path(tempfile.mkdtemp(prefix="run_eval_"))
    try:
        build_sample_requests_csv(gold_rows, tmp / "requests.csv")
        for name in (
            "financial_profiles.csv",
            "financial_events.csv",
            "exchange_rates.csv",
            "request_payment_options.csv",
            "messages.csv",
            "images.csv",
            "output.csv",
        ):
            shutil.copy(dataset_dir / name, tmp / name)
        if (dataset_dir / "media").exists():
            shutil.copytree(dataset_dir / "media", tmp / "media")

        ds = ING.load_all(tmp)
        msg_facts = MR.parse_messages(
            list(csv.DictReader(open(tmp / "messages.csv")))
        )

        counts = {k: 0 for k in OUT_FIELDS}
        divergent = []
        for gold in gold_rows:
            request = next(r for r in ds.requests if r.request_id == gold["request_id"])
            horizon = max(
                config.FORECAST_HORIZON_DAYS,
                (request.desired_completion_date - request.request_date).days,
            )
            ledger = RC.reconcile(
                ds,
                request.user_id,
                request.request_date,
                msg_facts=msg_facts.get(request.user_id, []),
                horizon_days=horizon,
            )
            row = MAIN.decide(ds, request, ledger, FC.simulate_balance(ledger))

            diffs = []
            for k in OUT_FIELDS:
                mine, theirs = row[k].strip(), gold[k].strip()
                if k in ("amount_safe_to_pay", "spending_changes_needed"):
                    mine = money_norm(mine)
                    theirs = money_norm(theirs)
                if mine == theirs:
                    counts[k] += 1
                else:
                    diffs.append(f"{k}: mine={mine!r} gold={theirs!r}")
            if diffs:
                divergent.append((request.request_id, diffs))

        n = len(gold_rows)
        print(f"exact matches per field ({n} samples):")
        for k in OUT_FIELDS:
            print(f"  {k}: {counts[k]}/{n}")
        print(f"\ndivergent rows ({len(divergent)}/{n}):")
        for rid, diffs in divergent:
            print(f"  {rid}:")
            for d in diffs:
                print(f"    {d}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())