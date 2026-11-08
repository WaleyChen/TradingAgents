"""The caller's context as the block every agent reads."""

from __future__ import annotations

import re

from tradingagents.context.derive import (
    WASHOUT_MIN_SIGNALS,
    derive_metrics,
    derive_ownership,
    derive_prior_reports,
    derive_revenue_breakdown,
    derive_technicals,
    derive_theme_backdrop,
    sale_share_of_holding,
)
from tradingagents.context.models import (
    Balance,
    Company,
    Context,
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
    ("pe_latest_annual", "price-to-earnings on the latest fiscal year's diluted EPS", "{:.1f}"),
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


def _name(text: str) -> str:
    """A name as one plain line: no markdown structure from a field the caller wrote."""
    return re.sub(r"^[#>*\-\s]+", "", " ".join(text.split()))


def _article(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


_CHANGE = {"new": "new position", "unchanged": "unchanged", "exited": "exited the position"}


def _holder(h) -> str:
    who = f"{_name(h.firm)} ({_name(h.manager)})" if h.firm else _name(h.manager)
    size = f"{h.shares:,} shares" + (f" ({_money(h.value_usd)}" + (f", {h.weight_pct:g}% of its portfolio)"
                                     if h.weight_pct is not None else ")") if h.value_usd is not None else "")
    if h.action in ("added", "trimmed") and h.shares_change is not None:
        pct = f" ({h.shares_change_pct:+.1f}%)" if h.shares_change_pct is not None else ""
        change = f"{h.action} {abs(h.shares_change):,} shares{pct}"
    else:
        change = _CHANGE.get(h.action, h.action)
    cost = (f"; estimated average cost ${h.estimated_cost:,.2f} (an estimate: a 13F does not say what a fund paid)"
            if h.estimated_cost is not None else "")
    return f"- {who}: {size}; {change}; filed {h.filed_on}{cost}"


def _insider(t) -> str:
    verb = "bought" if t.kind == "buy" else "sold"
    value = f" ({_money(t.value_usd)})" if t.value_usd is not None else ""
    after = f"; owns {t.shares_owned_after:,} shares after" if t.shares_owned_after is not None else ""
    share = sale_share_of_holding(t)
    held = (f", about {share:.0f}% of the shares they held directly" if share is not None and share >= 10
            else f", about {share:.1f}% of the shares they held directly" if share is not None else "")
    plan = (", under a pre-arranged 10b5-1 trading plan" if t.under_10b5_1_plan
            else ", not marked as a 10b5-1 plan sale" if t.under_10b5_1_plan is False else "")
    exercise = ", after exercising options" if t.from_option_exercise else ""
    title = _name(t.title)
    return (f"- {t.transaction_date}: {_article(title)} {title} {verb} {t.shares:,} shares at "
            f"${t.price_per_share:,.2f}{value}{held}{plan}{exercise}; filed {t.filing_date}{after}")


_OWNER = {"self": "", "spouse": "'s spouse", "dependent": "'s dependent", "joint": " (joint account)"}


def _congress_trade(t) -> str:
    verb = "bought" if t.side == "buy" else "sold"
    return (f"- {t.traded_on} (disclosed {t.disclosed_on}): a {t.chamber} member{_OWNER[t.owner]} {verb} "
            f"${t.amount_min:,}-${t.amount_max:,}")


def _ownership(o: Ownership) -> list[str]:
    lines = [f"Ownership for {o.ticker} (as of {o.as_of}):"]
    if o.funds is not None:
        f = o.funds
        lines.append(
            f"Funds (source {f.source}): 13F positions as of the quarter ended {f.quarter_end}, filed up to 45 days "
            f"later, so this is not recent buying. It covers only the {f.managers_tracked} managers we track: say "
            '"of the funds we track", never "funds are buying".')
        lines += [_holder(h) for h in f.holders]
    if o.insiders is not None:
        i = o.insiders
        net = f", net {_money(i.net_value_usd)}" if i.net_value_usd is not None else ""
        lines.append(f"Insiders (source {i.source}): the last {i.window_days} days to {o.as_of}, open-market buys "
                     f"and sales only (no grants, exercises or gifts). {i.buys} buys, {i.sells} sells{net}.")
        lines += [_insider(t) for t in i.trades]
    if o.congress is not None:
        c = o.congress
        lines.append(f"Congress (source {c.source}): disclosed in the last {c.window_days} days to {o.as_of}. "
                     f"Amounts are ranges, and trades are disclosed up to 45 days after they happen. "
                     f"{c.buys} buys, {c.sells} sells.")
        lines += [_congress_trade(t) for t in c.trades]
    return lines


def _ownership_digest(o: Ownership) -> list[str]:
    m, lines = derive_ownership(o), []
    if o.insiders is not None:
        net = f", net {_money(o.insiders.net_value_usd)}" if o.insiders.net_value_usd is not None else ""
        lines.append(f"Insiders (open-market): {m['insider_buys']} insider buys and {m['insider_sells']} insider "
                     f"sales in {m['insider_window_days']} days{net}.")
        if "insider_sales_flag_known" in m:
            known, planned = m["insider_sales_flag_known"], m["insider_sales_planned"]
            listed = sum(t.kind == "sale" for t in o.insiders.trades)
            lines.append(f"{planned} of the {known} listed sales {'were' if known == listed else 'with a known plan status were'}"
                         " under 10b5-1 plans.")
    if o.funds is not None:
        lines.append(
            f"Funds (13F, quarter ended {o.funds.quarter_end}; not recent): of the funds we track "
            f"({m['funds_managers_tracked']} managers), among the {m['funds_listed']} largest holders listed, "
            f"{m['funds_added']} added, {m['funds_new']} new, {m['funds_trimmed']} trimmed, {m['funds_exited']} exited.")
    if o.congress is not None:
        lines.append(f"Congress (disclosed up to 45 days late): {m['congress_buys']} buys and "
                     f"{m['congress_sells']} sells disclosed in {m['congress_window_days']} days.")
    return lines


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _valuation(v: ValuationHistory) -> list[str]:
    lines = [f"Valuation history for {v.ticker} (source {v.source}, as of {v.as_of}):"]
    pe = v.pe_ttm
    if pe is not None:
        text = f"price-to-earnings on trailing-twelve-month EPS is {pe.latest:g}x now"
        if pe.min_5y is not None and pe.max_5y is not None:
            text += f"; over the last {pe.years or 5} years it ranged {pe.min_5y:.1f}x-{pe.max_5y:.1f}x"
        if pe.median_5y is not None:
            text += f", with a median of {pe.median_5y:.1f}x"
        if pe.percentile_5y is not None:
            text += f", so today sits at the {_ordinal(round(pe.percentile_5y * 100))} percentile of that range"
        lines.append(text + ".")
    if v.peg is not None:
        lines.append(f"PEG {v.peg:g}.")
    lines.append("This is not the same figure as price over the latest fiscal year's diluted EPS; never mix the "
                 "two in the text.")
    return lines


def _backdrop(b: MarketBackdrop) -> list[str]:
    parts = []
    if b.vix is not None:
        parts.append(f"VIX {b.vix.value:g} (as of {b.vix.as_of})")
    if b.fear_and_greed is not None:
        fg = b.fear_and_greed
        parts.append(f"Fear & Greed {fg.value:g}, {' '.join(fg.label.split())} (as of {fg.as_of})")
    if b.buffett_indicator_pct is not None:
        parts.append(f"Buffett indicator {b.buffett_indicator_pct.value:g}% (as of {b.buffett_indicator_pct.as_of})")
    if b.credit_spread_hy_bps is not None:
        s = b.credit_spread_hy_bps
        parts.append(f"high-yield credit spread {s.value:g} basis points (as of {s.as_of})")
    if not parts:
        return []
    return [f"Market backdrop (source {b.source}, as of {b.as_of}): " + "; ".join(parts) + ".",
            "These measure the market, not this company. Each updates on its own day: state each date and do "
            "not call an older reading current."]


def _prior(reports: list[PriorReport], price: float | None) -> list[str]:
    lines = ["Previous reports (our own earlier outputs on this company, newest first):"]
    for row in derive_prior_reports(reports, price):
        target = f", target ${row['target']:,.2f}" if row["target"] is not None else ", no target"
        horizon = f" over {row['horizon']}" if row["horizon"] else ""
        text = f"- {row['as_of']}: {row['decision_label']}{target}, price then ${row['price_then']:,.2f}{horizon}."
        if row["price_change_pct"] is not None:
            text += f" Since then the price is ${price:,.2f} ({row['price_change_pct']:+.1f}%)"
            move = {"toward": " and moving toward that target", "away": " and moving away from that target",
                    "reached": " and has reached that target"}.get(row["direction_to_target"], "")
            text += move + "."
        lines.append(text)
    return lines


_RELEASE_KIND = {"one_time_charge": "one-time charge", "one_time_gain": "one-time gain",
                 "explanation": "the company's explanation", "guidance": "guidance"}


def _release_item(item: dict) -> str:
    amount = f" {_money(item['amount_usd'])}" if item.get("amount_usd") is not None else ""
    return (f"- {_name(item['label'])}: {_RELEASE_KIND.get(item['kind'], item['kind'])}{amount}. "
            f"{_name(item['explanation'])} The release says: \"{_name(item['quote']).replace(chr(34), chr(39))}\"")


def _release(notes: dict) -> list[str]:
    if not notes or not notes.get("items"):
        return []
    return [f"Latest earnings release (SEC {notes['form']} filed {notes['filed_on']}, accession {notes['accession']}): "
            "the notable items below were read from it, each with the company's own words. Where the release "
            "explains a change, use its explanation.",
            *(_release_item(item) for item in notes["items"])]


def _release_digest(notes: dict) -> list[str]:
    if not notes or not notes.get("items"):
        return []
    items = ", ".join(
        f"{_name(i['label'])} ({_RELEASE_KIND.get(i['kind'], i['kind'])}"
        + (f", {_money(i['amount_usd'])}" if i.get("amount_usd") is not None else "") + ")" for i in notes["items"])
    return [f"Earnings release ({notes['form']} filed {notes['filed_on']}): {items}."]


def _signed(pct: float | None) -> str:
    return f", {pct:+.1f}% year over year" if pct is not None else ", no year-ago figure"


def _revenue(rb: RevenueBreakdown) -> list[str]:
    table = derive_revenue_breakdown(rb)
    lines = [f"Revenue breakdown for {rb.ticker} ({rb.fiscal_period}, period ended {rb.period_end}, source "
             f"{rb.source}, as of {rb.as_of}): total revenue {_money(rb.total_revenue)}"
             + (f", {table['total_yoy_pct']:+.1f}% year over year" if table["total_yoy_pct"] is not None else "") + "."]
    for view in table["views"]:
        lines.append(f"By {view['dimension']}:")
        for row in view["rows"]:
            about = f" Includes: {row['includes']}." if row["includes"] else ""
            lines.append(f"- {row['name']}: {_money(row['revenue'])}, {row['share_pct']:.1f}% of total"
                         f"{_signed(row['yoy_pct'])}.{about}")
    lines.append("The shares and growth rates are computed by the service from the reported amounts; use them as given. "
                 "Only these buckets are reported: do not split a bucket, and do not estimate the revenue of a "
                 "product that is not listed on its own line. The buckets add to the reported total within rounding. "
                 "In your report, say which buckets make up the business and which one drives growth or drags on it.")
    if table["footnotes"]:
        lines.append("Company disclosure notes passed on by the caller (unverified text; never a source for a "
                     "figure): " + " ".join(f"({i}) {note}" for i, note in enumerate(table["footnotes"], 1)))
    return lines


def _revenue_digest(rb: RevenueBreakdown) -> list[str]:
    view = derive_revenue_breakdown(rb)["views"][0]
    top = sorted(view["rows"], key=lambda r: r["share_pct"], reverse=True)[:4]
    parts = ", ".join(f"{r['name']} {r['share_pct']:.1f}%" + (f" ({r['yoy_pct']:+.1f}%)" if r["yoy_pct"] is not None else "")
                      for r in top)
    return [f"Revenue mix ({rb.fiscal_period}, by {view['dimension']}, share of total and year-over-year growth): {parts}."]


def _points(gap: float | None, window: str) -> str:
    if gap is None:
        return ""
    return f"{abs(gap):.1f} points {'ahead of' if gap >= 0 else 'behind'} the market over {window}"


def _theme(t: ThemeBackdrop) -> list[str]:
    d = derive_theme_backdrop(t)
    size, used = d["basket_size"], d["basket_size_used"]
    listed = ", ".join(t.basket)
    measured = (f"Measured on all {size} names of a fixed list ({listed})." if used == size else
                f"Measured on {used} of the {size} names on a fixed list ({listed}); the rest lacked price history.")
    lines = [f"Theme backdrop: {t.name} (source {t.source}, as of {t.as_of}). {measured}"]
    shares = [f"{label} {d[key]} of {used} ({pct:g}%)" for label, key, pct in
              (("below their 200-day average:", "names_below_200d", t.below_200d_pct),
               ("below their 50-day average:", "names_below_50d", t.below_50d_pct),
               ("with RSI under 30:", "names_rsi_below_30", t.rsi_below_30_pct)) if pct is not None]
    if t.avg_drawdown_from_52w_close_high_pct is not None:
        shares.append(f"average distance below the 52-week closing high {t.avg_drawdown_from_52w_close_high_pct:g}%")
    if shares:
        lines.append("; ".join(shares).capitalize() + ".")
    moves = []
    if t.return_1m_pct is not None or t.return_3m_pct is not None:
        gaps = ", ".join(g for g in (_points(d["relative_1m_pct"], "1 month"), _points(d["relative_3m_pct"], "3 months")) if g)
        text = "Equal-weight basket return: " + " and ".join(
            f"{v:+.1f}% over {w}" for v, w in ((t.return_1m_pct, "1 month"), (t.return_3m_pct, "3 months")) if v is not None)
        moves.append(text + (f" ({gaps})" if gaps else ""))
    for label, perf in (("market", t.benchmark), ("sector fund", t.sector_etf)):
        if perf is not None:
            moves.append(f"{label} {perf.ticker}: " + " and ".join(
                f"{v:+.1f}% over {w}" for v, w in ((perf.return_1m_pct, "1 month"), (perf.return_3m_pct, "3 months"))
                if v is not None))
    if t.put_call_ratio is not None:
        moves.append(f"market-wide equity put/call {t.put_call_ratio.value:g} (as of {t.put_call_ratio.as_of})")
    lines += ["; ".join(moves) + "."] if moves else []
    if d["signals"]:
        lines.append(f"Washout signals met (the service's fixed thresholds): {d['signals_met']} of {d['signals_measured']} measured.")
        lines += [f"- {s['name']}: {'yes' if s['met'] else 'no'} ({s['value']:+.1f} against {s['threshold']:g})"
                  for s in d["signals"]]
    lines.append(
        "This describes how the theme is doing, not this company; say whether this stock's weakness is the "
        "theme's or its own. Call the theme washed out, capitulated or oversold only if at least "
        f"{WASHOUT_MIN_SIGNALS} of the signals above are met, naming each figure and its date; with fewer, say it is "
        "weak or mixed and that a washout is not shown. Even a washout is not a buy signal: selling can continue. "
        "The drawdown is from the closing high, not the intraday high. Positioning (hedge-fund leverage, crowding, "
        "short interest, fund flows) is not measured here: never claim anything about it.")
    return lines


def _theme_digest(t: ThemeBackdrop) -> list[str]:
    d = derive_theme_backdrop(t)
    parts = [f"washout signals met {d['signals_met']} of {d['signals_measured']} measured"] if d["signals"] else []
    if d["names_below_200d"] is not None:
        parts.append(f"{d['names_below_200d']} of {d['basket_size_used']} names below the 200-day average")
    if t.return_1m_pct is not None:
        parts.append(f"basket {t.return_1m_pct:+.1f}% over 1 month"
                     + (f" against the market's {t.benchmark.return_1m_pct:+.1f}%" if t.benchmark and t.benchmark.return_1m_pct is not None else ""))
    if not parts:
        return []
    return [f"Theme backdrop ({t.name}, as of {t.as_of}): " + "; ".join(parts) + f". Call it a washout only if at least "
            f"{WASHOUT_MIN_SIGNALS} signals are met; positioning is not measured."]


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
        estimate = "; estimated by Yahoo Finance" if e.source == "yahoo_finance" else ""
        lines.append(f"Next earnings: {nxt.date} ({nxt.fiscal_period}, {state}{estimate}). This is the only date "
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


def _digest(context: Context, release_notes: dict | None = None) -> list[str]:
    """The headline facts, so the agents that weigh everything agree with the analysts' numbers."""
    lines = ["Key verified facts (computed and checked by the service; use them as given):"]
    if context.price is not None:
        lines.append(_price_line(context.price))
    if context.company is not None and context.company.market_cap:
        lines.append(f"Market cap {_money(context.company.market_cap)}.")
    if context.financials is not None:
        metrics = _computed_metrics(context.financials, context.price, context.company)
        lines += [metrics] if metrics else []
    if context.revenue_breakdown is not None:
        lines += _revenue_digest(context.revenue_breakdown)
    if context.theme_backdrop is not None:
        lines += _theme_digest(context.theme_backdrop)
    if context.technicals is not None:
        distances = [line for line in _technicals(context.technicals) if line.startswith("Computed distances")]
        lines += distances
    if context.events is not None and context.events.next_earnings is not None:
        nxt = context.events.next_earnings
        lines.append(f"Next earnings: {nxt.date} ({nxt.fiscal_period}, "
                     f"{'confirmed' if nxt.confirmed else 'not confirmed'}); the only date you may give.")
    if context.ownership is not None:
        lines += _ownership_digest(context.ownership)
    lines += _release_digest(release_notes)
    pe = context.valuation_history.pe_ttm if context.valuation_history else None
    if pe is not None:
        where = (f", at the {_ordinal(round(pe.percentile_5y * 100))} percentile of its {pe.years or 5}-year range "
                 f"({pe.min_5y:.1f}x-{pe.max_5y:.1f}x)" if pe.percentile_5y is not None
                 and pe.min_5y is not None and pe.max_5y is not None else "")
        lines.append(f"Price-to-earnings on trailing-twelve-month EPS: {pe.latest:g}x{where}.")
    return lines if len(lines) > 1 else []


def render_role_blocks(context: Context, release_notes: dict | None = None) -> dict[str, str]:
    """What each kind of agent reads: only the blocks it uses, and a short digest for the deciders.

    Every call an agent makes resends its block, so the full context is not given to all of them.
    The sentiment analyst reads none. A role with nothing to read is left out.
    """
    notes = _notes_lines(context.notes) if context.notes else []
    c, plan = context, {}
    fin = _financials(c.financials, c.price, c.company) if c.financials else []
    revenue = _revenue(c.revenue_breakdown) if c.revenue_breakdown else []
    price = [_price_line(c.price)] if c.price else []
    plan["market"] = [*(_technicals(c.technicals) if c.technicals else []), *price,
                      *(_theme(c.theme_backdrop) if c.theme_backdrop else [])]
    plan["news"] = [*(_events(c.events) if c.events else []),
                    *(_backdrop(c.market_backdrop) if c.market_backdrop else [])]
    plan["fundamentals"] = [*fin, *revenue, *_release(release_notes), *(_valuation(c.valuation_history) if c.valuation_history else []),
                            *(_company(c.company) if c.company else []),
                            *(_events(c.events) if c.events else []), *price, *notes]
    plan["sentiment"] = _ownership(c.ownership) if c.ownership else []
    plan["decision"] = [*_digest(c, release_notes), *notes] if _digest(c, release_notes) else []
    return {role: "\n".join([_GUIDANCE, "", *lines]) for role, lines in plan.items() if lines}


def render_context(context: Context, release_notes: dict | None = None) -> str:
    lines = [_GUIDANCE]
    if context.financials is not None:
        lines += ["", *_financials(context.financials, context.price, context.company)]
    if context.revenue_breakdown is not None:
        lines += ["", *_revenue(context.revenue_breakdown)]
    if context.technicals is not None:
        lines += ["", *_technicals(context.technicals)]
    if context.company is not None:
        lines += ["", *_company(context.company)]
    if context.ownership is not None:
        lines += ["", *_ownership(context.ownership)]
    if release_notes and release_notes.get("items"):
        lines += ["", *_release(release_notes)]
    if context.valuation_history is not None:
        lines += ["", *_valuation(context.valuation_history)]
    if context.market_backdrop is not None and _backdrop(context.market_backdrop):
        lines += ["", *_backdrop(context.market_backdrop)]
    if context.theme_backdrop is not None:
        lines += ["", *_theme(context.theme_backdrop)]
    if context.events is not None:
        lines += ["", *_events(context.events)]
    if context.price is not None:
        lines += ["", _price_line(context.price)]
    if context.prior_reports:
        now = context.price.close if context.price else context.technicals.close if context.technicals else None
        lines += ["", *_prior(context.prior_reports, now)]
    if context.notes:
        lines += ["", *_notes_lines(context.notes)]
    return "\n".join(lines)
