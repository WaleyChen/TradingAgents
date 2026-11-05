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
from tradingagents.plan.check import Readings

Unit = Literal["usd", "usd_millions", "usd_billions", "pct", "x", "number"]
Source = Literal["context", "readings", "plan", "analyst_report", "derived"]
VERIFIABLE = ("context", "readings", "plan")

_SCALE = {"usd": 1.0, "usd_millions": 1e6, "usd_billions": 1e9, "pct": 1.0, "x": 1.0, "number": 1.0}
_KIND = {"usd": "usd", "usd_millions": "usd", "usd_billions": "usd", "pct": "pct", "x": "x", "number": "number"}
_TOLERANCE = 0.01          # a figure may differ from its source by 1%: the text rounds
_MIN_TOLERANCE = 0.01
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
    code: str
    subject: str
    message: str


def normalize(value: float, unit: str) -> tuple[str, float]:
    return _KIND[unit], value * _SCALE[unit]


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(_MIN_TOLERANCE, _TOLERANCE * abs(b))


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
                "ttm_revenue": "usd"}


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
        for name, value in derive_metrics(f, context.price).items():
            kind = _METRIC_KIND.get(name, "pct")
            found.append(Known(name.removesuffix("_pct").replace("_", " "), kind, float(value), "context"))
    if context.price is not None:
        found.append(Known("price close", "usd", context.price.close, "context"))
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
    seen = set()
    for kind, value, raw in figures_in_text(draft.text):
        if raw not in seen and not any(k == kind and _close(value, v) for k, v in declared):
            found.append(SummaryProblem("undeclared_figure", raw,
                                        f"{raw} appears in the text but is not in the figures list"))
        seen.add(raw)
    return found


def annotate(figures: list[Figure], known: list[Known]) -> list[dict[str, Any]]:
    """The figures as plain data, each marked verified only when checked against a verified source."""
    return [{**f.model_dump(), "verified": f.source in VERIFIABLE and _matches(f, known)} for f in figures]
