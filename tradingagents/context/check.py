"""Checks on the caller's verified data: do the periods, totals and ties agree?

A passing set is internally consistent. That is not the same as true: it says
nothing about whether the source matches the filings. Each problem has a stable
``code`` and a ``path`` to the offending value, so a caller can drop the failing
block and still send the request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from tradingagents.context.models import (
    Balance,
    Company,
    Context,
    EarningsRelease,
    Events,
    Financials,
    MarketBackdrop,
    Ownership,
    Price,
    PriorReport,
    RevenueBreakdown,
    Statement,
    Technicals,
    ThemeBackdrop,
    ValuationHistory,
)

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
    if None not in (b.total_debt, b.total_liabilities) and b.total_debt > b.total_liabilities:
        found.append(Problem("balance_order", path, f"{b.fiscal_period}: debt exceeds total liabilities"))
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


_MARKET_CAP_TOLERANCE = 0.03   # the cap and the close may be dated a day apart


def check_company(c: Company, price: Price | None) -> list[Problem]:
    if c.market_cap and c.shares_outstanding and price is not None:
        implied = price.close * c.shares_outstanding
        if abs(implied - c.market_cap) > _MARKET_CAP_TOLERANCE * c.market_cap:
            return [Problem("market_cap_mismatch", "company.market_cap",
                            f"market cap {_money(c.market_cap)} is not the price {price.close} times "
                            f"{c.shares_outstanding:,} shares ({_money(implied)})")]
    return []


_TIE_TOLERANCE = 0.005


def check_technicals(t: Technicals, price: Price | None, trade_date: date | None) -> list[Problem]:
    found, m = [], t.macd
    if m is not None and None not in (m.line, m.signal, m.histogram) \
            and abs(m.histogram - (m.line - m.signal)) > max(0.02, 0.01 * max(abs(m.line), abs(m.signal))):
        found.append(Problem("macd_inconsistent", "technicals.macd",
                             f"MACD histogram {m.histogram} is not the line {m.line} less the signal {m.signal}"))
    if (t.high_52w and t.close > t.high_52w * (1 + _TIE_TOLERANCE)) \
            or (t.low_52w and t.close < t.low_52w * (1 - _TIE_TOLERANCE)):
        found.append(Problem("range_inconsistent", "technicals.close",
                             f"close {t.close} is outside the 52-week range {t.low_52w}-{t.high_52w}"))
    if price is not None and price.as_of == t.as_of and abs(t.close - price.close) > _TIE_TOLERANCE * price.close:
        found.append(Problem("technicals_price_mismatch", "technicals.close",
                             f"technicals close {t.close} is not the price {price.close} on {t.as_of}"))
    if trade_date is not None and (t.as_of > trade_date or t.as_of < trade_date - timedelta(days=_PRICE_WINDOW_DAYS)):
        found.append(Problem("technicals_date", "technicals.as_of", f"the technicals are dated {t.as_of}, which does "
                             f"not stand for the run date {trade_date} (within {_PRICE_WINDOW_DAYS} days before it)"))
    return found


_PAGE = 20          # insider and congress lists stop here; counts above that are not listed
_VALUE_TOLERANCE = 0.02


def _later(path: str, label: str, when: date, as_of: date) -> list[Problem]:
    return [Problem("date_after_as_of", path, f"{label} {when} is after as_of {as_of}")] if when > as_of else []


def _check_holder(h, i: int, quarter_end: date, as_of: date) -> list[Problem]:
    path, found = f"ownership.funds.holders[{i}]", []
    found += _later(f"{path}.filed_on", f"{h.manager}: filed on", h.filed_on, as_of)
    if h.filed_on < quarter_end:
        found.append(Problem("filed_before_period_end", f"{path}.filed_on",
                             f"{h.manager}: filed {h.filed_on}, before the quarter ended {quarter_end}"))
    change, bad = h.shares_change, False
    if change is not None:
        bad = ((h.action == "new" and change != h.shares) or (h.action == "added" and change <= 0)
               or (h.action == "trimmed" and change >= 0) or (h.action == "unchanged" and change != 0)
               or (h.action == "exited" and (h.shares != 0 or change >= 0)))
        prior = h.shares - change
        if h.shares_change_pct is not None and prior > 0:
            bad = bad or abs(h.shares_change_pct - change / prior * 100) > max(0.5, 0.01 * abs(h.shares_change_pct))
    elif h.action == "exited" and h.shares != 0:
        bad = True
    if bad:
        found.append(Problem("holder_change_inconsistent", path,
                             f"{h.manager}: the action {h.action!r}, the shares and the change do not agree"))
    return found


def _check_funds(f, as_of: date) -> list[Problem]:
    found = _later("ownership.funds.quarter_end", "the quarter end", f.quarter_end, as_of)
    for i, h in enumerate(f.holders):
        found += _check_holder(h, i, f.quarter_end, as_of)
    if len(f.holders) > f.managers_tracked:
        found.append(Problem("managers_tracked_too_low", "ownership.funds",
                             f"{len(f.holders)} holders are listed but only {f.managers_tracked} managers are tracked"))
    return found


def _counts(listed: list, buys: int, sells: int, kind_of, code: str, path: str) -> list[Problem]:
    """Counts must cover every listed trade, and equal them when the list is not cut off."""
    n_buy = sum(kind_of(t) == "buy" for t in listed)
    n_sell = len(listed) - n_buy
    if n_buy > buys or n_sell > sells or (len(listed) < _PAGE and (n_buy, n_sell) != (buys, sells)):
        return [Problem(code, path, f"the counts say {buys} buys and {sells} sells, "
                                    f"but the list has {n_buy} buys and {n_sell} sells")]
    return []


def _check_insiders(ins, as_of: date) -> list[Problem]:
    found = []
    for i, t in enumerate(ins.trades):
        path = f"ownership.insiders.trades[{i}]"
        found += _later(f"{path}.filing_date", "the filing date", t.filing_date, as_of)
        if t.filing_date < t.transaction_date:
            found.append(Problem("filing_before_transaction", path, f"filed {t.filing_date}, before the trade on {t.transaction_date}"))
        if not as_of - timedelta(days=ins.window_days) <= t.transaction_date <= as_of:
            found.append(Problem("trade_outside_window", path,
                                 f"the trade on {t.transaction_date} is outside the {ins.window_days}-day window"))
        if t.value_usd is not None and abs(t.value_usd - t.shares * t.price_per_share) > _VALUE_TOLERANCE * t.value_usd:
            found.append(Problem("trade_value_mismatch", path,
                                 f"value {t.value_usd} is not {t.shares} shares at {t.price_per_share}"))
    found += _counts(ins.trades, ins.buys, ins.sells, lambda t: t.kind, "insider_count_mismatch", "ownership.insiders")
    complete = len(ins.trades) < _PAGE and all(t.value_usd is not None for t in ins.trades)
    if complete and ins.net_value_usd is not None:
        net = sum(t.value_usd if t.kind == "buy" else -t.value_usd for t in ins.trades)
        if abs(net - ins.net_value_usd) > max(1000, _VALUE_TOLERANCE * abs(ins.net_value_usd)):
            found.append(Problem("insider_net_mismatch", "ownership.insiders",
                                 f"net value {ins.net_value_usd} is not the listed trades' {net}"))
    return found


def _check_congress(c, as_of: date) -> list[Problem]:
    found = []
    for i, t in enumerate(c.trades):
        path = f"ownership.congress.trades[{i}]"
        found += _later(f"{path}.disclosed_on", "the disclosure date", t.disclosed_on, as_of)
        if t.disclosed_on < t.traded_on:
            found.append(Problem("disclosed_before_traded", path, f"disclosed {t.disclosed_on}, before the trade on {t.traded_on}"))
        if t.amount_min > t.amount_max:
            found.append(Problem("amount_range_inverted", path, f"the amount range {t.amount_min}-{t.amount_max} is inverted"))
        if not as_of - timedelta(days=c.window_days) <= t.disclosed_on <= as_of:
            found.append(Problem("trade_outside_window", path,
                                 f"disclosed {t.disclosed_on}, outside the {c.window_days}-day window"))
    found += _counts(c.trades, c.buys, c.sells, lambda t: "buy" if t.side == "buy" else "sell",
                     "congress_count_mismatch", "ownership.congress")
    return found


def check_ownership(o: Ownership) -> list[Problem]:
    found = []
    if o.funds is not None:
        found += _check_funds(o.funds, o.as_of)
    if o.insiders is not None:
        found += _check_insiders(o.insiders, o.as_of)
    if o.congress is not None:
        found += _check_congress(o.congress, o.as_of)
    return found


def check_valuation(v: ValuationHistory) -> list[Problem]:
    pe, found = v.pe_ttm, []
    if pe is None:
        return found
    path = "valuation_history.pe_ttm"
    low, med, high = pe.min_5y, pe.median_5y, pe.max_5y
    ordered = [x for x in (low, med, high) if x is not None]
    if ordered != sorted(ordered) or (low is not None and pe.latest < low * 0.99) \
            or (high is not None and pe.latest > high * 1.01):
        found.append(Problem("valuation_range_inconsistent", path,
                             f"P/E {pe.latest} is not inside its own range {low}-{high} (median {med})"))
    p = pe.percentile_5y
    if p is not None and ((high is not None and pe.latest >= high * 0.99 and p < 0.95)
                          or (low is not None and pe.latest <= low * 1.01 and p > 0.05)
                          or (med is not None and pe.latest > med * 1.02 and p < 0.45)
                          or (med is not None and pe.latest < med * 0.98 and p > 0.55)):
        found.append(Problem("percentile_inconsistent", path,
                             f"percentile {p} does not fit a P/E of {pe.latest} within {low}-{high} (median {med})"))
    return found


_FEAR_GREED_BANDS = ((25, "extreme fear"), (45, "fear"), (56, "neutral"), (76, "greed"), (101, "extreme greed"))


def check_backdrop(b: MarketBackdrop) -> list[Problem]:
    found = []
    for name in ("vix", "fear_and_greed", "buffett_indicator_pct", "credit_spread_hy_bps"):
        reading = getattr(b, name)
        if reading is not None:
            found += _later(f"market_backdrop.{name}.as_of", f"the {name.replace('_', ' ')} date", reading.as_of, b.as_of)
    fg = b.fear_and_greed
    if fg is not None:
        band = next(label for limit, label in _FEAR_GREED_BANDS if fg.value < limit)
        if fg.label.strip().lower() != band:
            found.append(Problem("label_inconsistent", "market_backdrop.fear_and_greed",
                                 f"Fear & Greed {fg.value:g} is {band}, not {fg.label!r}"))
    return found


_BUYS, _SELLS = ("Buy", "Overweight"), ("Sell", "Underweight")


def check_prior_reports(reports: list[PriorReport], trade_date: date | None) -> list[Problem]:
    found = []
    for i, r in enumerate(reports):
        path = f"prior_reports[{i}]"
        if trade_date is not None and r.as_of >= trade_date:
            found.append(Problem("prior_after_run", f"{path}.as_of",
                                 f"a previous report dated {r.as_of} is not before the run date {trade_date}"))
        if r.target is not None and ((r.decision in _BUYS and r.target <= r.price_then)
                                     or (r.decision in _SELLS and r.target >= r.price_then)):
            found.append(Problem("prior_target_inconsistent", f"{path}.target",
                                 f"a {r.decision} with a target of {r.target} against a price of {r.price_then}"))
    if any(a.as_of <= b.as_of for a, b in zip(reports, reports[1:], strict=False)):
        found.append(Problem("prior_order", "prior_reports", "previous reports must be newest first"))
    return found


_RELEASE_MAX_AGE_DAYS = 130       # a company reports about every 90 days; older means a newer release was missed
_TAG = re.compile(r"</?[A-Za-z][^>]*>")


def check_earnings_release(r: EarningsRelease, trade_date: date | None) -> list[Problem]:
    found = []
    if r.period_end is not None and r.period_end > r.filed_on:
        found.append(Problem("filed_before_period_end", "earnings_release.period_end",
                             f"the release is filed {r.filed_on}, before its period ended {r.period_end}"))
    if trade_date is not None:
        if r.filed_on > trade_date:
            found.append(Problem("as_of_after_trade_date", "earnings_release.filed_on",
                                 f"the release was filed {r.filed_on}, after the run date {trade_date}"))
        elif (trade_date - r.filed_on).days > _RELEASE_MAX_AGE_DAYS:
            found.append(Problem("release_stale", "earnings_release.filed_on",
                                 f"the release was filed {r.filed_on}, more than {_RELEASE_MAX_AGE_DAYS} days "
                                 f"before the run date {trade_date}: send the latest one"))
    if len(_TAG.findall(r.text)) > 20:
        found.append(Problem("text_not_plain", "earnings_release.text",
                             "the text still has HTML tags: strip them and send plain text"))
    return found


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


def _view_problems(view, i: int, rb: RevenueBreakdown) -> list[Problem]:
    path, found, seen = f"revenue_breakdown.views[{i}]", [], set()
    for j, seg in enumerate(view.segments):
        key = " ".join(seg.name.split()).casefold()
        if key in seen:
            found.append(Problem("segment_duplicate", f"{path}.segments[{j}].name",
                                 f"{seg.name!r} is listed twice in this view"))
        seen.add(key)
        if seg.revenue == 0:
            found.append(_zero(seg.name, f"{path}.segments[{j}].revenue", "revenue"))
        if seg.prior_year_revenue == 0:
            found.append(_zero(seg.name, f"{path}.segments[{j}].prior_year_revenue", "prior-year revenue"))
    total = sum(seg.revenue for seg in view.segments)
    if abs(total - rb.total_revenue) > rb.total_revenue * _TOLERANCE:
        found.append(Problem("revenue_breakdown_total", path,
                             f"the {view.dimension} buckets add to {_money(total)}, not the reported total "
                             f"{_money(rb.total_revenue)}"))
    priors = [seg.prior_year_revenue for seg in view.segments]
    if rb.prior_year_total is not None and all(p is not None for p in priors) \
            and abs(sum(priors) - rb.prior_year_total) > rb.prior_year_total * _TOLERANCE:
        found.append(Problem("revenue_breakdown_prior_total", path,
                             f"the prior-year {view.dimension} buckets add to {_money(sum(priors))}, not "
                             f"{_money(rb.prior_year_total)}"))
    return found


def check_revenue_breakdown(rb: RevenueBreakdown, financials: Financials | None) -> list[Problem]:
    found = []
    if rb.period_end > rb.as_of:
        found.append(Problem("breakdown_period_after_as_of", "revenue_breakdown.period_end",
                             f"the period ends {rb.period_end}, after the data's date {rb.as_of}"))
    for i, view in enumerate(rb.views):
        found += _view_problems(view, i, rb)
    if financials is not None:
        rows = financials.quarterly if rb.kind == "standalone" else financials.annual
        for s in rows:
            reported = s.income.revenue if s.income is not None else None
            if s.period_end == rb.period_end and reported and abs(reported - rb.total_revenue) > rb.total_revenue * _TOLERANCE:
                found.append(Problem("revenue_breakdown_vs_financials", "revenue_breakdown.total_revenue",
                                     f"the breakdown's total {_money(rb.total_revenue)} differs from the "
                                     f"financials' revenue {_money(reported)} for the period ending {rb.period_end}"))
    return found


_SHARE_TOLERANCE = 0.5     # points: callers may round a share to a whole percent


def check_theme_backdrop(t: ThemeBackdrop, trade_date: date | None) -> list[Problem]:
    found, seen = [], set()
    for i, ticker in enumerate(t.basket):
        if ticker in seen:
            found.append(Problem("basket_duplicate", f"theme_backdrop.basket[{i}]", f"{ticker} is listed twice"))
        seen.add(ticker)
    if t.basket_size is not None and t.basket_size != len(t.basket):
        found.append(Problem("basket_size_inconsistent", "theme_backdrop.basket_size",
                             f"basket_size is {t.basket_size}, but {len(t.basket)} tickers are listed"))
    if t.basket_size_used > len(t.basket):
        found.append(Problem("basket_size_inconsistent", "theme_backdrop.basket_size_used",
                             f"{t.basket_size_used} names measured, but only {len(t.basket)} are listed"))
    used = t.basket_size_used
    for name in ("below_200d_pct", "below_50d_pct", "rsi_below_30_pct"):
        pct = getattr(t, name)
        if pct is not None and abs(pct - round(pct * used / 100) / used * 100) > _SHARE_TOLERANCE:
            found.append(Problem("share_not_whole_names", f"theme_backdrop.{name}",
                                 f"{pct}% cannot be a share of {used} names: it is not a whole number of them"))
    if trade_date is not None and (trade_date - t.as_of).days > _PRICE_WINDOW_DAYS:
        found.append(Problem("theme_backdrop_stale", "theme_backdrop.as_of",
                             f"the basket is dated {t.as_of}, more than {_PRICE_WINDOW_DAYS} days before the run date {trade_date}"))
    return found


def check_context(context: Context, *, ticker: str | None = None, trade_date: date | None = None) -> list[Problem]:
    """Every problem with the context, including ties to the request when it is given."""
    found = []
    blocks = [("financials", context.financials), ("price", context.price), ("events", context.events),
              ("company", context.company), ("technicals", context.technicals), ("ownership", context.ownership),
              ("valuation_history", context.valuation_history), ("earnings_release", context.earnings_release),
              ("revenue_breakdown", context.revenue_breakdown)]
    if context.financials is not None:
        found += check_financials(context.financials)
    if context.events is not None:
        found += check_events(context.events)
    if context.company is not None:
        found += check_company(context.company, context.price)
    if context.technicals is not None:
        found += check_technicals(context.technicals, context.price, trade_date)
    if context.ownership is not None:
        found += check_ownership(context.ownership)
    if context.valuation_history is not None:
        found += check_valuation(context.valuation_history)
    if context.earnings_release is not None:
        found += check_earnings_release(context.earnings_release, trade_date)
    if context.market_backdrop is not None:
        found += check_backdrop(context.market_backdrop)
    if context.revenue_breakdown is not None:
        found += check_revenue_breakdown(context.revenue_breakdown, context.financials)
    if context.theme_backdrop is not None:
        found += check_theme_backdrop(context.theme_backdrop, trade_date)
    found += check_prior_reports(context.prior_reports, trade_date)
    if ticker is not None:
        found += [Problem("ticker_mismatch", f"prior_reports[{i}].ticker",
                          f"a previous report is for {r.ticker}, but the request is for {ticker}")
                  for i, r in enumerate(context.prior_reports) if r.ticker != ticker]
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
        for name, block in (("valuation_history", context.valuation_history),
                            ("market_backdrop", context.market_backdrop),
                            ("revenue_breakdown", context.revenue_breakdown),
                            ("theme_backdrop", context.theme_backdrop)):
            if block is not None and block.as_of > trade_date:
                found.append(Problem("as_of_after_trade_date", f"{name}.as_of",
                                     f"the {name.replace('_', ' ')} is dated {block.as_of}, after the run date {trade_date}"))
        if context.ownership is not None and context.ownership.as_of > trade_date:
            found.append(Problem("as_of_after_trade_date", "ownership.as_of",
                                 f"the ownership data is dated {context.ownership.as_of}, after the run date {trade_date}"))
        if context.company is not None and context.company.as_of > trade_date:
            found.append(Problem("as_of_after_trade_date", "company.as_of",
                                 f"the company data is dated {context.company.as_of}, after the run date {trade_date}"))
        if context.events is not None and context.events.as_of > trade_date:
            found.append(Problem("as_of_after_trade_date", "events.as_of",
                                 f"the events are dated {context.events.as_of}, after the run date {trade_date}"))
        if context.financials is not None and context.financials.as_of > trade_date:
            found.append(Problem("as_of_after_trade_date", "financials.as_of",
                                 f"the financials are dated {context.financials.as_of}, after the run date {trade_date}"))
    return found
