"""Metrics computed from the caller's data, in code, never by the model.

A metric that its inputs cannot support is left out. There is no debt figure in
the schema, so there is no debt-to-equity: only liabilities to equity.
"""

from __future__ import annotations

from tradingagents.context.models import (
    Company,
    Financials,
    Ownership,
    Price,
    PriorReport,
    Technicals,
)


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


def _market_cap(price: Price | None, company: Company | None) -> float | None:
    if company is None:
        return None
    if company.market_cap:
        return float(company.market_cap)
    if price is not None and company.shares_outstanding:
        return price.close * company.shares_outstanding
    return None


def _leverage(f: Financials, cap: float | None, ebitda: float | None) -> dict[str, float | None]:
    b = max(f.balance, key=lambda b: b.as_of, default=None)
    if b is None or b.total_debt is None:
        return {}
    net_debt = b.total_debt - b.cash_and_equivalents if b.cash_and_equivalents is not None else None
    ev = cap + net_debt if cap is not None and net_debt is not None else None
    return {
        "net_debt": net_debt, "enterprise_value": ev,
        "ev_to_ebitda": ev / ebitda if ev is not None and ebitda and ebitda > 0 else None,
        "debt_to_equity": b.total_debt / b.shareholders_equity
        if b.shareholders_equity and b.shareholders_equity > 0 else None,
    }


def derive_metrics(f: Financials, price: Price | None = None, company: Company | None = None) -> dict[str, float]:
    latest = max(f.annual, key=lambda s: s.period_end, default=None)
    income = latest.income if latest else None
    cash = latest.cash_flow if latest else None
    revenue = income.revenue if income else None
    capex = abs(cash.capital_expenditure) if cash and cash.capital_expenditure is not None else None
    eps = income.eps_diluted if income else None
    cap = _market_cap(price, company)
    da = income.depreciation_and_amortization if income else None
    ebitda = income.operating_income + da if income and income.operating_income is not None and da is not None else None
    metrics = {
        "gross_margin_pct": _pct(income.gross_profit if income else None, revenue),
        "operating_margin_pct": _pct(income.operating_income if income else None, revenue),
        "net_margin_pct": _pct(income.net_income if income else None, revenue),
        "fcf_margin_pct": _pct(cash.free_cash_flow if cash else None, revenue),
        "capex_to_revenue_pct": _pct(capex, revenue),
        "ttm_revenue": _ttm_revenue(f),
        "revenue_growth_yoy_pct": _growth(f),
        "pe_latest_annual": price.close / eps if price is not None and eps and eps > 0 else None,
        "fcf_yield_pct": _pct(cash.free_cash_flow if cash else None, cap),
        **_balance_metrics(f),
        **_leverage(f, cap, ebitda),
    }
    return {name: value for name, value in metrics.items() if value is not None}


def derive_technicals(t: Technicals) -> dict[str, float]:
    """How far the close sits from each level, and today's volume against its average, in code."""
    def away(level: float | None) -> float | None:
        return (t.close / level - 1) * 100 if level else None

    metrics = {
        "pct_vs_sma_20": away(t.sma_20), "pct_vs_sma_50": away(t.sma_50), "pct_vs_sma_200": away(t.sma_200),
        "pct_from_high_52w": away(t.high_52w),
        "volume_vs_average": t.volume / t.volume_avg_50d if t.volume is not None and t.volume_avg_50d else None,
    }
    return {name: value for name, value in metrics.items() if value is not None}


def derive_ownership(o: Ownership) -> dict[str, int]:
    """Counts the report may state, taken from the lists in code. Fund counts cover the listed holders only."""
    out: dict[str, int] = {}
    if o.funds is not None:
        out["funds_listed"] = len(o.funds.holders)
        out["funds_managers_tracked"] = o.funds.managers_tracked
        for action in ("new", "added", "trimmed", "exited", "unchanged"):
            out[f"funds_{action}"] = sum(h.action == action for h in o.funds.holders)
    if o.insiders is not None:
        out.update(insider_buys=o.insiders.buys, insider_sells=o.insiders.sells,
                   insider_window_days=o.insiders.window_days)
        if o.insiders.net_value_usd is not None:
            out["insider_net_value"] = o.insiders.net_value_usd
    if o.congress is not None:
        out.update(congress_buys=o.congress.buys, congress_sells=o.congress.sells,
                   congress_window_days=o.congress.window_days)
    return out


def derive_prior_reports(reports: list[PriorReport], current_price: float | None) -> list[dict]:
    """How the price has moved since each earlier report, and whether that is toward its target."""
    from tradingagents.rating_label import plain_rating

    rows = []
    for r in reports:
        plain = plain_rating(r.decision)
        row = {"as_of": r.as_of, "decision_label": plain.label if plain else r.decision, "target": r.target,
               "price_then": r.price_then, "horizon": r.horizon, "price_change_pct": None,
               "direction_to_target": None, "progress_pct": None}
        if current_price is not None:
            change = current_price - r.price_then
            row["price_change_pct"] = (current_price / r.price_then - 1) * 100
            gap = (r.target - r.price_then) if r.target is not None else 0
            if gap:
                progress = change / gap
                row["direction_to_target"] = "reached" if progress >= 1 else "toward" if progress > 0 else "away"
                row["progress_pct"] = max(progress, 0.0) * 100
        rows.append(row)
    return rows
