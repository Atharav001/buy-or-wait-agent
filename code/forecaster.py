"""forecaster: event-driven daily balance simulation (DEC-009).

simulate_balance applies every net flow on its actual date and produces a full
daily series. The solver queries "min balance between date A and B" repeatedly
via min_balance_between — this is the primitive the 90-day safety check uses to
prove (not approximate) that a plan never breaches minimum_balance_to_keep.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Iterable, Optional

from reconciler import ReconciledLedger


class DailyBalanceSeries:
    """Daily closing balance for request_date .. horizon_end (inclusive)."""

    def __init__(self, start: dt.date, end: dt.date, balances: dict[dt.date, Decimal]):
        self.start = start
        self.end = end
        self.balances = balances

    def balance_on(self, d: dt.date) -> Decimal:
        return self.balances.get(d, Decimal("0"))

    def min_balance_between(self, a: dt.date, b: dt.date) -> Decimal:
        """Minimum closing balance over every day in [a, b] inclusive."""
        cur = Decimal("0")
        n = 0
        day = max(a, self.start)
        last = min(b, self.end)
        while day <= last:
            v = self.balances.get(day, Decimal("0"))
            if n == 0 or v < cur:
                cur = v
                n = 1
            day += dt.timedelta(days=1)
        return cur if n else Decimal("0")

    def min_day_between(self, a: dt.date, b: dt.date) -> Optional[dt.date]:
        """Date inside [a, b] inclusive where the minimum closing balance occurs."""
        best_v: Optional[Decimal] = None
        best_d: Optional[dt.date] = None
        day = max(a, self.start)
        last = min(b, self.end)
        while day <= last:
            v = self.balances.get(day, Decimal("0"))
            if best_v is None or v < best_v:
                best_v = v
                best_d = day
            day += dt.timedelta(days=1)
        return best_d


def _series(ledger: ReconciledLedger, merged: dict[dt.date, Decimal]) -> DailyBalanceSeries:
    day = ledger.request_date
    balance = ledger.start_balance
    balances: dict[dt.date, Decimal] = {}
    while day <= ledger.horizon_end:
        if day in merged:
            balance += merged[day]
        balances[day] = balance
        day += dt.timedelta(days=1)
    return DailyBalanceSeries(ledger.request_date, ledger.horizon_end, balances)


def simulate_balance(
    ledger: ReconciledLedger,
    extra_flows: Iterable[tuple[dt.date, Decimal]] = (),
) -> DailyBalanceSeries:
    """Simulate ledger.flows with `extra_flows` DELTAS applied on top.

    The start balance is the profile's current_available_balance (as of
    request_date). Flows on request_date itself are applied before the day's
    closing balance is recorded. Only pass deltas here (e.g. a plan's payment
    legs); for a complete alternative flow map use simulate_from.
    """
    merged = dict(ledger.flows)
    for d, delta in extra_flows:
        merged[d] = merged.get(d, Decimal("0")) + delta
    return _series(ledger, merged)


def simulate_from(
    ledger: ReconciledLedger,
    flows: Iterable[tuple[dt.date, Decimal]],
) -> DailyBalanceSeries:
    """Simulate from a COMPLETE flow map (replaces ledger.flows wholesale).

    Used by the solver for spending-change scenarios and plan legs, where the
    caller already assembled the final per-day map. Never double-applies.
    """
    merged: dict[dt.date, Decimal] = {}
    for d, delta in flows:
        merged[d] = merged.get(d, Decimal("0")) + delta
    return _series(ledger, merged)