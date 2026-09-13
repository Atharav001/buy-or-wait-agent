"""ingest: load every input CSV into typed structures. Fail loud, never coerce.

Implements DEC-003: missing/mistyped columns raise; blank event amounts are
flagged for the extraction layer (needs_image_extraction), never zeroed.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable

from models import (
    ExchangeRate,
    FinancialEvent,
    FinancialProfile,
    ImageRow,
    MessageRow,
    PaymentOption,
    PurchaseRequest,
)

# required column sets, byte-verified against the real files (Phase 0)
_REQUIRED = {
    "financial_profiles.csv": {
        "user_id", "home_currency", "current_available_balance",
        "minimum_balance_to_keep", "financial_priorities",
        "expense_categories_to_protect", "expense_categories_user_is_willing_to_reduce",
        "expense_categories_user_is_willing_to_stop",
        "payment_methods_user_will_consider", "max_installment_months",
    },
    "financial_events.csv": {
        "event_id", "user_id", "event_type", "description", "category", "direction",
        "amount", "currency", "event_date", "settlement_date", "status",
        "linked_event_id", "flexibility", "minimum_allowed_amount",
    },
    "requests.csv": {
        "request_id", "user_id", "request_date", "request_type",
        "requested_amount", "desired_completion_date", "allows_partial_payment",
        "request_text",
    },
    # sample_requests.csv carries the same request columns plus golden outputs
    "sample_requests.csv": {
        "request_id", "user_id", "request_date", "request_type",
        "requested_amount", "desired_completion_date", "allows_partial_payment",
        "request_text",
    },
    "request_payment_options.csv": {
        "payment_option_id", "request_id", "payment_method", "payment_amount",
        "number_of_payments", "first_payment_date", "payment_frequency_days",
        "financing_fee", "total_payable_amount",
    },
    "messages.csv": {
        "message_id", "user_id", "request_id", "related_event_id",
        "sent_at", "source_type", "message_text",
    },
    "images.csv": {"image_id", "user_id", "request_id", "related_event_id"},
    "exchange_rates.csv": {"rate_date", "from_currency", "to_currency", "rate"},
}


class IngestError(Exception):
    """Raised on any schema-level failure. Never silently coerced."""


def _read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _check_schema(path: Path, rows: list[dict]) -> None:
    if not rows:
        raise IngestError(f"{path.name}: no data rows")
    have = set(rows[0].keys())
    need = _REQUIRED[path.name]
    missing = need - have
    if missing:
        raise IngestError(f"{path.name}: missing columns {sorted(missing)}")


def _dec(v: str | None) -> Decimal | None:
    if v is None or not str(v).strip():
        return None
    try:
        return Decimal(str(v).strip())
    except InvalidOperation:
        raise IngestError(f"non-numeric amount value: {v!r}")


def _date(v: str | None) -> str | None:
    if v is None or not str(v).strip():
        return None
    return str(v).strip()  # YYYY-MM-DD; typed conversion happens in models


def _split(v: str | None) -> list[str]:
    if not v:
        return []
    return [p.strip() for p in v.split("|") if p.strip()]


def _int_or_none(v: str | None) -> int | None:
    if v is None or not str(v).strip():
        return None
    return int(str(v).strip())


def _bool(v: str | None) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes")


@dataclass
class RawDataset:
    dataset_dir: Path
    profiles: dict[str, FinancialProfile] = field(default_factory=dict)
    events: list[FinancialEvent] = field(default_factory=list)
    requests: list[PurchaseRequest] = field(default_factory=list)
    requests_by_id: dict[str, PurchaseRequest] = field(default_factory=dict)
    payment_options: dict[str, list[PaymentOption]] = field(default_factory=dict)
    messages: list[MessageRow] = field(default_factory=list)
    images: list[ImageRow] = field(default_factory=list)
    exchange_rates: dict[tuple[str, str], dict[str, Decimal]] = field(default_factory=dict)

    events_by_user: dict[str, list[FinancialEvent]] = field(default_factory=dict)
    messages_by_user: dict[str, list[MessageRow]] = field(default_factory=dict)
    images_by_user: dict[str, list[ImageRow]] = field(default_factory=dict)

    @property
    def blank_amount_events(self) -> list[FinancialEvent]:
        return [e for e in self.events if e.needs_image_extraction]


_ROWS: dict[str, Callable[[dict], list]] = {}


def load_all(dataset_dir: str | Path, include_samples: bool = False) -> RawDataset:
    """Load every required CSV from dataset_dir into a typed RawDataset.

    include_samples merges sample_requests.csv into requests/requests_by_id so
    calibration and golden tests can run against the 25 public examples. The
    final output run only iterates requests.csv ids, so this is risk-free.
    """
    base = Path(dataset_dir)
    req_rows = _read_csv(base / "requests.csv")
    _check_schema(base / "requests.csv", req_rows)

    ds = RawDataset(dataset_dir=base)

    # financial_profiles
    rows = _read_csv(base / "financial_profiles.csv")
    _check_schema(base / "financial_profiles.csv", rows)
    for r in rows:
        p = FinancialProfile(
            user_id=r["user_id"],
            home_currency=r["home_currency"],
            current_available_balance=_dec(r["current_available_balance"]),
            minimum_balance_to_keep=_dec(r["minimum_balance_to_keep"]),
            financial_priorities=_split(r["financial_priorities"]),
            expense_categories_to_protect=_split(r["expense_categories_to_protect"]),
            expense_categories_user_is_willing_to_reduce=_split(
                r["expense_categories_user_is_willing_to_reduce"]
            ),
            expense_categories_user_is_willing_to_stop=_split(
                r["expense_categories_user_is_willing_to_stop"]
            ),
            payment_methods_user_will_consider=_split(
                r["payment_methods_user_will_consider"]
            ),
            max_installment_months=_int_or_none(r["max_installment_months"]),
        )
        ds.profiles[p.user_id] = p

    # financial_events  (25k rows)
    rows = _read_csv(base / "financial_events.csv")
    _check_schema(base / "financial_events.csv", rows)
    for r in rows:
        e = FinancialEvent(
            event_id=r["event_id"],
            user_id=r["user_id"],
            event_type=r["event_type"],
            description=r["description"],
            category=r["category"],
            direction=r["direction"],
            amount=_dec(r["amount"]),
            currency=r["currency"],
            event_date=_date(r["event_date"]),
            settlement_date=_date(r["settlement_date"]),
            status=r["status"],
            linked_event_id=r["linked_event_id"] or None,
            flexibility=r["flexibility"],
            minimum_allowed_amount=_dec(r["minimum_allowed_amount"]),
        )
        ds.events.append(e)
        ds.events_by_user.setdefault(e.user_id, []).append(e)

    # requests
    for r in req_rows:
        req = PurchaseRequest(
            request_id=r["request_id"],
            user_id=r["user_id"],
            request_date=_date(r["request_date"]),
            request_type=r["request_type"],
            requested_amount=_dec(r["requested_amount"]),
            desired_completion_date=_date(r["desired_completion_date"]),
            allows_partial_payment=_bool(r["allows_partial_payment"]),
            request_text=r["request_text"],
        )
        ds.requests.append(req)
        ds.requests_by_id[req.request_id] = req

    # sample_requests.csv (golden examples, optional for calibration)
    if include_samples:
        sample_rows = _read_csv(base / "sample_requests.csv")
        _check_schema(base / "sample_requests.csv", sample_rows)
        for r in sample_rows:
            req = PurchaseRequest(
                request_id=r["request_id"],
                user_id=r["user_id"],
                request_date=_date(r["request_date"]),
                request_type=r["request_type"],
                requested_amount=_dec(r["requested_amount"]),
                desired_completion_date=_date(r["desired_completion_date"]),
                allows_partial_payment=_bool(r["allows_partial_payment"]),
                request_text=r["request_text"],
            )
            ds.requests.append(req)
            ds.requests_by_id[req.request_id] = req

    # request_payment_options
    rows = _read_csv(base / "request_payment_options.csv")
    _check_schema(base / "request_payment_options.csv", rows)
    for r in rows:
        po = PaymentOption(
            payment_option_id=r["payment_option_id"],
            request_id=r["request_id"],
            payment_method=r["payment_method"],
            payment_amount=_dec(r["payment_amount"]),
            number_of_payments=int(r["number_of_payments"]),
            first_payment_date=_date(r["first_payment_date"]),
            payment_frequency_days=_int_or_none(r["payment_frequency_days"]),
            financing_fee=_dec(r["financing_fee"]),
            total_payable_amount=_dec(r["total_payable_amount"]),
        )
        ds.payment_options.setdefault(po.request_id, []).append(po)

    # messages
    rows = _read_csv(base / "messages.csv")
    _check_schema(base / "messages.csv", rows)
    for r in rows:
        m = MessageRow(
            message_id=r["message_id"],
            user_id=r["user_id"],
            request_id=r["request_id"] or None,
            related_event_id=r["related_event_id"] or None,
            sent_at=r["sent_at"],
            source_type=r["source_type"],
            message_text=r["message_text"],
        )
        ds.messages.append(m)
        ds.messages_by_user.setdefault(m.user_id, []).append(m)

    # images
    rows = _read_csv(base / "images.csv")
    _check_schema(base / "images.csv", rows)
    for r in rows:
        im = ImageRow(
            image_id=r["image_id"],
            user_id=r["user_id"],
            request_id=r["request_id"] or None,
            related_event_id=r["related_event_id"] or None,
        )
        ds.images.append(im)
        ds.images_by_user.setdefault(im.user_id, []).append(im)

    # exchange_rates -> {(from, to): {date: rate}}  exact-date, stated-direction
    rows = _read_csv(base / "exchange_rates.csv")
    _check_schema(base / "exchange_rates.csv", rows)
    for r in rows:
        ds.exchange_rates.setdefault(
            (r["from_currency"], r["to_currency"]),
            {},
        )[_date(r["rate_date"])] = _dec(r["rate"])

    return ds


def image_path(ds: RawDataset, image_id: str) -> Path:
    p = ds.dataset_dir / "media" / "images" / f"{image_id}.png"
    if not p.exists():
        raise IngestError(f"image file missing: {p}")
    return p