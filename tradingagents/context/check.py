"""Checks on the caller's verified data: do the periods, totals and ties agree?

A passing set is internally consistent. That is not the same as true: it says
nothing about whether the source matches the filings. Each problem has a stable
``code`` and a ``path`` to the offending value, so a caller can drop the failing
block and still send the request.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from tradingagents.context.models import Balance, Context, Events, Financials, Price, Statement

# How long each kind of period runs, in days.
_DAYS = {"standalone": (60, 120), "ytd": (120, 300), "annual": (300, 400)}
_TOLERANCE = 0.01          # totals may differ by 1%: filers round
_PRICE_WINDOW_DAYS = 7     # a close this old still stands for the run date (weekends, holidays)


@dataclass(frozen=True)
class Problem:
    code: str
    path: str
    message: str


def _money(value: float) -> str:
    return f"${value / 1e9:,.3f}B"


def _zero(label: str, path: str, name: str) -> Problem:
    return Problem("zero_placeholder", path,
                   f"{label}: {name} is 0, which looks like a missing value; send null if it is unknown")


def _derived_start_ok(s: Statement, rows: list[Statement]) -> bool:
    """A standalone quarter without a start is measured from the quarter before it."""
    earlier = [o.period_end for o in rows if o.kind == "standalone" and o.period_end < s.period_end]
    if not earlier:
        return False
    low, high = _DAYS["standalone"]
    return low <= (s.period_end - max(earlier)).days <= high


def _periods(rows: list[Statement], as_of: date, base: str) -> list[Problem]:
    found = []
    for i, s in enumerate(rows):
        path, low_high = f"{base}[{i}]", _DAYS[s.kind]
        if s.period_start is None:
            if s.kind == "ytd" or (s.kind == "standalone" and not _derived_start_ok(s, rows)):
                found.append(Problem("missing_period_start", f"{path}.period_start",
                                     f"{s.fiscal_period}: period_start is required for a {s.kind} figure "
                                     "unless a quarter ending 60-120 days earlier is also supplied"))
        elif not low_high[0] <= (s.period_end - s.period_start).days <= low_high[1]:
            days = (s.period_end - s.period_start).days
            found.append(Problem("period_length", path, f"{s.fiscal_period}: a {s.kind} figure should cover "
                                 f"{low_high[0]}-{low_high[1]} days, not {days} days"))
        if s.period_end > as_of:
            found.append(Problem("period_after_as_of", f"{path}.period_end",
                                 f"{s.fiscal_period}: the period ends after as_of"))
        if s.filed_at and s.filed_at < s.period_end:
            found.append(Problem("filed_before_period_end", f"{path}.filed_at",
                                 f"{s.fiscal_period}: filed before its period ended"))
    ends = [s.period_end for s in rows]
    for end in sorted({e for e in ends if ends.count(e) > 1}):
        found.append(Problem("duplicate_period", base, f"a period ending {end} appears twice"))
    return found


def _income(s: Statement, path: str) -> list[Problem]:
    i = s.income
    if i is None or i.revenue is None:
        return []
    found = []
    if i.revenue > 0:
        found += [_zero(s.fiscal_period, f"{path}.income.{name}", name) for name in
                  ("gross_profit", "operating_income", "net_income") if getattr(i, name) == 0]
    order = f"{path}.income"
    if i.gross_profit is not None and i.gross_profit > i.revenue:
        found.append(Problem("income_order", order, f"{s.fiscal_period}: gross profit exceeds revenue"))
    if i.operating_income is not None and i.gross_profit is not None and i.operating_income > i.gross_profit:
        found.append(Problem("income_order", order, f"{s.fiscal_period}: operating income exceeds gross profit"))
    if i.net_income is not None and i.net_income > i.revenue:
        found.append(Problem("income_order", order, f"{s.fiscal_period}: net income exceeds revenue"))
    return found


def _cash_flow(s: Statement, path: str) -> list[Problem]:
    c = s.cash_flow
    if c is None or c.net_cash_flow_from_operations is None:
        return []
    ocf, found = c.net_cash_flow_from_operations, []
    if ocf > 0:
        found += [_zero(s.fiscal_period, f"{path}.cash_flow.{name}", name)
                  for name in ("capital_expenditure", "free_cash_flow") if getattr(c, name) == 0]
    if c.capital_expenditure is not None and c.free_cash_flow is not None:
        expected = ocf - abs(c.capital_expenditure)       # capex may be signed either way
        if abs(c.free_cash_flow - expected) > _TOLERANCE * abs(ocf):
            found.append(Problem("fcf_mismatch", f"{path}.cash_flow",
                                 f"{s.fiscal_period}: free cash flow {_money(c.free_cash_flow)} does not equal "
                                 f"operating cash flow less capex {_money(expected)}"))
    return found


def _balance(b: Balance, path: str) -> list[Problem]:
    found = []
    if b.total_assets is not None and b.total_assets > 0:
        found += [_zero(b.fiscal_period, f"{path}.{name}", name)
                  for name in ("total_liabilities", "shareholders_equity") if getattr(b, name) == 0]
    if None not in (b.total_assets, b.total_liabilities, b.shareholders_equity):
        claimed = b.total_liabilities + b.shareholders_equity
        if abs(b.total_assets - claimed) > _TOLERANCE * abs(b.total_assets):
            found.append(Problem("balance_mismatch", path, f"{b.fiscal_period}: assets {_money(b.total_assets)} "
                                 f"do not equal liabilities plus equity {_money(claimed)}"))
    for part, whole in (("current_assets", "total_assets"), ("current_liabilities", "total_liabilities")):
        if None not in (getattr(b, part), getattr(b, whole)) and getattr(b, part) > getattr(b, whole):
            found.append(Problem("balance_order", path,
                                 f"{b.fiscal_period}: {part.replace('_', ' ')} exceed {whole.replace('_', ' ')}"))
    return found


def _balances(f: Financials) -> list[Problem]:
    found, dates = [], [b.as_of for b in f.balance]
    statements = [*f.annual, *f.quarterly]
    for i, b in enumerate(f.balance):
        path = f"financials.balance[{i}]"
        found += _balance(b, path)
        if b.as_of > f.as_of:
            found.append(Problem("period_after_as_of", f"{path}.as_of",
                                 f"{b.fiscal_period}: the balance sheet date is after as_of"))
        same_day = [s for s in statements if s.period_end == b.as_of]
        if same_day and all(s.fiscal_period.strip().lower() != b.fiscal_period.strip().lower() for s in same_day):
            found.append(Problem("balance_label_mismatch", path, f"{b.fiscal_period}: the statement ending "
                                 f"{b.as_of} is labelled {same_day[0].fiscal_period}"))
    for day in sorted({d for d in dates if dates.count(d) > 1}):
        found.append(Problem("duplicate_period", "financials.balance", f"a balance sheet dated {day} appears twice"))
    return found


def _quarters_vs_year(annual: Statement, quarters: list[Statement], path: str) -> list[Problem]:
    if annual.income is None or annual.income.revenue is None:
        return []
    start = annual.period_start or annual.period_end - timedelta(days=380)
    inside = [q for q in quarters if q.kind == "standalone" and start <= q.period_end <= annual.period_end
              and q.income is not None and q.income.revenue is not None]
    if not inside:
        return []
    total, year = sum(q.income.revenue for q in inside), annual.income.revenue
    label = f"{annual.fiscal_period}: the {len(inside)} quarters add up to {_money(total)}"
    if total > year * (1 + _TOLERANCE):
        return [Problem("quarters_exceed_year", path, f"{label}, more than the annual revenue {_money(year)}")]
    if len(inside) == 4 and abs(total - year) > _TOLERANCE * year:
        return [Problem("quarters_mismatch_year", path, f"{label}, not the annual revenue {_money(year)}")]
    return []


def check_financials(f: Financials) -> list[Problem]:
    """What is wrong with the statements; empty when they are consistent."""
    found = _periods(f.annual, f.as_of, "financials.annual") + _periods(f.quarterly, f.as_of, "financials.quarterly")
    for base, rows in (("financials.annual", f.annual), ("financials.quarterly", f.quarterly)):
        for i, s in enumerate(rows):
            found += _income(s, f"{base}[{i}]") + _cash_flow(s, f"{base}[{i}]")
    found += _balances(f)
    for i, annual in enumerate(f.annual):
        found += _quarters_vs_year(annual, f.quarterly, f"financials.annual[{i}]")
    return found


def check_price(p: Price, trade_date: date) -> list[Problem]:
    if p.as_of > trade_date or p.as_of < trade_date - timedelta(days=_PRICE_WINDOW_DAYS):
        return [Problem("price_date", "price.as_of", f"the price is dated {p.as_of}, which does not stand "
                        f"for the run date {trade_date} (it must be within {_PRICE_WINDOW_DAYS} days before it)")]
    return []


def check_events(e: Events) -> list[Problem]:
    found = []
    if e.next_earnings is not None and e.next_earnings.date <= e.as_of:
        found.append(Problem("earnings_date_passed", "events.next_earnings.date",
                             f"the next earnings date {e.next_earnings.date} is not after as_of {e.as_of}"))
    for i, r in enumerate(e.recent_earnings):
        path = f"events.recent_earnings[{i}]"
        if r.reported_on < r.period_end:
            found.append(Problem("reported_before_period_end", f"{path}.reported_on",
                                 f"{r.fiscal_period}: reported before its period ended"))
        if r.reported_on > e.as_of:
            found.append(Problem("reported_after_as_of", f"{path}.reported_on",
                                 f"{r.fiscal_period}: reported after as_of"))
    labels = [r.fiscal_period.strip().lower() for r in e.recent_earnings]
    for label in sorted({x for x in labels if labels.count(x) > 1}):
        found.append(Problem("duplicate_period", "events.recent_earnings", f"{label} appears twice"))
    return found


def check_context(context: Context, *, ticker: str | None = None, trade_date: date | None = None) -> list[Problem]:
    """Every problem with the context, including ties to the request when it is given."""
    found = []
    blocks = [("financials", context.financials), ("price", context.price), ("events", context.events)]
    if context.financials is not None:
        found += check_financials(context.financials)
    if context.events is not None:
        found += check_events(context.events)
    if ticker is not None:
        found += [Problem("ticker_mismatch", f"{name}.ticker",
                          f"{name} is for {block.ticker}, but the request is for {ticker}")
                  for name, block in blocks if block is not None and block.ticker != ticker]
    else:
        present = [(name, block) for name, block in blocks if block is not None]
        found += [Problem("ticker_mismatch", f"{name}.ticker",
                          f"{name} is for {block.ticker}, but {present[0][0]} is for {present[0][1].ticker}")
                  for name, block in present[1:] if block.ticker != present[0][1].ticker]
    if trade_date is not None:
        if context.price is not None:
            found += check_price(context.price, trade_date)
        if context.events is not None and context.events.as_of > trade_date:
            found.append(Problem("as_of_after_trade_date", "events.as_of",
                                 f"the events are dated {context.events.as_of}, after the run date {trade_date}"))
        if context.financials is not None and context.financials.as_of > trade_date:
            found.append(Problem("as_of_after_trade_date", "financials.as_of",
                                 f"the financials are dated {context.financials.as_of}, after the run date {trade_date}"))
    return found
