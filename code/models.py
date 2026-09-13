"""Core data contracts for Buy or Wait?.

Pydantic models at every LLM boundary and at the output.csv surface,
per brain/design.md section 4. Field names below are the verbatim columns
from the real dataset files (verified in Phase 0), not paraphrases.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Output surface (must match problem_statement.md byte-for-byte)
# ---------------------------------------------------------------------------

AffordabilityStatus = Literal[
    "affordable_now",
    "affordable_with_plan",
    "affordable_later",
    "not_affordable",
]

PaymentMethod = Literal[
    "full_payment",
    "partial_payment",
    "installments",
    "wait",
    "not_recommended",
]


class OutputRow(BaseModel):
    """One prediction row, written verbatim to output.csv in field order."""

    request_id: str
    amount_safe_to_pay: Decimal
    affordability_status: AffordabilityStatus
    recommended_payment_method: PaymentMethod
    payment_plan: str  # "YYYY-MM-DD:amount|..." or "none"
    earliest_date_for_full_payment: str  # "YYYY-MM-DD" or "" if never safe
    spending_changes_needed: str  # "stop:<event_id>|reduce_to:..." (<=3) or "none"
    decision_explanation: str

    _required_output_columns = (
        "request_id",
        "amount_safe_to_pay",
        "affordability_status",
        "recommended_payment_method",
        "payment_plan",
        "earliest_date_for_full_payment",
        "spending_changes_needed",
        "decision_explanation",
    )


# ---------------------------------------------------------------------------
# LLM boundary types (design.md section 3.3 / 4)
# ---------------------------------------------------------------------------


class ExtractedAmount(BaseModel):
    """A type-safe amount read off an image for a blank-amount event."""

    amount: Decimal
    currency: str
    confidence: float = Field(ge=0.0, le=1.0)
    source_image_id: str
    caveat: Optional[str] = None


ClaimFactKind = Literal[
    "amend_amount",
    "cancel_event",
    "confirm_event",
    "delay_event",
    "confirm_income",
]


class ClaimedFact(BaseModel):
    """A structured fact parsed from an untrusted message.

    The kind is a closed enum (DEC-011). No rule-altering fact type exists,
    so a message saying "ignore the minimum balance" has nowhere to land.
    """

    kind: ClaimFactKind
    target_event_id: Optional[str] = None
    payload: dict = {}
    source_message_id: str


class SpendingChange(BaseModel):
    """One spending-changes_needed action: stop or reduce_to."""

    action: Literal["stop", "reduce_to"]
    event_id: str
    new_amount: Optional[Decimal] = None  # only for reduce_to


class Plan(BaseModel):
    """A candidate payment plan. total_paid is fee-aware (DEC-023)."""

    method: PaymentMethod
    payment_option_id: Optional[str] = None
    legs: list[tuple[dt.date, Decimal]]
    spending_changes: list[SpendingChange] = []
    completion_date: dt.date
    total_paid: Decimal

    @property
    def requires_spending_changes(self) -> bool:
        return len(self.spending_changes) > 0

    @property
    def first_payment_date(self) -> dt.date:
        return self.legs[0][0]

    @property
    def number_of_payments(self) -> int:
        return len(self.legs)


# ---------------------------------------------------------------------------
# Domain records (typed views over the raw CSVs)
# ---------------------------------------------------------------------------


class EventStatus(BaseModel):
    """Validated literal set for financial_events.status."""

    value: Literal["settled", "pending", "scheduled", "cancelled", "failed", "unrealized"]


class FinancialEvent(BaseModel):
    """ModelConfig-free typed record; raw column names preserved.

    amount is Decimal | None: blank amounts are flagged for the extraction
    layer (DEC-003), never coerced to zero.
    """

    event_id: str
    user_id: str
    event_type: str
    description: str
    category: str
    direction: Literal["debit", "credit", "non_cash"]
    amount: Optional[Decimal]
    currency: str
    event_date: dt.date
    settlement_date: Optional[dt.date]
    status: Literal[
        "settled", "pending", "scheduled", "cancelled", "failed", "unrealized"
    ]
    linked_event_id: Optional[str] = None
    flexibility: Literal["fixed", "reducible", "stoppable", "reducible_or_stoppable"]
    minimum_allowed_amount: Optional[Decimal] = None

    @property
    def needs_image_extraction(self) -> bool:
        return self.amount is None


class FinancialProfile(BaseModel):
    """Typed financial_profiles row."""

    user_id: str
    home_currency: str
    current_available_balance: Decimal
    minimum_balance_to_keep: Decimal
    financial_priorities: list[str]
    expense_categories_to_protect: list[str]
    expense_categories_user_is_willing_to_reduce: list[str]
    expense_categories_user_is_willing_to_stop: list[str]
    payment_methods_user_will_consider: list[str]
    max_installment_months: Optional[int] = None


class PurchaseRequest(BaseModel):
    """Typed requests.csv row."""

    request_id: str
    user_id: str
    request_date: dt.date
    request_type: str
    requested_amount: Decimal
    desired_completion_date: dt.date
    allows_partial_payment: bool
    request_text: str


class PaymentOption(BaseModel):
    """Typed request_payment_options.csv row."""

    payment_option_id: str
    request_id: str
    payment_method: str
    payment_amount: Decimal
    number_of_payments: int
    first_payment_date: dt.date
    payment_frequency_days: Optional[int] = None
    financing_fee: Decimal
    total_payable_amount: Decimal


class MessageRow(BaseModel):
    """Typed messages.csv row. related_event_id blank => no one-to-one event."""

    message_id: str
    user_id: str
    request_id: Optional[str] = None
    related_event_id: Optional[str] = None
    sent_at: dt.datetime
    source_type: str
    message_text: str


class ImageRow(BaseModel):
    """Typed images.csv row, resolved to dataset/media/images/<image_id>.png."""

    image_id: str
    user_id: str
    request_id: Optional[str] = None
    related_event_id: Optional[str] = None


class ExchangeRate(BaseModel):
    """Typed exchange_rates.csv row (exact-date, stated-direction only, DEC-004)."""

    rate_date: dt.date
    from_currency: str
    to_currency: str
    rate: Decimal