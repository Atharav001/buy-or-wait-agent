"""Currency conversion tests (design.md section 3.2, DEC-004).

Fixture rates are lifted from the real exchange_rates.csv where possible so
the cross-currency path is tested against a genuine dataset row.
"""
from decimal import Decimal

import pytest

from currency import MissingRateError, convert

# real rows from dataset/exchange_rates.csv
RATES = {
    ("EUR", "USD"): {"2023-10-15": Decimal("1.08"), "2024-03-03": Decimal("1.10")},
    ("USD", "IDR"): {"2024-03-03": Decimal("15833.33")},
    ("EUR", "ZAR"): {"2023-10-15": Decimal("20.00")},
}


def test_same_currency_passthrough():
    assert convert(Decimal("123.45"), "USD", "USD", "2024-03-03", RATES) == Decimal(
        "123.45"
    )


def test_cross_currency_exact_date_real_rate():
    assert convert(Decimal("100"), "EUR", "USD", "2023-10-15", RATES) == Decimal(
        "108.00"
    )


def test_cross_currency_matches_preset_value():
    assert convert(Decimal("2"), "USD", "IDR", "2024-03-03", RATES) == Decimal(
        "31666.66"
    )


def test_missing_rate_for_that_date_raises():
    with pytest.raises(MissingRateError):
        convert(Decimal("10"), "EUR", "USD", "2030-01-01", RATES)


def test_missing_direction_raises_even_if_reverse_exists():
    with pytest.raises(MissingRateError):
        convert(Decimal("10"), "IDR", "USD", "2024-03-03", RATES)