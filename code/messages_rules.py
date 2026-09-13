"""Deterministic rules-based parser for dataset/messages.csv (no LLM).

The message corpus is a fixed set of template families (employer payroll
updates, service-provider payout/invoice notices, bank/merchant updates,
financial-service prize/investment notices). Every template maps to a typed
:class:`MessageFact` that the reconciler consumes as *evidence* (stream
amount/date overrides, continuation gates, exclusions). No embedded
instruction ever alters a rule; numbers land only as data (design.md
DEC-011 / DEC-025).

All matching is keyword + regex driven so results are reproducible and free.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Optional

CURRENCY = r"(IDR|INR|USD|EUR|ZAR|JPY|GBP)"
AMT = r"((?:\d[\d,]*)(?:\.\d+)?)"
DATE_ISO = r"(\d{4})-(\d{2})-(\d{2})"
MONTH_DAY = (
    r"(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|"
    r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+(\d{4})"
)
MONTH_NUM = {m: i + 1 for i, m in enumerate(
    "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split())
}


def _deci(raw: Optional[str]) -> Optional[Decimal]:
    if not raw:
        return None
    try:
        return Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None


def _date(raw: Optional[str]) -> Optional[dt.date]:
    if not raw:
        return None
    raw = raw.strip()
    m = re.search(DATE_ISO, raw)
    if m:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(MONTH_DAY, raw)
    if m:
        return dt.date(int(m.group(3)), MONTH_NUM[m.group(2).title()], int(m.group(1)))
    return None


@dataclass
class MessageFact:
    """One semantic fact from one message, resolving template structure.

    kind is a closed set consumed by the reconciler. amount/currency/date
    are the message's facts (amount in the message's currency); reconciler
    converts to home currency at the relevant settlement date.
    """

    kind: str
    user_id: str
    message_id: str
    sent_at: dt.datetime
    amount: Optional[Decimal] = None
    currency: Optional[str] = None
    date: Optional[dt.date] = None
    related_event_id: Optional[str] = None
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Template matchers. `text` is always lowercased for detection.
# ---------------------------------------------------------------------------

def _debits_fail(t: str) -> bool:
    return (
        "previous debit attempt failed" in t
        or "percobaan debit sebelumnya gagal" in t
        or "the bill is still outstanding" in t
        or "tagihan masih belum dibayar" in t
    )


def _extra_charge(t: str) -> bool:
    return (
        "extra card charge" in t or "tagihan kartu tambahan" in t
        or "reversal has not been posted" in t
        or "pembalikannya belum tercatat" in t
    )


def _prize_scam(t: str) -> bool:
    return ("release charge" in t or "biaya pencairan" in t
            or "processing charge" in t or "biaya pemrosesan" in t)


def _prize_pending(t: str) -> bool:
    return (
        ("prize" in t or "hadiah" in t or "claim" in t or "klaim" in t)
        and (
            "verified and is still in payment processing" in t
            or "sudah diverifikasi dan masih dalam proses pembayaran" in t
            or "has not been credited to your account yet" in t
            or "belum masuk ke rekening anda" in t
        )
        and not _prize_scam(t)
    )


def _prize_credited(t: str) -> bool:
    return (
        ("prize proceeds have reached" in t or "dana hadiah sudah masuk" in t
         or "proceeds have reached your account" in t)
        and not _prize_pending(t)
    )


def _claims_desk_reached(t: str) -> bool:
    return "prize proceeds" in t or "dana hadiah" in t


_PAYOUT_COMPANIES = ("quickcrew", "taskloop", "task sprint", "workdash",
                     "shiftpay", "tasksprint", "ridegrid")


def _bonus_pending(t: str) -> bool:
    return (
        "quarterly bonus is still subject" in t
        or "bonus kuartalan anda masih menunggu" in t
        or "the final amount and payment date have not been approved" in t
        or "jumlah akhir dan tanggal pembayaran belum disetujui" in t
    )


def _order_paid(t: str) -> bool:
    return (
        "was paid in" in t and "receipt has the final" in t
        or "order was paid in" in t
    )


def _property_receipt(t: str) -> bool:
    return (
        "property maintenance payment was received" in t
        or "receipt has the final" in t and "due date" in t
    )


def _bill_fx(t: str) -> bool:
    return (
        "bill was charged in a foreign currency" in t
        or "tagihan dikenakan dalam mata uang asing" in t
        or "charged in a foreign currency" in t
    )


def _payout_pending(t: str) -> bool:
    return (
        "payout is still pending" in t
        or "pembayarannya masih tertunda" in t
        or "belum dapat ditarik" in t
        or ("isn't withdrawable" in t or "is not withdrawable" in t)
        or any(c in t for c in _PAYOUT_COMPANIES) and "payout" in t and "pending" in t
    )


def _invoice_approved(t: str) -> bool:
    return (
        "client approved an invoice payment" in t
        or "klien menyetujui pembayaran faktur" in t
        or "client approved an invoice payment of" in t
        or "pembayaran faktur" in t
    ) and not _payout_pending(t)


def _refund_fx(t: str) -> bool:
    return (
        "foreign-currency refund is still processing" in t
        or "pengembalian dana mata uang asing" in t
        or "refund is still processing" in t
    )


def _refund_pending(t: str) -> bool:
    return (
        ("refund has been initiated" in t or "pengembalian dana sudah diproses" in t
         or "refund has been initiated but has not reached" in t
         or "belum masuk ke rekening anda" in t)
        and not _refund_fx(t)
    )


def _investment_value(t: str) -> bool:
    return (
        "no units have been sold" in t
        or "no cash proceeds have been generated" in t
        or "belum dijual dan tidak ada transaksi tunai" in t
        or "the holding has not been sold" in t
        or "no cash transaction" in t
        or "displayed market value" in t
        or "nilai investasi yang ditampilkan" in t
    )


def _sale_settled(t: str) -> bool:
    return (
        "proceeds from your investment sale have settled" in t
        or "hasil penjualan investasi anda sudah masuk" in t
        or "settled in the cash account" in t
    )


def _transfer_internal(t: str) -> bool:
    return (
        "transfer between your two accounts" in t
        or "transfer antara dua rekening anda" in t
        or "debit and credit came from a transfer" in t
    )


def _rent_increase(t: str) -> bool:
    return (
        "renewed lease increases monthly rent by 12%" in t
        or "perpanjangan sewa menaikkan biaya sewa" in t
        or "sewa bulanan sebesar 12%" in t
        or "increases monthly rent by 12%" in t
    )


def _seasonal_end(t: str) -> bool:
    return (
        "seasonal contract has ended" in t
        or "kontrak musiman saat ini telah berakhir" in t
        or "no off-season income" in t
    )


def _employment_end(t: str) -> bool:
    return (
        ("your employment has ended" in t or "hubungan kerja anda telah berakhir" in t)
        and ("no regular salary payments" in t
             or "tidak ada pembayaran gaji rutin" in t)
    )


def _household_reduce(t: str) -> bool:
    return (
        "household employment record has ended" in t
        or "salah satu sumber pendapatan kerja rumah tangga telah berakhir" in t
        or "sisa gaji bulanan" in t
        or "remaining confirmed monthly salary" in t
    )


def _salary_increase(t: str) -> bool:
    return (
        "salary has increased to" in t
        or "salary naik menjadi" in t
        or "gaji bulanan anda naik menjadi" in t
        or "gaji bulanan anda meningkat menjadi" in t
        or "monthly salary has increased" in t
    )


def _salary_reduce_next(t: str) -> bool:
    return (
        "next salary is reduced to" in t
        or "gaji berikutnya berkurang menjadi" in t
        or "salary is reduced" in t
    )


def _salary_temp(t: str) -> bool:
    return (
        "temporary monthly pay is" in t
        or "temporary pay" in t
        or "gaji bulanan sementara anda" in t
        or "jumlah yang lebih rendah masih berlaku" in t
        or "reduced amount continues" in t
    )


def _salary_resume(t: str) -> bool:
    return (
        "regular salary of" in t and "resumes on" in t
        or "gaji rutin" in t and "dilanjutkan" in t
        or ("resumes on" in t and "rubuh" in t)
    )


def _salary_date_change(t: str) -> bool:
    return (
        "confirmed salary is now expected on" in t
        or "gaji yang sudah dikonfirmasi kini diperkirakan masuk pada" in t
        or "replaces the payroll date" in t
        or "tanggal ini menggantikan tanggal penggajian" in t
    )


def _first_salary(t: str) -> bool:
    return (
        "first salary will be" in t
        or "first salary will be" in t
        or "gaji pertama anda" in t
        or "gaji pertama dari perusahaan baru" in t
        or "first salary from the new employer" in t
        or "first salary of" in t
    ) or _salary_templates_first(t)


def _salary_templates_first(t: str) -> bool:
    return (
        "first salary" in t
        or "gaji pertama" in t
        or ("salary of" in t and "confirmed for" in t)
    )


def _salary_confirmed_fx(t: str) -> bool:
    return (
        "salary of" in t and "confirmed for" in t and "convert" in t
        or "converted using the rate applied on the settlement date" in t
        or "mengonversinya dengan kurs pada tanggal penyelesaian" in t
    )


def _salary_base_only(t: str) -> bool:
    return (
        "confirmed base salary" in t
        or "gaji pokok yang dikonfirmasi" in t
        or "base salary is" in t
        or "commission shown for open deals" in t
        or "komisi dari transaksi yang masih berjalan" in t
    )


def _salary_regular_arrears(t: str) -> bool:
    return (
        "regular salary for the next payroll" in t
        or "gaji rutin untuk penggajian berikutnya" in t
        or "one-time arrears adjustment" in t
        or "penyesuaian tunggakan satu kali" in t
        or "penyesuaian satu kali" in t
    )


def _salary_routine_confirmed(t: str) -> bool:
    return (
        "slip gaji berikutnya akan menampilkan" in t
        or "untuk penggajian berikutnya sudah dikonfirmasi" in t
        or "regular salary is scheduled" in t
    )


def _salary_confirmed_credit(t: str) -> bool:
    return (
        "employer has confirmed a" in t and "salary credit" in t
        or "confirmed a" in t and "salary credit for" in t
    )


def _reimbursement(t: str) -> bool:
    return (
        "reimbursement for your earlier work expense" in t
        or "penggantian atas biaya kerja anda sebelumnya" in t
    )


def _min_two_cards(t: str) -> bool:
    return "minimum payments due on two separate card" in t


def _salary_scheduled(t: str) -> bool:
    return (
        "scheduled for" in t and "payroll" in t
        or "dijadwalkan pada" in t and "gaji" in t
        or "approved and sent it for processing" in t
    )


# ---------------------------------------------------------------------------
# Builder: turn a matched message into a MessageFact
# ---------------------------------------------------------------------------

def _parse_amount(t: str) -> tuple[Optional[Decimal], Optional[str]]:
    m = re.search(CURRENCY + r"\s*" + AMT, t, flags=re.IGNORECASE)
    if not m:
        return None, None
    ccy = m.group(1).upper()
    return _deci(m.group(2)), ccy


def _parse_date_any(t: str) -> Optional[dt.date]:
    m = re.search(DATE_ISO, t)
    if m:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(MONTH_DAY, t)
    if m:
        return dt.date(int(m.group(3)), MONTH_NUM[m.group(2).title()], int(m.group(1)))
    return None


def _sent(row) -> dt.datetime:
    try:
        return dt.datetime.fromisoformat(row["sent_at"].replace("Z", "+00:00"))
    except Exception:
        return dt.datetime(2020, 1, 1)


def _fact(row, kind, ref_pattern=None) -> MessageFact:
    text = row["message_text"]
    amt, ccy = _parse_amount(text)
    d = _parse_date_any(text)
    fact = MessageFact(
        kind=kind,
        user_id=row["user_id"],
        message_id=row["message_id"],
        sent_at=_sent(row),
        amount=amt,
        currency=ccy,
        date=d,
        related_event_id=row.get("related_event_id") or None,
    )
    if ref_pattern and re.search(ref_pattern, row["message_id"]):
        pass
    fact.extra["source_type"] = row.get("source_type", "")
    return fact


_DECISION_DATE_HINTS = (
    "applies from", "berlaku mulai", "confirmed credit date",
    "tanggal kredit yang dikonfirmasi", "settlement is expected on",
    "penyelesaian diperkirakan pada", "resumes on", "dilanjutkan pada",
    "expected on", "diperkirakan masuk pada", "confirmed for",
    "dikonfirmasi untuk", "credit date is", "tanggalkredit",
    "for 15 september", "scheduled for", "dijadwalkan pada",
)


def _date_for_fact(text: str) -> Optional[dt.date]:
    """Pick the decision date: prefer the template's anchored date phrase."""
    for hint in _DECISION_DATE_HINTS:
        idx = text.lower().find(hint)
        if idx < 0:
            continue
        tail = text[idx: idx + 60]
        d = _parse_date_any(tail)
        if d:
            return d
    return _parse_date_any(text)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_message(row: dict) -> Optional[MessageFact]:
    """Return one MessageFact for a messages.csv row, or None if irrelevant."""
    text = row["message_text"].lower()
    t_en = text
    sent = _sent(row)

    def mk(kind):
        return _fact(row, kind)

    if _debits_fail(t_en):
        return mk("debit_failed_retry")
    if _extra_charge(t_en):
        return mk("card_charge_investigation")
    if _transfer_internal(t_en):
        return mk("transfer_internal")
    if _rent_increase(t_en):
        return mk("rent_increase")
    if _min_two_cards(t_en):
        return mk("min_payment_two_cards")
    if _prize_scam(t_en):
        return mk("prize_scam")
    if _prize_credited(t_en):
        return mk("prize_credited")
    if _prize_pending(t_en):
        return mk("prize_pending")
    if _sale_settled(t_en):
        return mk("sale_settled")
    if _investment_value(t_en):
        return mk("investment_value_only")
    if _refund_fx(t_en):
        return mk("refund_fx")
    if _refund_pending(t_en):
        return mk("refund_pending")
    if _invoice_approved(t_en):
        f = mk("invoice_approved")
        d = _date_for_fact(row["message_text"])
        if d:
            f.date = d
        return f
    if _reimbursement(t_en):
        return mk("reimbursement_credited")
    # ----- employer payroll family -----
    if _household_reduce(t_en):
        return mk("salary_household_reduce")
    if _salary_resume(t_en):
        f = mk("salary_resume")
        d = _date_for_fact(row["message_text"])
        if d:
            f.date = d
        return f
    if _seasonal_end(t_en):
        return mk("salary_stop_seasonal")
    if _employment_end(t_en):
        return mk("salary_stop_employment")
    if _salary_increase(t_en):
        return mk("salary_increase")
    if _salary_base_only(t_en):
        return mk("salary_base_only")
    if _salary_reduce_next(t_en):
        f = mk("salary_reduce_next")
        d = _date_for_fact(row["message_text"])
        if d:
            f.date = d
        return f
    if _salary_temp(t_en):
        return mk("salary_temp")
    if _salary_confirmed_fx(t_en):
        f = mk("salary_confirmed_fx")
        d = _date_for_fact(row["message_text"])
        if d:
            f.date = d
        return f
    if _salary_confirmed_credit(t_en):
        f = mk("salary_confirmed_credit")
        d = _date_for_fact(row["message_text"])
        if d:
            f.date = d
        return f
    if _first_salary(t_en):
        f = mk("salary_first")
        d = _date_for_fact(row["message_text"])
        if d:
            f.date = d
        return f
    if _salary_scheduled(t_en):
        f = mk("salary_scheduled")
        d = _date_for_fact(row["message_text"])
        if d:
            f.date = d
        return f
    if _salary_date_change(t_en):
        f = mk("salary_date_change")
        d = _date_for_fact(row["message_text"])
        if d:
            f.date = d
        return f
    if _salary_regular_arrears(t_en):
        f = mk("salary_regular_arrears")
        d = _date_for_fact(row["message_text"])
        if d:
            f.date = d
        return f
    if _salary_routine_confirmed(t_en):
        return mk("salary_routine_confirmed")
    if _bonus_pending(t_en):
        return mk("bonus_pending")
    if _order_paid(t_en):
        return mk("order_paid")
    if _property_receipt(t_en):
        return mk("property_payment_receipt")
    if _bill_fx(t_en):
        return mk("bill_fx_charged")
    if _payout_pending(t_en):
        return mk("payout_pending")
    return None


