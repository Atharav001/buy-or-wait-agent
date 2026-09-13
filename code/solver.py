"""solver: the deterministic decision engine (design.md section 3.6).

100% plain code. Produces amount_safe_to_pay, earliest_full_payment_date,
candidate plans, safety checks and the 6-step tie-broken ranking. Nothing an
LLM outputs touches these numbers directly (DEC-001).
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Optional

from forecaster import (
    DailyBalanceSeries,
    simulate_from,
)
from ingest import RawDataset
from models import Plan, PurchaseRequest, SpendingChange
from reconciler import (
    ReconciledLedger,
    Recurrence,
    eligible_flexible_streams,
    flows_with_spending_changes,
)

ZERO = Decimal("0")
ROUND = Decimal("0.01")


# ---------------------------------------------------------------------------
# Core capacity measures (no spending changes)
# ---------------------------------------------------------------------------


def compute_amount_safe_to_pay(
    ledger: ReconciledLedger,
    forecast: DailyBalanceSeries,
    requested_amount: Decimal,
) -> Decimal:
    """Max safe single payment on request_date, capped at requested_amount.

    = min over horizon of (projected balance - minimum_balance_to_keep),
    floored at 0 and capped at requested_amount (0 <= safe <= requested).
    """
    cushion = forecast.min_balance_between(
        ledger.request_date, ledger.horizon_end
    ) - ledger.profile.minimum_balance_to_keep
    safe = max(ZERO, cushion)
    return min(safe, requested_amount).quantize(ROUND)


def compute_earliest_full_payment_date(
    ledger: ReconciledLedger,
    forecast: DailyBalanceSeries,
    requested_amount: Decimal,
) -> Optional[dt.date]:
    """First date the full amount passes the safety check without changes."""
    floor = ledger.profile.minimum_balance_to_keep
    day = ledger.request_date
    while day <= ledger.horizon_end:
        min_after = forecast.min_balance_between(day, ledger.horizon_end)
        if min_after - requested_amount >= floor:
            return day
        day += dt.timedelta(days=1)
    return None


# ---------------------------------------------------------------------------
# Safety check
# ---------------------------------------------------------------------------


def plan_is_safe(
    ledger: ReconciledLedger,
    flows: dict[dt.date, Decimal],
    plan: Plan,
    from_date: Optional[dt.date] = None,
) -> bool:
    """True if simulating with the plan's legs keeps balance >= floor always.

    The safety window ends at the plan's own completion date (the 'forecast
    period' of a recommendation is the time needed to complete it), never
    extending past the ledger horizon.
    """
    floor = ledger.profile.minimum_balance_to_keep
    bound = min(ledger.horizon_end, plan.completion_date)
    merged = dict(flows)
    for d, amt in plan.legs:
        if d < ledger.request_date or d > ledger.horizon_end:
            continue
        merged[d] = merged.get(d, ZERO) - amt
    series = simulate_from(ledger, merged.items())
    start = from_date if from_date is not None else ledger.request_date
    return series.min_balance_between(start, bound) >= floor


# ---------------------------------------------------------------------------
# Spending-change augmentation (DEC-025, DEC-028)
# ---------------------------------------------------------------------------


def select_spending_changes(
    ledger: ReconciledLedger,
    flows: dict[dt.date, Decimal],
    plan: Plan,
    max_changes: int = 3,
) -> Optional[list[SpendingChange]]:
    """Find the minimal sufficient set of flexible cuts (largest first) that
    makes `plan` safe, or None if <=max_changes cuts can't do it."""
    floor = ledger.profile.minimum_balance_to_keep
    candidates = eligible_flexible_streams(ledger)
    used: list[tuple[Recurrence, str, Optional[Decimal]]] = []
    target_changes: list[SpendingChange] = []

    def current_min() -> Decimal:
        nonlocal flows
        mf = flows_with_spending_changes(ledger, used, plan.first_payment_date)
        merged = dict(mf)
        for d, amt in plan.legs:
            merged[d] = merged.get(d, ZERO) - amt
        bound = min(ledger.horizon_end, plan.completion_date)
        return simulate_from(ledger, merged.items()).min_balance_between(
            ledger.request_date, bound
        )

    if current_min() >= floor:
        return []

    for rec, can_stop, can_reduce in candidates:
        if len(target_changes) >= max_changes:
            break
        action: Optional[str] = None
        new_amount: Optional[Decimal] = None
        if can_stop:
            action = "stop"
        elif can_reduce:
            action = "reduce_to"
            new_amount = rec.minimum_allowed_amount
        if action is None:
            continue
        used.append((rec, action, new_amount))
        if current_min() < floor:
            # this cut alone wasn't enough; keep it and continue adding
            pass
        # represent as SpendingChange
        if action == "stop":
            target_changes.append(
                SpendingChange(action="stop", event_id=rec.event_ids[-1])
            )
        else:
            target_changes.append(
                SpendingChange(
                    action="reduce_to",
                    event_id=rec.event_ids[-1],
                    new_amount=new_amount,
                )
            )
        if current_min() >= floor:
            return target_changes
    return None


