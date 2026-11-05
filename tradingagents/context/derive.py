"""Metrics computed from the caller's data, in code, never by the model.

A metric that its inputs cannot support is left out. There is no debt figure in
the schema, so there is no debt-to-equity: only liabilities to equity.
"""

from __future__ import annotations

from tradingagents.context.models import Financials, Price


def _pct(part: float | None, whole: float | None) -> float | None:
    return None if part is None or not whole or whole <= 0 else part / whole * 100


def _ttm_revenue(f: Financials) -> float | None:
    quarters = sorted((q for q in f.quarterly if q.kind == "standalone" and q.income
                       and q.income.revenue is not None), key=lambda q: q.period_end)[-4:]
    gaps = [(b.period_end - a.period_end).days for a, b in zip(quarters, quarters[1:], strict=False)]
    if len(quarters) < 4 or not all(60 <= g <= 120 for g in gaps):
        return None
    return sum(q.income.revenue for q in quarters)


def _growth(f: Financials) -> float | None:
    years = sorted((s for s in f.annual if s.income and s.income.revenue), key=lambda s: s.period_end)
    if len(years) < 2:
        return None
    prior, latest = years[-2], years[-1]
    if not 300 <= (latest.period_end - prior.period_end).days <= 400:
        return None
    return (latest.income.revenue / prior.income.revenue - 1) * 100


def _balance_metrics(f: Financials) -> dict[str, float | None]:
    b = max(f.balance, key=lambda b: b.as_of, default=None)
    if b is None:
        return {}
    equity = b.shareholders_equity
    both = b.current_assets is not None and b.current_liabilities is not None
    return {
        "liabilities_to_equity": (b.total_liabilities / equity
                                  if b.total_liabilities is not None and equity and equity > 0 else None),
        "working_capital": b.current_assets - b.current_liabilities if both else None,
    }


def derive_metrics(f: Financials, price: Price | None = None) -> dict[str, float]:
    latest = max(f.annual, key=lambda s: s.period_end, default=None)
    income = latest.income if latest else None
    cash = latest.cash_flow if latest else None
    revenue = income.revenue if income else None
    capex = abs(cash.capital_expenditure) if cash and cash.capital_expenditure is not None else None
    eps = income.eps_diluted if income else None
    metrics = {
        "gross_margin_pct": _pct(income.gross_profit if income else None, revenue),
        "operating_margin_pct": _pct(income.operating_income if income else None, revenue),
        "net_margin_pct": _pct(income.net_income if income else None, revenue),
        "fcf_margin_pct": _pct(cash.free_cash_flow if cash else None, revenue),
        "capex_to_revenue_pct": _pct(capex, revenue),
        "ttm_revenue": _ttm_revenue(f),
        "revenue_growth_yoy_pct": _growth(f),
        "pe_latest_annual": price.close / eps if price is not None and eps and eps > 0 else None,
        **_balance_metrics(f),
    }
    return {name: value for name, value in metrics.items() if value is not None}
