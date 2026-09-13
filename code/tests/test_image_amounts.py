"""Blank-amount image resolution (DEC-032/034) and the main.py wiring guard.

The gap: 16 financial-events rows carry an empty amount but have a receipt in
dataset/media/images/. Without a configured extraction provider the evaluated
run must stay deterministic and byte-identical, so resolve_image_amounts falls
back to ({}, None) and reconcile conservatively excludes unresolved blanks.
"""
import csv
import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

import config
import ingest as ING
import main as MAIN
from reconciler import reconcile

from conftest import DATASET


def _write(tmp: Path, name: str, cols: list[str], rows: list[list]) -> None:
    with open(tmp / name, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)


def _synthetic_dataset(tmp_path: Path) -> Path:
    tmp = tmp_path / "ds"
    tmp.mkdir()
    _write(
        tmp,
        "financial_profiles.csv",
        [
            "user_id",
            "home_currency",
            "current_available_balance",
            "minimum_balance_to_keep",
            "financial_priorities",
            "expense_categories_to_protect",
            "expense_categories_user_is_willing_to_reduce",
            "expense_categories_user_is_willing_to_stop",
            "payment_methods_user_will_consider",
            "max_installment_months",
        ],
        [["u", "ZAR", "5000", "1000", "savings", "", "dining", "subs", "full_payment", ""]],
    )
    rows = [
        # confirmed same-currency rent (explicit)
        [
            "e1",
            "u",
            "expense",
            "Rent March",
            "housing",
            "debit",
            "1500",
            "ZAR",
            "2024-03-01",
            "2024-03-01",
            "settled",
            "",
            "fixed",
            "",
        ],
        # blank-amount groceries receipt -> resolved from image (in EUR)
        [
            "e2",
            "u",
            "expense",
            "GROCERIES RECEIPT TILL 123",
            "groceries",
            "debit",
            "",
            "EUR",
            "2024-03-05",
            "2024-03-05",
            "settled",
            "",
            "reducible",
            "",
        ],
        # other blank never resolved -> must stay excluded
        [
            "e3",
            "u",
            "expense",
            "Mystery pending",
            "other",
            "debit",
            "",
            "ZAR",
            "2024-03-06",
            "2024-03-06",
            "pending",
            "",
            "fixed",
            "",
        ],
        # scheduled foreign-currency salary credit (DEC-034 fx to_home fix)
        [
            "e4",
            "u",
            "salary",
            "Next confirmed salary credit",
            "salary",
            "credit",
            "1000",
            "EUR",
            "2024-03-10",
            "2024-03-10",
            "scheduled",
            "",
            "fixed",
            "",
        ],
    ]
    _write(
        tmp,
        "financial_events.csv",
        [
            "event_id",
            "user_id",
            "event_type",
            "description",
            "category",
            "direction",
            "amount",
            "currency",
            "event_date",
            "settlement_date",
            "status",
            "linked_event_id",
            "flexibility",
            "minimum_allowed_amount",
        ],
        rows,
    )
    # EUR->ZAR exact-date rate for 2024-03-05 (from->to direction stated).
    _write(
        tmp,
        "exchange_rates.csv",
        ["rate_date", "from_currency", "to_currency", "rate"],
        [
            ["2024-03-05", "EUR", "ZAR", "20.5"],
            ["2024-03-10", "EUR", "ZAR", "20.5"],
        ],
    )
    _write(
        tmp,
        "requests.csv",
        [
            "request_id",
            "user_id",
            "request_date",
            "request_type",
            "requested_amount",
            "desired_completion_date",
            "allows_partial_payment",
            "request_text",
        ],
        [["request_test", "u", "2024-03-01", "purchase", "2000", "2024-03-31", "true", "x"]],
    )
    _write(
        tmp,
        "request_payment_options.csv",
        [
            "payment_option_id",
            "request_id",
            "payment_method",
            "payment_amount",
            "number_of_payments",
            "first_payment_date",
            "payment_frequency_days",
            "financing_fee",
            "total_payable_amount",
        ],
        [["po1", "request_test", "full_payment", "2000", "1", "2024-03-01", "0", "0", "2000"]],
    )
    for name, cols, row in (
        (
            "messages.csv",
            ["message_id", "user_id", "request_id", "related_event_id", "sent_at", "source_type", "message_text"],
            ["msg1", "u", "request_test", "", "2024-03-01 10:00:00", "message", "note"],
        ),
        (
            "images.csv",
            ["image_id", "user_id", "request_id", "related_event_id"],
            ["image_gg", "u", "request_test", "e2"],
        ),
    ):
        _write(tmp, name, cols, [row])
    return tmp


class TestImageAmountsInReconcile:
    def test_resolved_blank_is_included_in_home_currency(self, tmp_path):
        ds = ING.load_all(_synthetic_dataset(tmp_path))
        without = reconcile(ds, "u", dt.date(2024, 3, 1), horizon_days=30)
        assert without.flows.get(dt.date(2024, 3, 5)) is None  # blank excluded

        with_ = reconcile(
            ds,
            "u",
            dt.date(2024, 3, 1),
            horizon_days=30,
            image_amounts={"e2": Decimal("2050")},
        )
        # EUR 2050 -> ZAR at the exact 2024-03-05 rate 20.5 = 42025 (DEC-034 fx fix)
        assert with_.flows.get(dt.date(2024, 3, 5), Decimal("0")) == Decimal("-42025")
        assert any("image-resolved" in a for a in with_.audit)

    def test_unresolved_blank_still_excluded(self, tmp_path):
        ds = ING.load_all(_synthetic_dataset(tmp_path))
        ledger = reconcile(
            ds,
            "u",
            dt.date(2024, 3, 1),
            horizon_days=30,
            image_amounts={"e2": Decimal("2050")},  # e3 has no image amount
        )
        assert dt.date(2024, 3, 6) not in ledger.flows

    def test_image_amounts_ignored_when_dict_empty(self, tmp_path):
        ds = ING.load_all(_synthetic_dataset(tmp_path))
        ledger = reconcile(ds, "u", dt.date(2024, 3, 1), horizon_days=30,
                           image_amounts={})
        assert dt.date(2024, 3, 5) not in ledger.flows

    def test_scheduled_foreign_salary_credit_counts(self, tmp_path):
        ds = ING.load_all(_synthetic_dataset(tmp_path))
        ledger = reconcile(ds, "u", dt.date(2024, 3, 1), horizon_days=30)
        # EUR 1000 * 20.5 (2024-03-10) = 20500 positive flow (DEC-034 fx fix:
        # an exact-date ISO key, not a raw date, reaches the string-keyed rates).
        assert ledger.flows.get(dt.date(2024, 3, 10), Decimal("0")) == Decimal("20500")


@pytest.mark.parametrize("provider", [None])
def test_resolve_image_amounts_requires_provider(monkeypatch, provider):
    monkeypatch.setattr(config, "EXTRACTION_PROVIDER", provider)
    amounts, engine = MAIN.resolve_image_amounts(ING.load_all(DATASET, include_samples=True))
    assert amounts == {}
    assert engine is None