# ---------------------------------------------------------------------------
# Candidate generation (design.md section 4.5 eligibility gates)
# ---------------------------------------------------------------------------


def generate_candidate_plans(
    ds: RawDataset,
    ledger: ReconciledLedger,
    req: PurchaseRequest,
    flows: dict[dt.date, Decimal],
) -> list[Plan]:
    prof = ledger.profile
    consider = set(prof.payment_methods_user_will_consider)
    requested = req.requested_amount
    forecast = simulate_from(ledger, flows.items())
    candidates: list[Plan] = []

    def build(
        method: str,
        legs: list[tuple[dt.date, Decimal]],
        total_paid: Decimal,
        payment_option_id: Optional[str] = None,
        changes: Optional[list[SpendingChange]] = None,
    ) -> Plan:
        return Plan(
            method=method,
            payment_option_id=payment_option_id,
            legs=legs,
            spending_changes=changes or [],
            completion_date=legs[-1][0],
            total_paid=total_paid.quantize(ROUND),
        )

    # ---- full_payment at request_date -------------------------------------
    if "full_payment" in consider:
        base = build(
            "full_payment",
            [(req.request_date, requested)],
            requested,
        )
        if plan_is_safe(ledger, flows, base):
            candidates.append(base)
        else:
            changes = select_spending_changes(ledger, flows, base)
            if changes:
                c = build("full_payment", base.legs, base.total_paid, changes=changes)
                candidates.append(c)

    # ---- partial_payment --------------------------------------------------
    if req.allows_partial_payment and "partial_payment" in consider:
        amount_safe = compute_amount_safe_to_pay(ledger, forecast, requested)
        earliest = compute_earliest_full_payment_date(ledger, forecast, requested)
        if (
            ZERO < amount_safe < requested
            and earliest is not None
            and earliest <= req.desired_completion_date
        ):
            legs = [
                (req.request_date, amount_safe),
                (earliest, (requested - amount_safe).quantize(ROUND)),
            ]
            p = build("partial_payment", legs, requested)
            if plan_is_safe(ledger, flows, p):
                candidates.append(p)

    # ---- installments ------------------------------------------------------
    if "installments" in consider and prof.max_installment_months:
        max_months = prof.max_installment_months
        for option in ds.payment_options.get(req.request_id, []):
            if option.payment_method != "installments":
                continue
            if option.number_of_payments > max_months:
                continue
            if option.first_payment_date < req.request_date:
                continue
            legs: list[tuple[dt.date, Decimal]] = []
            freq = option.payment_frequency_days or 0
            finish_ok = True
            for i in range(option.number_of_payments):
                d = option.first_payment_date + dt.timedelta(days=i * freq)
                if d > ledger.horizon_end:
                    finish_ok = False
                    break
                legs.append((d, option.payment_amount.quantize(ROUND)))
            if not legs or not finish_ok:
                continue
            base = build(
                "installments",
                legs,
                option.total_payable_amount,
                payment_option_id=option.payment_option_id,
            )
            if plan_is_safe(ledger, flows, base):
                candidates.append(base)
            else:
                changes = select_spending_changes(ledger, flows, base)
                if changes:
                    c = build(
                        "installments",
                        legs,
                        base.total_paid,
                        payment_option_id=option.payment_option_id,
                        changes=changes,
                    )
                    candidates.append(c)

    # ---- wait --------------------------------------------------------------
    if "full_payment" in consider:
        earliest = compute_earliest_full_payment_date(ledger, forecast, requested)
        if (
            earliest is not None
            and earliest <= req.desired_completion_date
            and earliest > req.request_date
        ):
            candidates.append(
                build("wait", [(earliest, requested)], requested)
            )

    # deadline is a hard gate AND tie-break #1 (DEC-021): drop candidates that
    # complete after desired_completion_date
    candidates = [
        c for c in candidates if c.completion_date <= req.desired_completion_date
    ]
    return candidates


# ---------------------------------------------------------------------------
# Ranking (design.md section 4.6) — 6-step deterministic tie-break
# ---------------------------------------------------------------------------


def _option_rank(candidate: Plan) -> int:
    if candidate.payment_option_id is None:
        return 0
    try:
        return int(candidate.payment_option_id.rsplit("_", 1)[-1])
    except ValueError:
        return 0


def rank_plans(
    candidates: list[Plan],
    req: PurchaseRequest,
) -> Optional[Plan]:
    if not candidates:
        return None
    best = min(
        candidates,
        key=lambda c: (
            0 if c.completion_date <= req.desired_completion_date else 1,  # 1. deadline
            c.total_paid,  # 2. minimize total payment cost (fee-aware, DEC-023)
            c.first_payment_date,  # 3. start earliest
            c.number_of_payments,  # 4. fewer payments
            len(c.spending_changes),  # 5. fewer spending changes
            _option_rank(c),  # 6. lowest payment_option_id
        ),
    )
    return best