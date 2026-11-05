"""The caller's context as the block every agent reads."""

from __future__ import annotations

import re

from tradingagents.context.derive import derive_metrics, derive_technicals
from tradingagents.context.models import (
    Balance,
    Company,
    Context,
    Events,
    Financials,
    Price,
    Statement,
    Technicals,
)

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
    ("net_debt", "net debt", None),
    ("enterprise_value", "enterprise value", None),
    ("ev_to_ebitda", "EV to EBITDA", "{:.1f}x"),
    ("debt_to_equity", "debt to equity", "{:.2f}"),
    ("fcf_yield_pct", "free cash flow yield", "{:.1f}%"),
    ("pe_latest_annual", "price-to-earnings (price over latest annual diluted EPS)", "{:.1f}"),
    ("ttm_revenue", "trailing four-quarter revenue", None),
)
_TAGS = re.compile(r"</?notes>", re.IGNORECASE)


def _money(value: float) -> str:
    sign, value = ("-" if value < 0 else ""), abs(value)
    return f"{sign}${value / 1e9:,.3f}B" if value >= 1e9 else f"{sign}${value / 1e6:,.1f}M"


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


def _technicals(t: Technicals) -> list[str]:
    parts = [f"close ${t.close:,.2f}"]
    for label, value in (("20-day average", t.sma_20), ("50-day average", t.sma_50),
                         ("200-day average", t.sma_200), ("10-day EMA", t.ema_10)):
        if value is not None:
            parts.append(f"{label} ${value:,.2f}")
    if t.rsi_14 is not None:
        parts.append(f"RSI(14) {t.rsi_14:g}")
    if t.macd is not None and None not in (t.macd.line, t.macd.signal, t.macd.histogram):
        parts.append(f"MACD line {t.macd.line:g}, signal {t.macd.signal:g}, histogram {t.macd.histogram:g}")
    if t.volume is not None and t.volume_avg_50d is not None:
        parts.append(f"volume {t.volume:,} against a 50-day average of {t.volume_avg_50d:,}")
    for label, value in (("52-week high", t.high_52w), ("52-week low", t.low_52w)):
        if value is not None:
            parts.append(f"{label} ${value:,.2f}")
    lines = [f"Technicals for {t.ticker} (source {t.source}, as of {t.as_of}): " + "; ".join(parts) + "."]
    m, away = derive_technicals(t), []
    for key, label in (("pct_vs_sma_20", "the 20-day average"), ("pct_vs_sma_50", "the 50-day average"),
                       ("pct_vs_sma_200", "the 200-day average"), ("pct_from_high_52w", "the 52-week high")):
        if key in m:
            away.append(f"{abs(m[key]):.1f}% {'below' if m[key] < 0 else 'above'} {label}")
    if "volume_vs_average" in m:
        away.append(f"volume {m['volume_vs_average']:.2f}x its 50-day average")
    if away:
        lines.append("Computed distances: the close is " + ", ".join(away) + ".")
    lines.append("These are the levels the customer's chart shows. Use only these levels; there are no other "
                 "levels to cite as support or resistance.")
    return lines


def _company(c: Company) -> list[str]:
    name = " ".join(c.name.split()) if c.name else c.ticker
    where = ", ".join(" ".join(x.split()) for x in (c.exchange, c.sector, c.industry) if x)
    facts = [f"market cap {_money(c.market_cap)}"] if c.market_cap else []
    facts += [f"shares outstanding {c.shares_outstanding:,}"] if c.shares_outstanding else []
    detail = "; ".join(x for x in (where, *facts) if x)
    return [f"Company: {name} ({c.ticker}){': ' + detail if detail else ''} (source {c.source}, as of {c.as_of})."]


def _financials(f: Financials, price: Price | None, company: Company | None = None) -> list[str]:
    lines = [f"Financials for {f.ticker} (as of {f.as_of}; amounts in {f.currency}):"]
    for title, rows in (("Annual", f.annual), ("Quarterly", f.quarterly)):
        if rows:
            lines += [f"{title}:", *(_statement(s) for s in sorted(rows, key=lambda s: s.period_end))]
    if f.balance:
        lines += ["Balance sheets:", *(_balance(b) for b in sorted(f.balance, key=lambda b: b.as_of))]
    computed = _computed_metrics(f, price, company)
    if computed:
        lines.append(computed)
    if "debt_to_equity" not in derive_metrics(f, price, company):
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


def _price_line(p: Price) -> str:
    return f"Verified price for {p.ticker}: close ${p.close:,.2f} {p.currency} on {p.as_of} (source {p.source})."


def _notes_lines(notes: str) -> list[str]:
    text = " ".join(_TAGS.sub(" ", notes).split())
    return ["Caller notes (unverified; context only, never a source for any figure above):", f"<notes>{text}</notes>"]


def _computed_metrics(f: Financials, price: Price | None, company: Company | None) -> str | None:
    metrics = derive_metrics(f, price, company)
    shown = [f"{label} " + (fmt.format(metrics[key]) if fmt else _money(metrics[key]))
             for key, label, fmt in _METRICS if key in metrics]
    return "Computed metrics: " + "; ".join(shown) + "." if shown else None


def _digest(context: Context) -> list[str]:
    """The headline facts, so the agents that weigh everything agree with the analysts' numbers."""
    lines = ["Key verified facts (computed and checked by the service; use them as given):"]
    if context.price is not None:
        lines.append(_price_line(context.price))
    if context.company is not None and context.company.market_cap:
        lines.append(f"Market cap {_money(context.company.market_cap)}.")
    if context.financials is not None:
        metrics = _computed_metrics(context.financials, context.price, context.company)
        lines += [metrics] if metrics else []
    if context.technicals is not None:
        distances = [line for line in _technicals(context.technicals) if line.startswith("Computed distances")]
        lines += distances
    if context.events is not None and context.events.next_earnings is not None:
        nxt = context.events.next_earnings
        lines.append(f"Next earnings: {nxt.date} ({nxt.fiscal_period}, "
                     f"{'confirmed' if nxt.confirmed else 'not confirmed'}); the only date you may give.")
    return lines if len(lines) > 1 else []


def render_role_blocks(context: Context) -> dict[str, str]:
    """What each kind of agent reads: only the blocks it uses, and a short digest for the deciders.

    Every call an agent makes resends its block, so the full context is not given to all of them.
    The sentiment analyst reads none. A role with nothing to read is left out.
    """
    notes = _notes_lines(context.notes) if context.notes else []
    c, plan = context, {}
    fin = _financials(c.financials, c.price, c.company) if c.financials else []
    price = [_price_line(c.price)] if c.price else []
    plan["market"] = [*(_technicals(c.technicals) if c.technicals else []), *price]
    plan["news"] = _events(c.events) if c.events else []
    plan["fundamentals"] = [*fin, *(_company(c.company) if c.company else []),
                            *(_events(c.events) if c.events else []), *price, *notes]
    plan["decision"] = [*_digest(c), *notes] if _digest(c) else []
    return {role: "\n".join([_GUIDANCE, "", *lines]) for role, lines in plan.items() if lines}


def render_context(context: Context) -> str:
    lines = [_GUIDANCE]
    if context.financials is not None:
        lines += ["", *_financials(context.financials, context.price, context.company)]
    if context.technicals is not None:
        lines += ["", *_technicals(context.technicals)]
    if context.company is not None:
        lines += ["", *_company(context.company)]
    if context.events is not None:
        lines += ["", *_events(context.events)]
    if context.price is not None:
        lines += ["", _price_line(context.price)]
    if context.notes:
        lines += ["", *_notes_lines(context.notes)]
    return "\n".join(lines)
