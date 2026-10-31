"""Per-model token accounting turned into a run cost estimate.

A run splits work across two models: ``deep_think_llm`` drives two nodes
(research manager, portfolio manager) while ``quick_think_llm`` drives the
analysts, both debates, the trader, reflection, and signal processing. A single
token total hides that split, so this module prices each model separately.

Built-in rates cover the Anthropic catalog only. Any other provider reports
token counts with a blank cost until rates are supplied via
``TRADINGAGENTS_MODEL_PRICING`` — a wrong number is worse than no number.
"""

from __future__ import annotations

import json
import os

# USD per 1M tokens, as (input, output).
# Source: Anthropic published pricing, recorded 2026-06-24. Verify against
# https://platform.claude.com/docs/en/pricing before relying on these for
# billing decisions — rates change and introductory pricing expires.
BUILTIN_PRICING: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

PRICING_ENV_VAR = "TRADINGAGENTS_MODEL_PRICING"
SHOW_USAGE_ENV_VAR = "TRADINGAGENTS_SHOW_USAGE"

_BOOL_TRUE = ("true", "1", "yes", "on")


def usage_enabled(flag: bool | None = None) -> bool:
    """Decide whether to print the usage report.

    An explicit ``--usage/--no-usage`` flag wins; otherwise honor
    ``TRADINGAGENTS_SHOW_USAGE``. Off by default — the report is a measurement
    aid, not part of the normal run output.
    """
    if flag is not None:
        return flag
    return os.environ.get(SHOW_USAGE_ENV_VAR, "").strip().lower() in _BOOL_TRUE

# Cache multipliers applied to the model's input rate. Reads are heavily
# discounted; writes carry a premium that scales with the entry's TTL.
_CACHE_READ_MULTIPLIER = 0.1
_CACHE_WRITE_5M_MULTIPLIER = 1.25
_CACHE_WRITE_1H_MULTIPLIER = 2.0

_PER_MILLION = 1_000_000


def load_pricing_overrides() -> dict[str, tuple[float, float]]:
    """Read per-model rate overrides from the environment.

    Format is a JSON object of ``{"model-id": [input_per_mtok, output_per_mtok]}``.
    Lets users price providers we don't ship rates for without a code change.
    Invalid values raise rather than silently producing a wrong cost.
    """
    raw = os.environ.get(PRICING_ENV_VAR)
    if not raw or not raw.strip():
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid {PRICING_ENV_VAR}: expected a JSON object ({exc})") from exc

    if not isinstance(parsed, dict):
        raise ValueError(f"Invalid {PRICING_ENV_VAR}: expected a JSON object, got {type(parsed).__name__}")

    overrides: dict[str, tuple[float, float]] = {}
    for model, rates in parsed.items():
        if not isinstance(rates, (list, tuple)) or len(rates) != 2:
            raise ValueError(
                f"Invalid {PRICING_ENV_VAR} entry for {model!r}: "
                "expected [input_per_mtok, output_per_mtok]"
            )
        try:
            overrides[model] = (float(rates[0]), float(rates[1]))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Invalid {PRICING_ENV_VAR} rates for {model!r}: {rates!r}"
            ) from exc
    return overrides


def resolve_price(
    model: str,
    overrides: dict[str, tuple[float, float]] | None = None,
) -> tuple[float, float] | None:
    """Return (input, output) USD per 1M tokens for a model, or None if unknown.

    Providers report dated snapshot IDs (``claude-haiku-4-5-20251001``), so an
    exact miss falls back to the longest matching prefix.
    """
    table = {**BUILTIN_PRICING, **(overrides or {})}
    if model in table:
        return table[model]

    matches = [key for key in table if model.startswith(key)]
    if not matches:
        return None
    return table[max(matches, key=len)]


def estimate_cost(
    model: str,
    usage: dict,
    overrides: dict[str, tuple[float, float]] | None = None,
) -> float | None:
    """Estimate USD for one model's usage, or None when the model has no rate.

    ``usage["input_tokens"]`` is the provider total *including* cache reads and
    cache writes (langchain-anthropic adds them back in), so the cached spans
    are subtracted out before billing the remainder at full rate. Reasoning
    tokens are already inside ``output_tokens`` and are not billed separately.
    """
    price = resolve_price(model, overrides)
    if price is None:
        return None
    rate_in, rate_out = price

    cache_read = usage.get("cache_read_tokens", 0)
    write_5m = usage.get("cache_creation_5m_tokens", 0)
    write_1h = usage.get("cache_creation_1h_tokens", 0)
    uncached_input = max(0, usage.get("input_tokens", 0) - cache_read - write_5m - write_1h)

    billable = (
        uncached_input * rate_in
        + cache_read * rate_in * _CACHE_READ_MULTIPLIER
        + write_5m * rate_in * _CACHE_WRITE_5M_MULTIPLIER
        + write_1h * rate_in * _CACHE_WRITE_1H_MULTIPLIER
        + usage.get("output_tokens", 0) * rate_out
    )
    return billable / _PER_MILLION


def build_usage_rows(
    by_model: dict[str, dict],
    overrides: dict[str, tuple[float, float]] | None = None,
) -> tuple[list[dict], float]:
    """Build per-model report rows plus the summed cost of the priced models.

    Rows are ordered most expensive first so the dominant model is obvious.
    Unpriced models still appear (with ``cost=None``) and are excluded from the
    total, which is therefore a lower bound whenever any model lacks a rate.
    """
    rows = []
    total = 0.0
    for model, usage in by_model.items():
        cost = estimate_cost(model, usage, overrides)
        if cost is not None:
            total += cost
        rows.append({
            "model": model,
            "calls": usage.get("calls", 0),
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_read_tokens": usage.get("cache_read_tokens", 0),
            "reasoning_tokens": usage.get("reasoning_tokens", 0),
            "cost": cost,
        })

    rows.sort(key=lambda row: (row["cost"] is not None, row["cost"] or 0), reverse=True)
    return rows, total


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def render_usage_table(rows: list[dict], total: float):
    """Build a rich table of per-model usage and estimated cost."""
    from rich.table import Table

    table = Table(title="Token usage and estimated cost", title_style="bold cyan")
    table.add_column("Model", style="cyan", no_wrap=True)
    table.add_column("Calls", justify="right")
    table.add_column("Input", justify="right")
    table.add_column("Output", justify="right")
    table.add_column("Cached", justify="right")
    table.add_column("Thinking", justify="right")
    table.add_column("Cost (USD)", justify="right", style="green")

    for row in rows:
        table.add_row(
            row["model"],
            str(row["calls"]),
            _fmt_tokens(row["input_tokens"]),
            _fmt_tokens(row["output_tokens"]),
            _fmt_tokens(row["cache_read_tokens"]),
            _fmt_tokens(row["reasoning_tokens"]),
            f"${row['cost']:.4f}" if row["cost"] is not None else "—",
        )

    unpriced = [row["model"] for row in rows if row["cost"] is None]
    label = "Total (priced models only)" if unpriced else "Total"
    table.add_section()
    table.add_row(
        f"[bold]{label}[/bold]", "", "", "", "", "", f"[bold]${total:.4f}[/bold]"
    )
    return table


def unpriced_models(rows: list[dict]) -> list[str]:
    """Models in the report that have no rate, and so contribute no cost."""
    return [row["model"] for row in rows if row["cost"] is None]
