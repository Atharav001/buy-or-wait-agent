"""reconciler: turn raw + extracted events into one clean cash-flow ledger.

Encodes the design.md section 3.4 rules exactly:
  * inclusion: exclude failed/cancelled, never spend unrealized/non-cash value,
    reserve pending/scheduled debits, ignore pending non-salary credits until
    settled (DEC-007)
  * conflict priority on duplicate chains via linked_event_id (DEC-005):
    explicit amendment/cancellation > newer record > settled > safer reading
  * recurrence only with real repeat history (DEC-008)
  * applies ClaimedFacts from messages at event scope (amend/delay/cancel/confirm)
  * exposes flexible recurring streams for select_spending_changes (DEC-025/028)

Output: ReconciledLedger of net daily flows in home_currency + audit trail.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from currency import MissingRateError, convert
from ingest import RawDataset
from messages_rules import (
    MessageFact,
    SALARY_AMOUNT_KINDS,
    SALARY_CONTINUE_KINDS,
    SALARY_STOP_KINDS,
)
from models import ClaimedFact, FinancialEvent, FinancialProfile

# experimental: force-skip the salary continuation gate (ablation/testing only)
_FORCE_SALARY: bool = False

RECURRING_TYPES_DEBIT = {"expense", "subscription", "debt_payment"}
CONFIRMED_CREDIT_TYPES = {"income", "refund", "investment_sale"}
# Real dataset cadences (Phase 0 scan): monthly dominates, then 7/5/10/14/21.
# 28/30/31 are kept as fixed-day fallbacks only; monthly is detected preferred.
CANDIDATE_INTERVALS = (5, 7, 10, 14, 21, 28, 30, 31, 91, 365)
MONTHLY = "monthly"


class ReconciliationError(Exception):
    pass


@dataclass
class Recurrence:
    category: str
    direction: str  # debit for expenses, credit for salary
    interval_days: int | str  # fixed days or MONTHLY ("monthly")
    anchor_date: dt.date
    amount: Decimal
    event_type: str
    flexibility: str
    minimum_allowed_amount: Optional[Decimal]
    event_ids: list[str] = field(default_factory=list)
    extra_amount_per_month: Decimal = Decimal("0")  # reserved residual allowance

    @property
    def is_credit(self) -> bool:
        return self.direction == "credit"

    def occurrences_from(self, from_date: dt.date, end: dt.date) -> list[dt.date]:
        """Future occurrence dates of this stream inside [from_date, end]."""
        out: list[dt.date] = []
        d = _step(self.anchor_date, self.interval_days)
        while d <= end:
            if d >= from_date:
                out.append(d)
            d = _step(d, self.interval_days)
        return out


@dataclass
class ReconciledLedger:
    user_id: str
    request_date: dt.date
    horizon_end: dt.date
    profile: FinancialProfile
    start_balance: Decimal
    flows: dict[dt.date, Decimal] = field(default_factory=dict)
    recurrences: list[Recurrence] = field(default_factory=list)
    audit: list[str] = field(default_factory=list)


def _as_date(v: Optional[dt.date | str]) -> Optional[dt.date]:
    """ingest/models already parse ISО dates to dt.date; accept both."""
    if v is None:
        return None
    return v if isinstance(v, dt.date) else dt.date.fromisoformat(str(v))


def _conv(amount: Decimal, from_ccy: str, to_ccy: str, on_date: str, ds: RawDataset) -> Decimal:
    if from_ccy == to_ccy:
        return amount
    try:
        return convert(amount, from_ccy, to_ccy, on_date, ds.exchange_rates)
    except MissingRateError:
        return None  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Message facts -> affected event rows (event-scoped evidence, DEC-024)
# ---------------------------------------------------------------------------


def apply_facts_to_events(events: list[FinancialEvent], facts: list[ClaimedFact]) -> dict[str, FinancialEvent]:
    """Return {event_id: effective event} after message amendments.

    Only event-scoped facts (target_event_id populated) are applied here.
    Supported kinds: amend_amount (payload: amount/currency/date),
    delay_event (payload: new_date), cancel_event (drop), confirm_event
    (force inclusion).
    """
    effective = {e.event_id: e for e in events}
    for f in facts:
        tgt = f.target_event_id
        if not tgt or tgt not in effective:
            continue
        e = effective[tgt]
        if f.kind == "amend_amount":
            payload = f.payload or {}
            if "amount" in payload:
                e = e.model_copy(
                    update={"amount": Decimal(str(payload["amount"]))}
                )
            if "date" in payload:
                e = e.model_copy(update={"event_date": str(payload["date"])})
            if "currency" in payload:
                e = e.model_copy(update={"currency": str(payload["currency"]).upper()})
        elif f.kind == "delay_event":
            new_date = f.payload.get("new_date")
            if new_date:
                e = e.model_copy(
                    update={"settlement_date": str(new_date), "status": "scheduled"}
                )
        elif f.kind == "cancel_event":
            e = e.model_copy(update={"status": "cancelled"})
        elif f.kind == "confirm_event":
            e = e.model_copy(update={"status": "settled"})
        effective[tgt] = e
    return effective


# ---------------------------------------------------------------------------
# Chain dedupe via linked_event_id (DEC-005)
# ---------------------------------------------------------------------------


def collapse_chains(events: dict[str, FinancialEvent]) -> list[FinancialEvent]:
    """One economic event per connected linked_event_id chain.

    Priority within a chain: explicit cancellation drops everything; else a
    settled row beats scheduled/pending; same priority -> latest settlement
    date wins (newer record supersedes, DEC-005 rule 2).
    """
    events = dict(events)
    parent: dict[str, str] = {}

    def find(eid: str) -> str:
        parent.setdefault(eid, eid)
        while parent[eid] != eid:
            parent[eid] = parent[parent[eid]]
            eid = parent[eid]
        return eid

    for e in events.values():
        if e.linked_event_id and e.linked_event_id in events:
            a, b = find(e.event_id), find(e.linked_event_id)
            if a != b:
                parent[a] = b

    groups: dict[str, list[FinancialEvent]] = {}
    for e in events.values():
        groups.setdefault(find(e.event_id), []).append(e)

    survivors: list[FinancialEvent] = []
    for chain in groups.values():
        if any(e.status == "cancelled" for e in chain):
            continue  # explicit cancellation wins (rule 1)
        status_rank = {"settled": 0, "scheduled": 1, "pending": 2}
        chain.sort(
            key=lambda e: (
                status_rank.get(e.status, 3),
                -(_as_date(e.settlement_date) or dt.date(1900, 1, 1)).toordinal(),
            )
        )
        survivors.append(chain[0])
    return survivors


# ---------------------------------------------------------------------------
# Recurrence detection (DEC-008): repeat history only
# ---------------------------------------------------------------------------


def _detect_interval(dates: list[dt.date]) -> Optional[int | str]:
    """Cadence of a repeat stream, from real repeat history only (DEC-008).

    Calendar-month signatures are preferred (database generator's dominant
    cadence) and step cleanly without drift. Month-end clamping (e.g. Jan 31
    -> Feb 29) counts as monthly. Otherwise the best fixed-day interval wins
    with a 2-day tolerance.
    """
    if len(dates) < 2:
        return None
    gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
    assert gaps
    month_matches = 0
    for a, b in zip(dates, dates[1:]):
        delta_months = (b.year - a.year) * 12 + (b.month - a.month)
        if delta_months == 1 and (
            abs(b.day - a.day) <= 2 or (a.day >= 28 and b.day >= 28)
        ):
            month_matches += 1
    if month_matches >= max(1, (len(gaps) + 1) // 2):
        return MONTHLY
    for cand in CANDIDATE_INTERVALS:
        # tighter tolerance for small cadences so 5 vs 7 never blur (Phase 0:
        # real weekly streams are exactly 7d, weekday streams exactly 5d)
        tol = 1 if cand <= 7 else 2
        matches = sum(1 for g in gaps if abs(g - cand) <= tol)
        if matches >= max(1, (len(gaps) + 1) // 2):
            return cand
    return None


def _step(d: dt.date, step: int | str) -> dt.date:
    """Next occurrence after d for a detected step (calendar month or days)."""
    if step == MONTHLY:
        year = d.year + ((d.month) // 12)
        month = (d.month % 12) + 1
        day = min(d.day, _days_in_month(year, month))
        return dt.date(year, month, day)
    return d + dt.timedelta(days=step)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (dt.date(year, month + 1, 1) - dt.date(year, month, 1)).days


def _history_dates(events: list[FinancialEvent], direction: str) -> dict[str, list[FinancialEvent]]:
    """Group repeat-stream *evidence* by category with sorted settlement dates.

    DEC-008: recurrence only when 2+ real supplied occurrences support it.
    The dataset ships a rolling window of rows (a trailing few settled + the
    next scheduled ones) rather than full history, so evidence = real rows:
      * debit streams: settled + scheduled + pending (debits are reserved)
      * credit streams: settled + scheduled only (pending non-salary credits
        must never be projected into phantom income)
    Projection then only fills dates with no explicit confirmed row, so this
    can never double-book a supplied occurrence (DEC-004/007 safety).
    """
    eligible_status = ("settled", "scheduled", "pending") if direction == "debit" else ("settled", "scheduled")
    groups: dict[str, list[FinancialEvent]] = {}
    for e in events:
        if e.direction != direction or e.direction == "non_cash":
            continue
        if e.status not in eligible_status or e.amount is None or e.settlement_date is None:
            continue
        groups.setdefault(e.category, []).append(e)
    for g in groups.values():
        g.sort(key=lambda e: e.settlement_date)
    return groups


# ---------------------------------------------------------------------------
# Message-aware salary streams (DEC-024/027): continuation is *evidence-gated*
# ---------------------------------------------------------------------------


def _salary_base_stream(evs: list[FinancialEvent]) -> tuple[Optional[int], Optional[Decimal], list[FinancialEvent]]:
    """Identify the regular (base) salary sub-stream.

    Salary credits often contain several cadences in one category (monthly
    salary + monthly commission, or a second job). The generator's base
    stream is the cluster that is both well populated and stable in amount.
    Returns (base_day, base_amount, sub_stream_events).
    """
    if not evs:
        return None, None, []
    by_day: dict[int, list[FinancialEvent]] = {}
    for e in evs:
        by_day.setdefault(e.settlement_date.day, []).append(e)  # type: ignore[union-attr]
    days = sorted(by_day)

    # union-find day clusters with tolerance 2 (monthly-day signature: ±2 or
    # both >= 28, matching _detect_interval's monthly rule)
    parent = {d: d for d in days}

    def find(d):
        parent.setdefault(d, d)
        while parent[d] != d:
            parent[d] = parent[parent[d]]
            d = parent[d]
        return d

    for i, a in enumerate(days):
        for b in days[i + 1:]:
            if b - a <= 2 or (a >= 28 and b >= 28):
                parent[find(a)] = find(b)
    clusters: dict[int, list[int]] = {}
    for d in days:
        clusters.setdefault(find(d), []).append(d)

    def spread(evs_like):  # relative variability of amounts
        amts = [abs(e.amount) for e in evs_like if e.amount]
        if not amts:
            return None
        mean = sum(amts, Decimal(0)) / len(amts)
        if mean == 0:
            return Decimal("0")
        var = sum((a - mean) ** 2 for a in amts) / len(amts)
        return var

    best = None
    best_score = None
    for cluster_days in clusters.values():
        members = [e for d in cluster_days for e in by_day[d]]
        if len(members) < 2:
            continue
        s = spread(members)
        if s is None:
            continue
        # prefer: more events, then lower relative variability, then later day
        score = (-len(members), s, min(cluster_days))
        if best_score is None or score < best_score:
            best_score = score
            best = (cluster_days, members)

    if best is None:
        return None, None, []
    cluster_days, members = best
    members = sorted(members, key=lambda e: e.settlement_date)
    base_day = _most_frequent_day(members)
    return base_day, members[-1].amount, members


def _most_frequent_day(evs: list[FinancialEvent]) -> int:
    counts: dict[int, int] = {}
    for e in evs:
        counts[e.settlement_date.day] = counts.get(e.settlement_date.day, 0) + 1  # type: ignore[union-attr]
    return max(counts, key=lambda d: (counts[d], -d))


def _latest_payroll_override(
    msgs: list[MessageFact],
) -> tuple[Optional[Decimal], Optional[dt.date], Optional[str]]:
    """Most recent (by sent_at) salary amount fact for the user."""
    cands = [
        f for f in msgs
        if f.kind in SALARY_AMOUNT_KINDS and f.amount is not None
    ]
    if not cands:
        return None, None, None
    cands.sort(key=lambda f: f.sent_at)
    f = cands[-1]
    return f.amount, f.date, f.kind


def _salary_sub_streams(
    evs: list[FinancialEvent],
) -> list[tuple[int, list[FinancialEvent]]]:
    """Independent salary sub-streams by day-cluster, each with 2+ events."""
    by_day: dict[int, list[FinancialEvent]] = {}
    for e in evs:
        by_day.setdefault(e.settlement_date.day, []).append(e)  # type: ignore[union-attr]
    days = sorted(by_day)
    parent = {d: d for d in days}

    def find(d):
        parent.setdefault(d, d)
        while parent[d] != d:
            parent[d] = parent[parent[d]]
            d = parent[d]
        return d

    for i, a in enumerate(days):
        for b in days[i + 1:]:
            if b - a <= 2 or (a >= 28 and b >= 28):
                parent[find(a)] = find(b)
    out: list[tuple[int, list[FinancialEvent]]] = []
    grouped: dict[int, list[int]] = {}
    for d in days:
        grouped.setdefault(find(d), []).append(d)
    for cluster_days in grouped.values():
        members = sorted(
            (e for d in cluster_days for e in by_day[d]),
            key=lambda e: e.settlement_date,
        )
        if len(members) >= 2:
            out.append((_most_frequent_day(members), members))
    return out


def _credit_continuation_evidence(
    evs: list[FinancialEvent],
    request_date: dt.date,
    horizon_end: dt.date,
    msgs: list[MessageFact],
) -> bool:
    """True if this credit stream has evidence that it continues.

    Evidence = a future-settled (>= last settled) schedule row in the stream,
    OR an in-window explicit credit row, OR a confirming message fact for the
    category (invoice approved / sale settled / prize credited).
    """
    scheduled_after = any(
        e.status == "scheduled"
        and e.settlement_date is not None
        and e.settlement_date > _max_settled(evs)
        for e in evs
    )
    in_window = any(
        e.settlement_date is not None
        and request_date <= e.settlement_date <= horizon_end
        and e.status in ("settled", "scheduled")
        for e in evs
    )
    return scheduled_after or in_window


def _max_settled(evs: list[FinancialEvent]) -> dt.date:
    ds = [e.settlement_date for e in evs if e.status == "settled" and e.settlement_date]
    return max(ds) if ds else dt.date(1900, 1, 1)


def _next_occurrence(after: dt.date, step: int | str, strictly_after: dt.date) -> Optional[dt.date]:
    d = _step(after, step)
    while d <= strictly_after:
        d = _step(d, step)
    return d


def _next_on_day(after: dt.date, day: int) -> dt.date:
    y, m = after.year, after.month
    while True:
        if m == 12:
            y += 1
            m = 1
        else:
            m += 1
        last = _days_in_month(y, m)
        cand = dt.date(y, m, min(day, last))
        if cand > after:
            return cand


def _book_salary_streams(
    ledger: ReconciledLedger,
    survivors: list[FinancialEvent],
    evs: list[FinancialEvent],
    msgs: list[MessageFact],
    ds: RawDataset,
    hc: str,
    request_date: dt.date,
    horizon_end: dt.date,
    explicit_events_by_category: dict[str, list[tuple[dt.date, Decimal]]],
    household_reduce_facts: list[MessageFact],
) -> None:
    """Project salary credits with evidence-gated continuation + overrides.

    Gold-generator semantics reproduced here:
      * salary streams keep projecting through the forecast window unless an
        explicit stop/pending signal kills them; an employer payroll message,
        a scheduled future salary row, or a regular settled cadence all mean
        the payroll continues.
      * payroll messages set the continued amount (raise applies from its
        effective date; reduce applies from the next cycle and persists;
        temp/resume/first pay persist) and confirm ongoing payroll; the base
        stream replaces secondary ones.
      * a confirmed-date message migrates the cadence day of month.
      * employment / seasonal stop = no salary credit after the message date.
    """
    explicit_days = {d for d, _ in explicit_events_by_category.get("salary", [])}
    pay_msgs = [f for f in msgs if f.kind in SALARY_CONTINUE_KINDS]
    stop_msgs = [f for f in msgs if f.kind in SALARY_STOP_KINDS]
    stop_after = None
    if stop_msgs:
        stop_after = max((f.date or f.sent_at.date()) for f in stop_msgs)

    # a payout that is still pending (gig/contract income) is not confirmed
    # money: no income is counted until it actually settles (user_10 pattern).
    if any(f.kind == "payout_pending" for f in msgs):
        return

    over_amount, over_date, over_kind = _latest_payroll_override(pay_msgs)
    if household_reduce_facts:
        hr = max(household_reduce_facts, key=lambda f: f.sent_at)
        if hr.amount is not None:
            over_amount, over_date, over_kind = hr.amount, hr.date, "salary_temp"

    date_change = [f for f in msgs if f.kind == "salary_date_change" and f.date]

    base_day, base_amount, base_evs = _salary_base_stream(evs)
    if base_day is None or not base_evs or base_amount is None:
        return

    # with a payroll message the generator focuses on the single regular
    # salary; variable secondary streams (commissions etc.) stay pending.
    streams: list[tuple[int, list[FinancialEvent]]] = [(base_day, base_evs)]
    if not pay_msgs:
        for day, members in _salary_sub_streams(evs):
            if members is base_evs:
                continue
            if any(
                e.status == "scheduled" and e.settlement_date
                and e.settlement_date > _max_settled(members)
                for e in members
            ):
                streams.append((day, members))

    base_last = _max_settled(base_evs)
    for day, members in streams:
        is_base = members is base_evs
        if is_base:
            use_day = base_day
            if date_change:
                use_day = date_change[-1].date.day  # type: ignore[union-attr]
                base_last = max(
                    (e.settlement_date for e in base_evs
                     if e.settlement_date is not None and e.settlement_date <= date_change[-1].date),
                    default=base_last,
                )
            d = _next_on_day(base_last, use_day)
            step_day = use_day
            currency = base_evs[-1].currency
            rec_anchor = base_last
        else:
            m_last = _max_settled(members)
            d = _next_occurrence(m_last, MONTHLY, m_last)
            step_day = _most_frequent_day(members)
            currency = members[-1].currency
            rec_anchor = m_last

        stream_amount = base_amount if is_base else members[-1].amount

        def amount_on(dd: dt.date) -> Decimal:
            if over_amount is None or not is_base:
                return stream_amount
            if over_kind == "salary_increase":
                eff = over_date or request_date
                return over_amount if dd >= eff else base_amount
            if over_kind == "salary_reduce_next":
                first = _next_on_day(base_last, base_day)
                return over_amount if dd >= first else stream_amount
            return over_amount

        rec = Recurrence(
            category="salary",
            direction="credit",
            interval_days=MONTHLY,
            anchor_date=rec_anchor,
            amount=amount_on(_next_on_day(rec_anchor, step_day)),
            event_type=members[-1].event_type,
            flexibility=members[-1].flexibility,
            minimum_allowed_amount=members[-1].minimum_allowed_amount,
            event_ids=[e.event_id for e in members],
        )
        ledger.recurrences.append(rec)

        while d <= horizon_end:
            if d < request_date or d in explicit_days:
                d = _next_on_day(d, step_day)
                continue
            if stop_after is not None and d > stop_after and not pay_msgs:
                break
            amt = amount_on(d)
            if amt is None:
                d = _next_on_day(d, step_day)
                continue
            home = _conv(amt, currency, hc, d.isoformat(), ds)
            if home is None:
                d = _next_on_day(d, step_day)
                continue
            ledger.flows[d] = ledger.flows.get(d, Decimal("0")) + home
            ledger.audit.append(f"recurrence salary@{d.strftime('%Y-%m-%d')} {home}")
            d = _next_on_day(d, step_day)


# ---------------------------------------------------------------------------
# Main reconcile entry point
# ---------------------------------------------------------------------------


def reconcile(
    ds: RawDataset,
    user_id: str,
    request_date: dt.date,
    facts: list[ClaimedFact] | None = None,
    msg_facts: list[MessageFact] | None = None,
    horizon_days: int = 90,
) -> ReconciledLedger:
    profile = ds.profiles[user_id]
    horizon_end = request_date + dt.timedelta(days=horizon_days)
    facts = facts or []
    msg_facts = msg_facts or []

    # only evidence available on or before the request date counts (temporal).
    msgs = [
        f for f in msg_facts
        if f.user_id == user_id and f.sent_at.date() <= request_date
    ]

    all_events = ds.events_by_user.get(user_id, [])
    effective = apply_facts_to_events(all_events, facts)
    survivors = collapse_chains(effective)
    survivors = [e for e in survivors if e.status not in ("cancelled", "failed")]

    ledger = ReconciledLedger(
        user_id=user_id,
        request_date=request_date,
        horizon_end=horizon_end,
        profile=profile,
        start_balance=profile.current_available_balance,
    )

    hc = profile.home_currency

    def to_home(e: FinancialEvent) -> Optional[Decimal]:
        if e.amount is None:
            return None  # unresolved blank amount -> excluded (safer, DEC-004/006)
        on = e.settlement_date or e.event_date
        return _conv(e.amount, e.currency, hc, on, ds)

    # -- explicit ledger rows inside the horizon (confirmed facts) ----------
    explicit_events_by_category: dict[str, list[tuple[dt.date, Decimal]]] = {}
    for e in survivors:
        settle = _as_date(e.settlement_date) or _as_date(e.event_date)
        if not (request_date <= settle <= horizon_end):
            continue
        amt = to_home(e)
        if amt is None:
            ledger.audit.append(
                f"excluded {e.event_id}: unresolved blank amount/rate (conservative)"
            )
            continue
        if e.direction == "debit":
            delta = -amt
        elif e.direction == "credit":
            if e.status in ("settled", "scheduled") and e.category == "salary":
                delta = amt  # confirmed salary counts on settlement date
            elif e.status == "settled":
                delta = amt  # settled refund/sale counts
            else:
                ledger.audit.append(
                    f"excluded {e.event_id}: pending non-salary credit not settled"
                )
                continue
        else:  # non_cash / unrealized: never spendable
            ledger.audit.append(f"excluded {e.event_id}: non-cash (unrealized value)")
            continue
        d = settle
        ledger.flows[d] = ledger.flows.get(d, Decimal("0")) + delta
        explicit_events_by_category.setdefault(e.category, []).append((d, amt))

    # -- recurrence projection for streams with real settled history ----------
    # Debits: settled + scheduled + pending reserve history (unchanged).
    # Credits: evidence-gated continuation; salary streams additionally honour
    # payroll messages (raise / reduction / resume / stop / first pay / date).
    payout_pending = {f.message_id for f in msgs if f.kind == "payout_pending"}
    invoice_facts = [
        f for f in msgs
        if f.kind == "invoice_approved" and f.date and f.amount is not None
    ]
    rent_facts = [f for f in msgs if f.kind == "rent_increase"]
    household_reduce = [f for f in msgs if f.kind == "salary_household_reduce"]

    for direction in ("debit", "credit"):
        groups = _history_dates(survivors, direction)
        for category, evs in groups.items():
            if category == "salary" and direction == "credit":
                _book_salary_streams(
                    ledger, survivors, evs, msgs, ds, hc,
                    request_date, horizon_end, explicit_events_by_category,
                    household_reduce,
                )
                continue
            if direction == "credit":
                # pending-payout notice kills the projection of that stream.
                if payout_pending:
                    continue
                if not _credit_continuation_evidence(evs, request_date, horizon_end, msgs):
                    continue
            dates = [e.settlement_date for e in evs if e.settlement_date]
            step = _detect_interval(dates)
            if step is None:
                continue
            anchor = dates[-1]
            amount = evs[-1].amount
            event_ids = [e.event_id for e in evs]
            # real confirmed (settled/scheduled/pending) horizon rows for this
            # category are already booked above; never double-project them.
            explicit_days = {
                d for d, _ in explicit_events_by_category.get(category, [])
            }
            # fixed rent streams: apply a +12% message override after its date.
            rent_override = None
            if category == "rent" and rent_facts:
                base_d = _max_settled(evs)
                nxt = _next_occurrence(anchor, step, base_d)
                if nxt:
                    rent_override = (nxt, amount * Decimal("1.12"))
            rec = Recurrence(
                category=category,
                direction=direction,
                interval_days=step,
                anchor_date=anchor,
                amount=amount,
                event_type=evs[-1].event_type,
                flexibility=evs[-1].flexibility,
                minimum_allowed_amount=evs[-1].minimum_allowed_amount,
                event_ids=event_ids,
            )
            ledger.recurrences.append(rec)
            d = anchor
            while True:
                d = _step(d, step)
                if d > horizon_end:
                    break
                if d < request_date or d in explicit_days:
                    continue  # history (in start balance) / confirmed row already booked
                amt = amount
                if rent_override and d >= rent_override[0]:
                    amt = rent_override[1]
                delta = -amt if direction == "debit" else amt
                ledger.flows[d] = ledger.flows.get(d, Decimal("0")) + delta
                ledger.audit.append(
                    f"recurrence {category}@{d.strftime('%Y-%m-%d')} {delta}"
                )

    # -- confirmed invoice credits (client-approved invoices) ---------------
    for f in invoice_facts:
        d = f.date
        if not (request_date <= d <= horizon_end):
            continue
        amt = _conv(f.amount, f.currency or hc, hc, d.isoformat(), ds)
        if amt is None:
            continue
        ledger.flows[d] = ledger.flows.get(d, Decimal("0")) + amt
        ledger.audit.append(f"invoice credit confirmed @{d} {amt}")

    return ledger


# ---------------------------------------------------------------------------
# Spending-change support (DEC-025, DEC-028)
# ---------------------------------------------------------------------------


def flows_with_spending_changes(
    ledger: ReconciledLedger,
    changes: list[tuple[Recurrence, str, Optional[Decimal]]],
    from_date: dt.date,
) -> dict[dt.date, Decimal]:
    """Copy of ledger.flows with stop/reduce_to adjustments applied.

    Each change tuple is (recurrence, action in {stop, reduce_to}, new_amount).
    Only recurring debit projections from from_date onward are adjusted; the
    anchored/occurred history is untouched (already inside start balance).
    """
    flows = dict(ledger.flows)
    for rec, action, new_amount in changes:
        if rec.direction != "debit":
            continue
        saved = rec.amount if action == "stop" else (rec.amount - new_amount)
        if saved <= 0:
            continue
        for d in rec.occurrences_from(from_date, ledger.horizon_end):
            flows[d] = flows.get(d, Decimal("0")) + saved  # debit reduced => +saved
    return flows


def eligible_flexible_streams(
    ledger: ReconciledLedger,
) -> list[tuple[Recurrence, bool, bool]]:
    """Recurring debit streams the user allowed to adjust (never protected).

    Each item: (recurrence, can_stop, can_reduce). DEC-025/028: never touch
    protected categories; sort largest-amount-first for the minimal set.
    """
    prof = ledger.profile
    protected = set(prof.expense_categories_to_protect)
    reduce_cats = set(prof.expense_categories_user_is_willing_to_reduce)
    stop_cats = set(prof.expense_categories_user_is_willing_to_stop)
    out: list[tuple[Recurrence, bool, bool]] = []
    for r in ledger.recurrences:
        if r.direction != "debit" or r.category in protected:
            continue
        if r.amount <= 0:
            continue
        min_allowed = r.minimum_allowed_amount
        can_reduce = (
            r.flexibility in ("reducible", "reducible_or_stoppable")
            and r.category in reduce_cats
            and min_allowed is not None
            and min_allowed < r.amount
        )
        can_stop = (
            r.flexibility in ("stoppable", "reducible_or_stoppable")
            and r.category in stop_cats
        )
        if can_reduce or can_stop:
            out.append((r, can_stop, can_reduce))
    out.sort(key=lambda t: (-t[0].amount, t[0].category, t[0].anchor_date.isoformat()))
    return out