"""Check the numbers in the summary against the data they claim to come from.

The summary declares every dollar, percent and multiple it states, with a unit
and a source. A figure that claims a verified source (the caller's context, the
market readings, the position plan) must match a value in that source, and a
figure in the text that was never declared is flagged. Figures from the analyst
reports cannot be checked in code, so they are declared as such and never marked
verified. A bare number with no $, % or x is outside what this can see.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from tradingagents.context import Context, derive_metrics
from tradingagents.context.derive import derive_ownership, derive_technicals, sale_share_of_holding
from tradingagents.plan.check import Readings

Unit = Literal["usd", "usd_millions", "usd_billions", "pct", "x", "number"]
Source = Literal["context", "readings", "plan", "analyst_report", "derived"]
VERIFIABLE = ("context", "readings", "plan")

_SCALE = {"usd": 1.0, "usd_millions": 1e6, "usd_billions": 1e9, "pct": 1.0, "x": 1.0, "number": 1.0}
_KIND = {"usd": "usd", "usd_millions": "usd", "usd_billions": "usd", "pct": "pct", "x": "x", "number": "number"}
_TOLERANCE = 0.01          # a figure may differ from its source by 1%: the text rounds
_MIN_TOLERANCE = 0.01
_TEXT_TOLERANCE = 0.02      # the text may round a declared figure by up to 2%
MAX_FIGURES = 60


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Figure(_Strict):
    label: Annotated[str, Field(min_length=1, max_length=80)]
    value: float
    unit: Unit
    source: Source
    period: Annotated[str, Field(max_length=40)] | None = None


class SummaryDraft(_Strict):
    text: Annotated[str, Field(min_length=1, max_length=12000)]
    figures: list[Figure] = Field(default_factory=list, max_length=MAX_FIGURES)


@dataclass(frozen=True)
class Known:
    label: str
    kind: str
    value: float
    source: str


@dataclass(frozen=True)
class SummaryProblem:
    """``error``: a figure claims verified data it is not in. ``warning``: a figure was left unlisted,
    so it is unverified, which is not a false claim."""

    code: str
    subject: str
    message: str
    severity: str = "error"


def normalize(value: float, unit: str) -> tuple[str, float]:
    return _KIND[unit], value * _SCALE[unit]


def _close(a: float, b: float, tolerance: float = _TOLERANCE) -> bool:
    return abs(a - b) <= max(_MIN_TOLERANCE, tolerance * abs(b))


# --- what the text states ---------------------------------------------------

_MONEY = re.compile(r"(?<![\w$])\$\s?(\d[\d,]*(?:\.\d+)?)(\s?(?:billion|million|thousand)\b|[BMKbmk]\b)?")
_PERCENT = re.compile(r"(?<![\w.])([-+]?\d+(?:\.\d+)?)\s?%")
_MULTIPLE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s?[x×](?![\w])")
_SUFFIX = {"b": 1e9, "billion": 1e9, "m": 1e6, "million": 1e6, "k": 1e3, "thousand": 1e3}


def figures_in_text(text: str) -> list[tuple[str, float, str]]:
    """(kind, value, as written) for every dollar, percent and multiple in the text."""
    found = []
    for m in _MONEY.finditer(text):
        scale = _SUFFIX.get((m.group(2) or "").strip().lower(), 1.0)
        found.append(("usd", float(m.group(1).replace(",", "")) * scale, m.group(0).strip()))
    found += [("pct", float(m.group(1)), m.group(0).strip()) for m in _PERCENT.finditer(text)]
    found += [("x", float(m.group(1)), m.group(0).strip()) for m in _MULTIPLE.finditer(text)]
    return found


# --- what the verified sources hold -----------------------------------------

_METRIC_KIND = {"liabilities_to_equity": "x", "pe_latest_annual": "x", "working_capital": "usd",
                "ttm_revenue": "usd", "net_debt": "usd", "enterprise_value": "usd", "ev_to_ebitda": "x",
                "debt_to_equity": "x"}
_METRIC_LABEL = {"fcf_yield_pct": "free cash flow yield"}


def _context_values(context: Context) -> list[Known]:
    f, found = context.financials, []
    if f is not None:
        for s in [*f.annual, *f.quarterly]:
            for part in (s.income, s.cash_flow):
                for name, value in (part.model_dump().items() if part else ()):
                    if value is not None:
                        found.append(Known(name.replace("_", " "), "usd", float(value), "context"))
        for b in f.balance:
            found += [Known(name.replace("_", " "), "usd", float(value), "context")
                      for name, value in b.model_dump().items()
                      if isinstance(value, int) and not isinstance(value, bool)]
        for name, value in derive_metrics(f, context.price, context.company).items():
            kind = _METRIC_KIND.get(name, "pct")
            label = _METRIC_LABEL.get(name, name.removesuffix("_pct").replace("_", " "))
            found.append(Known(label, kind, float(value), "context"))
    if context.price is not None:
        found.append(Known("price close", "usd", context.price.close, "context"))
    if context.technicals is not None:
        found += _technicals_values(context.technicals)
    if context.ownership is not None:
        found += _ownership_values(context.ownership)
    found += _market_values(context)
    if context.company is not None:
        if context.company.market_cap:
            found.append(Known("market cap", "usd", float(context.company.market_cap), "context"))
        if context.company.shares_outstanding:
            found.append(Known("shares outstanding", "number", float(context.company.shares_outstanding), "context"))
    if context.events is not None:
        for r in context.events.recent_earnings:
            for name, label in (("eps_actual", "reported eps"), ("eps_estimate", "estimated eps"),
                                ("revenue_actual", "reported revenue"), ("revenue_estimate", "estimated revenue")):
                if getattr(r, name) is not None:
                    found.append(Known(label, "usd", float(getattr(r, name)), "context"))
    return found


def _technicals_values(t) -> list[Known]:
    found = [Known("technicals close", "usd", t.close, "context")]
    for name in ("sma_20", "sma_50", "sma_200", "ema_10", "high_52w", "low_52w"):
        if getattr(t, name) is not None:
            found.append(Known(f"technicals {name}", "usd", float(getattr(t, name)), "context"))
    for name in ("rsi_14", "volume", "volume_avg_50d"):
        if getattr(t, name) is not None:
            found.append(Known(f"technicals {name}", "number", float(getattr(t, name)), "context"))
    if t.macd is not None:
        found += [Known(f"technicals macd {n}", "number", float(v), "context")
                  for n, v in (("line", t.macd.line), ("signal", t.macd.signal), ("histogram", t.macd.histogram))
                  if v is not None]
    for name, value in derive_technicals(t).items():
        label = (name.replace("pct_vs_", "pct vs ").replace("pct_from_", "pct from ")
                 if name.startswith("pct_") else name.replace("_", " "))
        found.append(Known(label, "x" if name == "volume_vs_average" else "pct", float(value), "context"))
    return found


def _market_values(context: Context) -> list[Known]:
    found = []
    pe = context.valuation_history.pe_ttm if context.valuation_history else None
    if pe is not None:
        found.append(Known("pe ttm", "x", pe.latest, "context"))
        for label, value in (("pe ttm min 5y", pe.min_5y), ("pe ttm median 5y", pe.median_5y),
                             ("pe ttm max 5y", pe.max_5y)):
            found += [Known(label, "x", value, "context")] if value is not None else []
        found += [Known("pe ttm percentile", "pct", pe.percentile_5y * 100, "context")] if pe.percentile_5y is not None else []
        found += [Known("pe ttm years", "number", float(pe.years), "context")] if pe.years is not None else []
    if context.valuation_history and context.valuation_history.peg is not None:
        found.append(Known("peg", "x", context.valuation_history.peg, "context"))
    b = context.market_backdrop
    if b is not None:
        found += [Known("vix", "number", b.vix.value, "context")] if b.vix else []
        found += [Known("fear and greed", "number", b.fear_and_greed.value, "context")] if b.fear_and_greed else []
        found += ([Known("buffett indicator", "pct", b.buffett_indicator_pct.value, "context")]
                  if b.buffett_indicator_pct else [])
        found += [Known("credit spread", "number", b.credit_spread_hy_bps.value, "context")] if b.credit_spread_hy_bps else []
    for r in context.prior_reports:
        found += [Known("prior target", "usd", r.target, "context")] if r.target is not None else []
        found.append(Known("prior price", "usd", r.price_then, "context"))
    return found


def _ownership_values(o) -> list[Known]:
    """The counts, the totals and each listed figure, so the report may cite them as verified."""
    found = [Known(name.replace("_", " "), "usd" if name == "insider_net_value" else "number", float(value), "context")
             for name, value in derive_ownership(o).items()]
    for h in (o.funds.holders if o.funds else []):
        found += [Known("fund holder shares", "number", float(h.shares), "context")]
        found += [Known("fund holder value", "usd", float(h.value_usd), "context")] if h.value_usd is not None else []
        found += [Known("fund holder weight", "pct", h.weight_pct, "context")] if h.weight_pct is not None else []
        found += [Known("fund holder change", "pct", h.shares_change_pct, "context")] if h.shares_change_pct is not None else []
        found += [Known("estimated cost", "usd", h.estimated_cost, "context")] if h.estimated_cost is not None else []
    for t in (o.insiders.trades if o.insiders else []):
        found += [Known("insider trade shares", "number", float(t.shares), "context"),
                  Known("insider trade price", "usd", t.price_per_share, "context")]
        found += [Known("insider trade value", "usd", float(t.value_usd), "context")] if t.value_usd is not None else []
        share = sale_share_of_holding(t)
        found += [Known("insider sale share of holding", "pct", share, "context")] if share is not None else []
    for t in (o.congress.trades if o.congress else []):
        found += [Known("congress amount minimum", "usd", float(t.amount_min), "context"),
                  Known("congress amount maximum", "usd", float(t.amount_max), "context")]
    return found


def _readings_values(r: Readings) -> list[Known]:
    usd = (("price", r.price), ("sma_20", r.sma_20), ("sma_50", r.sma_50), ("sma_200", r.sma_200))
    num = (("rsi_14", r.rsi_14), ("macd_histogram", r.macd_histogram))
    return [Known(f"reading {n}", kind, float(v), "readings")
            for kind, group in (("usd", usd), ("number", num)) for n, v in group if v is not None]


def _trigger_values(trigger: dict[str, Any]) -> list[Known]:
    found = []
    if trigger.get("price"):
        found.append(Known("plan trigger price", "usd", float(trigger["price"]["value"]), "plan"))
    if trigger.get("indicator") and trigger.get("value") is not None:
        found.append(Known("plan trigger level", "number", float(trigger["value"]), "plan"))
    for leaf in (trigger.get("all_of") or []) + (trigger.get("any_of") or []):
        found += _trigger_values(leaf)
    return found


def _plan_values(plan: dict[str, Any]) -> list[Known]:
    found = [Known("plan as of price", "usd", float(plan["as_of_price"]), "plan"),
             Known("plan target", "usd", float(plan["target"]["price"]), "plan"),
             Known("plan position size", "pct", float(plan["now"]["portfolio_pct"]), "plan")]
    if plan.get("expected_return_pct") is not None:
        found.append(Known("plan expected return", "pct", float(plan["expected_return_pct"]), "plan"))
    for s in plan["scenarios"]:
        found += [Known("plan scenario price", "usd", float(s["price"]), "plan"),
                  Known("plan scenario probability", "pct", s["probability"] * 100, "plan")]
    for leg in [*plan.get("entries", []), *plan.get("exits", [])]:
        found += [Known("plan position size", "pct", float(leg["portfolio_pct"]), "plan"),
                  *_trigger_values(leg["trigger"])]
    return found


def known_values(context: Context | None, readings: Readings | None, plan: dict[str, Any] | None) -> list[Known]:
    """Every value the three verified sources hold, tagged with its source."""
    return [*(_context_values(context) if context else []),
            *(_readings_values(readings) if readings else []),
            *(_plan_values(plan) if plan else [])]


# --- the check --------------------------------------------------------------

def _matches(figure: Figure, known: list[Known]) -> bool:
    kind, value = normalize(figure.value, figure.unit)
    return any(k.source == figure.source and k.kind == kind and _close(value, k.value) for k in known)


def check_summary(draft: SummaryDraft, known: list[Known]) -> list[SummaryProblem]:
    """What is wrong with the summary's figures; empty when every one is accounted for."""
    found = [SummaryProblem("figure_not_in_source", f.label,
                            f"{f.label}: {f.value:g} ({f.unit}) is not in the {f.source} data")
             for f in draft.figures if f.source in VERIFIABLE and not _matches(f, known)]
    declared = [normalize(f.value, f.unit) for f in draft.figures]
    stated = figures_in_text(draft.text)
    seen, undeclared = set(), []
    for kind, value, raw in stated:
        if raw not in seen and not any(k == kind and _close(value, v, _TEXT_TOLERANCE) for k, v in declared):
            undeclared.append(SummaryProblem("undeclared_figure", raw,
                                             f"{raw} appears in the text but is not in the figures list", "warning"))
        seen.add(raw)
    distinct = len({raw for _, _, raw in stated})
    if distinct >= 3 and len(undeclared) / distinct > 0.5:   # the list was skipped, not just short by one
        found.append(SummaryProblem(
            "figures_not_declared", "figures",
            f"you declared {distinct - len(undeclared)} of the {distinct} dollar amounts, percentages and multiples "
            "in the text: list every one in `figures`, with its exact value, unit, source and period"))
    return found + undeclared


def annotate(figures: list[Figure], known: list[Known]) -> list[dict[str, Any]]:
    """The figures as plain data, each marked verified only when checked against a verified source."""
    return [{**f.model_dump(), "verified": f.source in VERIFIABLE and _matches(f, known)} for f in figures]
