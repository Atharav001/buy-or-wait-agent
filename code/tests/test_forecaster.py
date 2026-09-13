"""Tests for the daily balance simulation primitive."""
import datetime as dt
from decimal import Decimal

from forecaster import DailyBalanceSeries, simulate_balance
from reconciler import ReconciledLedger
from models import FinancialProfile


def _ledger():
    prof = FinancialProfile(
        user_id="u",
        home_currency="ZAR",
        current_available_balance=Decimal("1000"),
        minimum_balance_to_keep=Decimal("100"),
        financial_priorities=[],
        expense_categories_to_protect=[],
        expense_categories_user_is_willing_to_reduce=[],
        expense_categories_user_is_willing_to_stop=[],
        payment_methods_user_will_consider=["full_payment"],
    )
    return ReconciledLedger(
        user_id="u",
        request_date=dt.date(2024, 1, 1),
        horizon_end=dt.date(2024, 1, 10),
        profile=prof,
        start_balance=Decimal("1000"),
        flows={
            dt.date(2024, 1, 5): Decimal("-300"),
            dt.date(2024, 1, 8): Decimal("500"),
        },
    )


class TestSimulateBalance:
    def test_closing_balances(self):
        series = simulate_balance(_ledger())
        assert series.balance_on(dt.date(2024, 1, 4)) == Decimal("1000")
        assert series.balance_on(dt.date(2024, 1, 5)) == Decimal("700")
        assert series.balance_on(dt.date(2024, 1, 8)) == Decimal("1200")

    def test_request_date_flow_applies_same_day(self):
        flows = {dt.date(2024, 1, 1): Decimal("-400")}
        l = _ledger()
        l.flows = flows
        series = simulate_balance(l)
        assert series.balance_on(dt.date(2024, 1, 1)) == Decimal("600")

    def test_min_balance_between_inclusive(self):
        series = simulate_balance(_ledger())
        assert series.min_balance_between(dt.date(2024, 1, 5), dt.date(2024, 1, 7)) == Decimal("700")
        assert series.min_balance_between(dt.date(2024, 1, 1), dt.date(2024, 1, 10)) == Decimal("700")

    def test_extra_flows_merged(self):
        series = simulate_balance(_ledger(), [(dt.date(2024, 1, 2), Decimal("-50"))])
        assert series.balance_on(dt.date(2024, 1, 2)) == Decimal("950")


class TestDailyBalanceSeries:
    def test_empty_range_returns_zero(self):
        series = DailyBalanceSeries(dt.date(2024, 1, 1), dt.date(2024, 1, 1), {})
        assert series.min_balance_between(dt.date(2024, 1, 2), dt.date(2024, 1, 3)) == Decimal("0")