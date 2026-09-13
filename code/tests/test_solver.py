"""End-to-end solver tests anchored on the golden sample request_01."""
import datetime as dt
from decimal import Decimal

from forecaster import simulate_balance
from ingest import load_all
from reconciler import reconcile
from solver import (
    compute_amount_safe_to_pay,
    compute_earliest_full_payment_date,
    generate_candidate_plans,
    plan_is_safe,
    rank_plans,
)
from conftest import DATASET


class TestSolverUser01:
    def setup_method(self):
        self.ds = load_all(DATASET, include_samples=True)
        self.ledger = reconcile(self.ds, "user_01", dt.date(2024, 3, 3))
        self.req = self.ds.requests_by_id["request_01"]
        self.flows = self.ledger.flows
        self.forecast = simulate_balance(self.ledger)

    def test_golden_amount_safe(self):
        safe = compute_amount_safe_to_pay(
            self.ledger, self.forecast, self.req.requested_amount
        )
        assert safe == Decimal("25256")

    def test_golden_earliest_full_payment(self):
        d = compute_earliest_full_payment_date(
            self.ledger, self.forecast, self.req.requested_amount
        )
        assert d == dt.date(2024, 3, 3)

    def test_golden_candidate_selection(self):
        candidates = generate_candidate_plans(self.ds, self.ledger, self.req, self.flows)
        best = rank_plans(candidates, self.req)
        assert best is not None
        assert best.method == "full_payment"
        assert best.legs == [(dt.date(2024, 3, 3), Decimal("25256"))]
        assert best.total_paid == Decimal("25256")
        assert best.spending_changes == []
        assert plan_is_safe(self.ledger, self.flows, best)

    def test_no_installments_for_user01(self):
        # request_01 offers installment options, but user_01 only considers
        # full_payment -> installments must never appear (preference gate).
        candidates = generate_candidate_plans(self.ds, self.ledger, self.req, self.flows)
        assert all(c.method != "installments" for c in candidates)