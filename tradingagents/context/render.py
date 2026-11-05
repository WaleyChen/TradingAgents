"""The caller's context as the block every agent reads."""

from __future__ import annotations

import re

from tradingagents.context.derive import derive_metrics
from tradingagents.context.models import Balance, Context, Events, Financials, Price, Statement

_GUIDANCE = (
    "Verified data supplied by the caller. The service checked these statements for "
    "consistent periods and totals, and each names its source. For these figures use this "
    "data over anything your tools return, and say where they differ. Cite each figure by "
    "its source and period. Metrics are computed from the values by the service; use them as given."
)
_METRICS = (
    ("gross_margin_pct", "gross margin", "{:.1f}%"),
    ("operating_margin_pct", "operating margin", "{:.1f}%"),
    ("net_margin_pct", "net margin", "{:.1f}%"),
    ("fcf_margin_pct", "free cash flow margin", "{:.1f}%"),
    ("capex_to_revenue_pct", "capex to revenue", "{:.1f}%"),
    ("revenue_growth_yoy_pct", "revenue growth year over year", "{:.1f}%"),
    ("liabilities_to_equity", "liabilities to equity", "{:.2f}"),
    ("working_capital", "working capital", None),
    ("pe_latest_annual", "price-to-earnings (price over latest annual diluted EPS)", "{:.1f}"),
    ("ttm_revenue", "trailing four-quarter revenue", None),
)
_TAGS = re.compile(r"</?notes>", re.IGNORECASE)


def _money(value: float) -> str:
    return f"${value / 1e9:,.3f}B" if abs(value) >= 1e9 else f"${value / 1e6:,.1f}M"


def _fields(item, names: tuple[tuple[str, str], ...]) -> list[str]:
    return [f"{label} {_money(getattr(item, name))}" for name, label in names
            if item is not None and getattr(item, name) is not None]


def _statement(s: Statement) -> str:
    span = f"{s.period_start} to {s.period_end}" if s.period_start else f"period ending {s.period_end}"
    filed = f"; filed {s.filed_at}" if s.filed_at else ""
    values = _fields(s.income, (("revenue", "revenue"), ("gross_profit", "gross profit"),
                                ("operating_income", "operating income"), ("net_income", "net income")))
    if s.income is not None:
        values += [f"{label} ${getattr(s.income, name):,.2f}" for name, label in
                   (("eps_basic", "basic EPS"), ("eps_diluted", "diluted EPS"))
                   if getattr(s.income, name) is not None]
    values += _fields(s.cash_flow, (("net_cash_flow_from_operations", "operating cash flow"),
                                    ("capital_expenditure", "capex"), ("free_cash_flow", "free cash flow")))
    return f"- {s.fiscal_period} ({span}; {s.kind}; source {s.source}{filed}): " + "; ".join(values)


def _balance(b: Balance) -> str:
    values = _fields(b, (("total_assets", "total assets"), ("total_liabilities", "total liabilities"),
                         ("shareholders_equity", "shareholders' equity"),
                         ("cash_and_equivalents", "cash and equivalents"),
                         ("current_assets", "current assets"), ("current_liabilities", "current liabilities")))
    return f"- {b.fiscal_period} as of {b.as_of} (source {b.source}): " + "; ".join(values)


def _financials(f: Financials, price: Price | None) -> list[str]:
    lines = [f"Financials for {f.ticker} (as of {f.as_of}; amounts in {f.currency}):"]
    for title, rows in (("Annual", f.annual), ("Quarterly", f.quarterly)):
        if rows:
            lines += [f"{title}:", *(_statement(s) for s in sorted(rows, key=lambda s: s.period_end))]
    if f.balance:
        lines += ["Balance sheets:", *(_balance(b) for b in sorted(f.balance, key=lambda b: b.as_of))]
    metrics = derive_metrics(f, price)
    shown = [f"{label} " + (fmt.format(metrics[key]) if fmt else _money(metrics[key]))
             for key, label, fmt in _METRICS if key in metrics]
    if shown:
        lines.append("Computed metrics: " + "; ".join(shown) + ".")
    lines.append("There is no debt figure, so do not calculate debt-to-equity from liabilities.")
    return lines


def _events(e: Events) -> list[str]:
    lines = [f"Events for {e.ticker} (source {e.source}, as of {e.as_of}):"]
    nxt = e.next_earnings
    if nxt is None:
        lines.append("Next earnings: not supplied. Do not state or guess an earnings date.")
    else:
        state = "confirmed" if nxt.confirmed else "not confirmed"
        lines.append(f"Next earnings: {nxt.date} ({nxt.fiscal_period}, {state}). This is the only date "
                     "you may give for the next earnings report.")
    for r in e.recent_earnings:
        values = []
        for name, label, money in (("eps_actual", "EPS", False), ("revenue_actual", "revenue", True)):
            actual = getattr(r, name)
            estimate = getattr(r, name.replace("actual", "estimate"))
            if actual is None:
                continue
            shown = _money(actual) if money else f"${actual:,.2f}"
            est = (_money(estimate) if money else f"${estimate:,.2f}") if estimate is not None else None
            values.append(f"{label} {shown}" + (f" against an estimate of {est}" if est else ""))
        missing = ("estimates not supplied" if r.eps_estimate is None and r.revenue_estimate is None else "")
        detail = "; ".join([*values, *([missing] if missing else [])])
        lines.append(f"- {r.fiscal_period} (period ended {r.period_end}, reported {r.reported_on}): {detail}")
    return lines


def render_context(context: Context) -> str:
    lines = [_GUIDANCE]
    if context.financials is not None:
        lines += ["", *_financials(context.financials, context.price)]
    if context.events is not None:
        lines += ["", *_events(context.events)]
    if context.price is not None:
        p = context.price
        lines += ["", f"Verified price for {p.ticker}: close ${p.close:,.2f} {p.currency} on {p.as_of} (source {p.source})."]
    if context.notes:
        notes = " ".join(_TAGS.sub(" ", context.notes).split())
        lines += ["", "Caller notes (unverified; context only, never a source for any figure above):",
                  f"<notes>{notes}</notes>"]
    return "\n".join(lines)
