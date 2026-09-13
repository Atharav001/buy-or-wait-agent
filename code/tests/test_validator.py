import datetime as dt
from decimal import Decimal
import models as M
import main


def _dummy_req(req_id="req_test", amount=Decimal("1000"), req_date=dt.date(2024, 6, 1), comp_date=dt.date(2024, 9, 1)):
    return M.PurchaseRequest(
        request_id=req_id,
        user_id="user_test",
        request_date=req_date,
        request_type="purchase",
        requested_amount=amount,
        desired_completion_date=comp_date,
        allows_partial_payment=True,
        request_text="Test purchase request",
    )


def test_valid_row():
    req = _dummy_req()
    row = {
        "request_id": req.request_id,
        "amount_safe_to_pay": "1000",
        "affordability_status": "affordable_now",
        "recommended_payment_method": "full_payment",
        "payment_plan": "2024-06-01:1000",
        "earliest_date_for_full_payment": "2024-06-01",
        "spending_changes_needed": "none",
        "decision_explanation": "Pay USD 1,000 today.",
    }
    valid, err = main.validate_row(row, req)
    assert valid is True
    assert err is None


def test_out_of_bounds_safe_amount():
    req = _dummy_req(amount=Decimal("500"))
    row = {
        "request_id": req.request_id,
        "amount_safe_to_pay": "600",  # > 500
        "affordability_status": "affordable_now",
        "recommended_payment_method": "full_payment",
        "payment_plan": "2024-06-01:500",
        "earliest_date_for_full_payment": "2024-06-01",
        "spending_changes_needed": "none",
        "decision_explanation": "Pay USD 500 today.",
    }
    valid, err = main.validate_row(row, req)
    assert valid is False
    assert "out of bounds" in err


def test_invalid_status_enum():
    req = _dummy_req()
    row = {
        "request_id": req.request_id,
        "amount_safe_to_pay": "500",
        "affordability_status": "partially_affordable",  # invalid enum
        "recommended_payment_method": "full_payment",
        "payment_plan": "2024-06-01:1000",
        "earliest_date_for_full_payment": "2024-06-01",
        "spending_changes_needed": "none",
        "decision_explanation": "Pay USD 1,000 today.",
    }
    valid, err = main.validate_row(row, req)
    assert valid is False
    assert "Invalid affordability_status" in err


def test_invalid_partial_payment_legs():
    req = _dummy_req(amount=Decimal("1000"))
    row = {
        "request_id": req.request_id,
        "amount_safe_to_pay": "400",
        "affordability_status": "affordable_with_plan",
        "recommended_payment_method": "partial_payment",
        "payment_plan": "2024-06-01:400|2024-07-01:500",  # sum is 900 != 1000
        "earliest_date_for_full_payment": "2024-07-01",
        "spending_changes_needed": "none",
        "decision_explanation": "Pay partial.",
    }
    valid, err = main.validate_row(row, req)
    assert valid is False
    assert "legs sum" in err


def test_non_chronological_plan():
    req = _dummy_req(amount=Decimal("1000"))
    row = {
        "request_id": req.request_id,
        "amount_safe_to_pay": "0",
        "affordability_status": "affordable_with_plan",
        "recommended_payment_method": "installments",
        "payment_plan": "2024-07-01:500|2024-06-01:500",  # inverted dates
        "earliest_date_for_full_payment": "2024-08-01",
        "spending_changes_needed": "none",
        "decision_explanation": "Installments.",
    }
    valid, err = main.validate_row(row, req)
    assert valid is False
    assert "not chronological" in err


def test_excess_or_duplicate_spending_changes():
    req = _dummy_req()
    row = {
        "request_id": req.request_id,
        "amount_safe_to_pay": "1000",
        "affordability_status": "affordable_with_plan",
        "recommended_payment_method": "full_payment",
        "payment_plan": "2024-06-01:1000",
        "earliest_date_for_full_payment": "2024-06-01",
        "spending_changes_needed": "stop:ev1|reduce_to:ev1:50",  # duplicate ev1
        "decision_explanation": "Spending changes.",
    }
    valid, err = main.validate_row(row, req)
    assert valid is False
    assert "Duplicate event_id" in err
