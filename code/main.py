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


_MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def money_comma(x) -> str:
    """Gold-style human amount: thousands separated, 2dp kept unless whole."""
    s = f"{Decimal(x):,.2f}"
    if s.endswith(".00"):
        s = s[:-3]
    return s


def human_date(d: dt.date) -> str:
    """Gold-style human date: '15 September 2024' (no zero padding)."""
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year}"


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


def _event_name(ds, request, event_id: str) -> str:
    """Display name for a change target: its event description when available."""
    for e in ds.events_by_user.get(request.user_id, []):
        if e.event_id == event_id:
            if e.description:
                return e.description
            return e.category.replace("_", " ")
    return event_id


def _change_lead(ds, request, changes) -> str:
    """Gold-style spending-change preface: 'Stop the X and reduce the Y to Z,'."""
    parts = []
    for ch in changes:
        name = _event_name(ds, request, ch.event_id)
        if ch.action == "stop":
            parts.append(f"stop the {name}")
        else:
            parts.append(f"reduce the {name} to {cur_text(ds, request, ch.new_amount)}")
    text = parts[0].capitalize()
    if len(parts) > 2:
        text += ", " + ", ".join(parts[1:-1])
    if len(parts) > 1:
        text += " and " + parts[-1]
    return text + ","


def cur_text(ds, request, amount) -> str:
    return f"{ds.profiles[request.user_id].home_currency} {money_comma(amount)}"


def resolve_image_amounts(ds: ING.RawDataset):
    """Blank-amount receipts -> {event_id: amount} via the caged VLM layer.

    Only runs when an extraction provider + API key is configured (DEC-027).
    Results are cached on disk keyed by (user, image_id) (DEC-014). Without a
    key this returns an empty map and nothing changes: unresolved blanks stay
    conservatively excluded (DEC-006/032), so the deterministic output is
    byte-identical whether or not a key is present.
    """
    if not config.EXTRACTION_PROVIDER:
        return {}, None
    try:
        from extraction import ExtractionEngine

        engine = ExtractionEngine(cache_dir=config.CACHE_DIR)
    except Exception:
        return {}, None  # broken/missing provider config: degrade to no-key path

    img_by_event = {im.related_event_id: im for im in ds.images if im.related_event_id}
    amounts: dict[str, Decimal] = {}
    for e in ds.blank_amount_events:
        img = img_by_event.get(e.event_id)
        if img is None:
            continue
        try:
            ex = engine.extract_amount_from_image(
                ING.image_path(ds, img.image_id),
                {
                    "event_type": e.event_type,
                    "description": e.description,
                    "status": e.status,
                    "category": e.category,
                    "direction": e.direction,
                },
                e.user_id,
                img.image_id,
            )
            amounts[e.event_id] = ex.amount
        except Exception as exc:  # ExtractionError and network/parse failures
            print(f"  warn: image extraction {img.image_id} skipped ({exc!r})")
    return amounts, engine


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
    min_txt = f"{cur} {money_comma(prof.minimum_balance_to_keep)}"

    if best is None:
        if safe > ZERO:
            reason = (
                f"Do not proceed with the {cur} {money_comma(requested)} request. "
                f"Although {cur} {money_comma(safe)} is available today, the full amount "
                f"cannot be completed safely within 90 days."
            )
        else:
            reason = (
                f"Do not make this payment by {human_date(request.desired_completion_date)}. "
                f"None of the available options keeps the {min_txt} minimum protected."
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
    changes = best.spending_changes or []
    if method == "wait":
        status = "affordable_later"
        reason = (
            f"Pay {cur} {money_comma(requested)} in full on "
            f"{human_date(earliest)}. Paying earlier would take the balance below the "
            f"{min_txt} minimum."
        )
    elif (
        method == "full_payment"
        and not changes
        and safe >= requested
    ):
        status = "affordable_now"
        reason = (
            f"Pay {cur} {money_comma(requested)} today. This leaves at least "
            f"{min_txt} available over the next 90 days."
        )
        earliest = request.request_date
    else:
        status = "affordable_with_plan"
        if method == "partial_payment":
            rest = (requested - safe).quantize(Decimal("0.01"))
            reason = (
                f"Pay {cur} {money_comma(safe)} today and the remaining "
                f"{cur} {money_comma(rest)} on {human_date(best.completion_date)}. "
                f"This completes the full request and keeps the {min_txt} minimum protected."
            )
        elif method == "full_payment":
            if changes:
                lead = (
                    f"{_change_lead(ds, request, changes)} then pay "
                    f"{cur} {money_comma(requested)} today. "
                )
            else:
                lead = f"Pay {cur} {money_comma(requested)} today. "
            reason = lead + f"This leaves at least {min_txt} available."
        else:  # installments
            n = best.number_of_payments
            reason = (
                f"Use {n} installments of {cur} {money_comma(best.legs[0][1])}, starting "
                f"{human_date(best.first_payment_date)}. This leaves at least "
                f"{min_txt} available."
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
    image_amounts, engine = resolve_image_amounts(ds)
    if engine is not None:
        print(
            f"image extraction active: provider={config.EXTRACTION_PROVIDER}, "
            f"resolved {len(image_amounts)} blank-amount event(s)"
        )

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
            image_amounts=image_amounts,
        )
        forecast = FC.simulate_balance(ledger)
        row = decide(ds, request, ledger, forecast)
        rows.append({"request_id": request.request_id, **row})

    if engine is not None:
        st = engine.stats
        print(
            f"extraction usage: image_calls={st.image_misses} image_cache_hits={st.image_hits}"
        )

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