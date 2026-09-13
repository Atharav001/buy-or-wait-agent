"""currency: exact-date, stated-direction conversion (DEC-004).

convert() never interpolates or approximates a rate. Missing rate for the
exact date -> MissingRateError; callers exclude the event from safe-cash math
(the safer interpretation per DEC-006), never assume 0 or 1:1.

Verified in Phase 0: all 139 cross-currency event rows resolve via a direct
(from=event currency, to=home currency) row on the settlement date. No
inversion or nearest-date fallback is needed or permitted.
"""
from __future__ import annotations

from decimal import Decimal


class MissingRateError(Exception):
    """No exact-date rate exists for the requested (from, to, date)."""


def convert(
    amount: Decimal,
    from_ccy: str,
    to_ccy: str,
    on_date: str,
    rates: dict[tuple[str, str], dict[str, Decimal]],
) -> Decimal:
    """Convert amount at the exact-date rate row (stated direction only).

    rates is the {(from, to): {date: rate}} map produced by ingest.
    """
    if from_ccy == to_ccy:
        return amount
    date_map = rates.get((from_ccy, to_ccy))
    if date_map is None or on_date not in date_map:
        raise MissingRateError(
            f"no exact-date rate {from_ccy}->{to_ccy} on {on_date}"
        )
    return (amount * date_map[on_date]).quantize(Decimal("0.01"))