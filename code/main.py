"""main: deterministic Buy or Wait? pipeline -> output.csv (repo root).

For every request in dataset/requests.csv:
  1. reconcile every relevant financial fact (events + messages) into a ledger
  2. simulate the daily balance forecast over a conservative horizon
  3. compute amount_safe_to_pay and earliest_date_for_full_payment
  4. build the best valid plan (full / partial / installments / wait)
  5. map it to the required output schema and write output.csv

Run:  python3 code/main.py
"""
from __future__ import annotations

import csv
import datetime as dt
import sys
from decimal import Decimal
from pathlib import Path

import config
import forecaster as FC
import ingest as ING
import messages_rules as MR
import reconciler as RC
import solver as SOL


ZERO = Decimal("0")


# ---------------------------------------------------------------------------
# gold-style decimal strings (2 dp, ".00" trimmed on whole numbers)
# ---------------------------------------------------------------------------

def money(x) -> str:
    s = f"{Decimal(x):.2f}"
    if s.endswith(".00"):
        s = s[:-3]
    return s


def plan_string(legs: list) -> str:
    return "|".join(f"{d.isoformat()}:{money(a)}" for d, a in legs)


def changes_string(changes: list) -> str:
    if not changes:
        return "none"
    parts = []
    for ch in changes[: config.MAX_SPENDING_CHANGES]:
        if ch.action == "stop":
            parts.append(f"stop:{ch.event_id}")
        else:
            parts.append(f"reduce_to:{ch.event_id}:{money(ch.new_amount)}")
    return "|".join(parts)


# ---------------------------------------------------------------------------
# affordability decision + explanation (mirrors sample gold semantics)
# ---------------------------------------------------------------------------

def decide(ds, request, ledger, forecast):
    requested = request.requested_amount
    safe = SOL.compute_amount_safe_to_pay(ledger, forecast, requested)
    earliest = SOL.compute_earliest_full_payment_date(ledger, forecast, requested)
    flows = ledger.flows

    candidates = SOL.generate_candidate_plans(ds, ledger, request, flows)
    best = SOL.rank_plans(candidates, request)

    prof = ledger.profile
    cur = ds.profiles[request.user_id].home_currency

    if best is None:
        reason = (
            f"Do not make this payment by {request.desired_completion_date.isoformat()}. "
            f"None of the available options keeps the {cur} {money(prof.minimum_balance_to_keep)} "
            "minimum protected."
        )
        return {
            "amount_safe_to_pay": money(safe),
            "affordability_status": "not_affordable",
            "recommended_payment_method": "not_recommended",
            "payment_plan": "none",
            "earliest_date_for_full_payment": "",
            "spending_changes_needed": "none",
            "decision_explanation": reason,
        }

    method = best.method
    if method == "wait":
        status = "affordable_later"
        reason = (
            f"Wait until {earliest.isoformat()}, then pay {cur} {money(requested)} in full. "
            f"Paying sooner would take the balance below the {cur} "
            f"{money(prof.minimum_balance_to_keep)} minimum."
        )
    elif (
        method == "full_payment"
        and not best.spending_changes
        and safe >= requested
    ):
        status = "affordable_now"
        reason = (
            f"Pay {cur} {money(requested)} in full on "
            f"{best.first_payment_date.isoformat()}. This is safe and keeps at least {cur} "
            f"{money(prof.minimum_balance_to_keep)} available."
        )
        earliest = request.request_date
    else:
        status = "affordable_with_plan"
        if method == "partial_payment":
            rest = (requested - safe).quantize(Decimal("0.01"))
            reason = (
                f"Pay {cur} {money(safe)} today and the remaining {cur} {money(rest)} on "
                f"{best.completion_date.isoformat()}. This completes the full request and keeps "
                f"the {cur} {money(prof.minimum_balance_to_keep)} minimum protected."
            )
        elif method == "full_payment":
            cuts = best.spending_changes or []
            if cuts:
                lead = (
                    f"Apply {changes_string(cuts)}, then pay {cur} {money(requested)} today. "
                )
            else:
                lead = f"Pay {cur} {money(requested)} today. "
            reason = (
                lead
                + f"This keeps at least {cur} {money(prof.minimum_balance_to_keep)} available."
            )
        else:  # installments
            n = best.number_of_payments
            reason = (
                f"Use {n} installments of {cur} {money(best.legs[0][1])}, starting "
                f"{best.first_payment_date.isoformat()}. This leaves at least {cur} "
                f"{money(prof.minimum_balance_to_keep)} available."
            )

    return {
        "amount_safe_to_pay": money(safe),
        "affordability_status": status,
        "recommended_payment_method": method,
        "payment_plan": plan_string(best.legs),
        "earliest_date_for_full_payment": earliest.isoformat() if earliest else "",
        "spending_changes_needed": changes_string(best.spending_changes),
        "decision_explanation": reason,
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def run(dataset_dir: Path, out_path: Path) -> None:
    ds = ING.load_all(dataset_dir)
    msg_facts = MR.parse_messages(list(csv.DictReader(open(dataset_dir / "messages.csv"))))

    rows = []
    for request in ds.requests:
        # Balance safety and earliest_full_payment are evaluated over a longer,
        # fixed forecast window (gold measures safe over ~90 days); PLANS only
        # need to hold the minimum balance until they complete, which the
        # solver enforces through plan.completion_date.
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
        forecast = FC.simulate_balance(ledger)
        row = decide(ds, request, ledger, forecast)
        rows.append({"request_id": request.request_id, **row})

    cols = [
        "request_id",
        "amount_safe_to_pay",
        "affordability_status",
        "recommended_payment_method",
        "payment_plan",
        "earliest_date_for_full_payment",
        "spending_changes_needed",
        "decision_explanation",
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    print(f"wrote {len(rows)} rows to {out_path}")
    return rows


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    dataset_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "dataset"
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else root / "output.csv"
    run(dataset_dir, out_path)