def parse_messages(rows: list[dict]) -> dict[str, list[MessageFact]]:
    """Parse all messages.csv rows -> {user_id: [MessageFact, ...]} sorted by sent_at."""
    out: dict[str, list[MessageFact]] = {}
    for row in rows:
        f = parse_message(row)
        if f is None:
            continue
        out.setdefault(f.user_id, []).append(f)
    for lst in out.values():
        lst.sort(key=lambda f: f.sent_at)
    return out


# Facts that allow the salary stream to keep being projected (continuation
# evidence), distinguishing a raise/date tweak from a permanent stop.
SALARY_CONTINUE_KINDS = {
    "salary_increase",
    "salary_reduce_next",
    "salary_temp",
    "salary_first",
    "salary_scheduled",
    "salary_confirmed_fx",
    "salary_confirmed_credit",
    "salary_base_only",
    "salary_resume",
    "salary_regular_arrears",
    "salary_routine_confirmed",
}

# Facts that carry a replacement amount for the salary stream.
SALARY_AMOUNT_KINDS = {
    "salary_increase",
    "salary_reduce_next",
    "salary_temp",
    "salary_first",
    "salary_scheduled",
    "salary_confirmed_fx",
    "salary_confirmed_credit",
    "salary_base_only",
    "salary_resume",
}

SALARY_STOP_KINDS = {"salary_stop_seasonal", "salary_stop_employment"}