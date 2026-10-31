"""Tests for per-model token accounting and run cost estimation.

The framework splits work across two models (``deep_think_llm`` runs two nodes;
``quick_think_llm`` runs everything else), so a single token total can't tell
you where the money went. These tests cover the per-model breakdown and the
pricing math that turns it into a dollar estimate.
"""

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from cli.stats_handler import StatsCallbackHandler
from cli.usage_report import (
    build_usage_rows,
    estimate_cost,
    load_pricing_overrides,
    render_usage_table,
    resolve_price,
    unpriced_models,
    usage_enabled,
)


def _llm_result(
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    cache_read: int = 0,
    cache_creation_5m: int = 0,
    cache_creation_1h: int = 0,
    reasoning: int = 0,
) -> LLMResult:
    """Build an LLMResult shaped like langchain-anthropic's output.

    Note ``input_tokens`` is the *total* including cache reads and creations —
    that is what ``langchain_anthropic`` reports, and mispricing it as
    uncached-only is the bug these tests guard against.
    """
    message = AIMessage(
        content="",
        response_metadata={"model_name": model},
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "input_token_details": {
                "cache_read": cache_read,
                "ephemeral_5m_input_tokens": cache_creation_5m,
                "ephemeral_1h_input_tokens": cache_creation_1h,
            },
            "output_token_details": {"reasoning": reasoning},
        },
    )
    return LLMResult(generations=[[ChatGeneration(message=message)]])


@pytest.mark.unit
class TestPerModelAccounting:
    def test_splits_usage_by_model(self):
        handler = StatsCallbackHandler()
        handler.on_llm_end(_llm_result("claude-haiku-4-5", 1000, 200))
        handler.on_llm_end(_llm_result("claude-haiku-4-5", 500, 100))
        handler.on_llm_end(_llm_result("claude-opus-4-8", 3000, 400))

        by_model = handler.get_model_usage()
        assert set(by_model) == {"claude-haiku-4-5", "claude-opus-4-8"}
        assert by_model["claude-haiku-4-5"]["input_tokens"] == 1500
        assert by_model["claude-haiku-4-5"]["output_tokens"] == 300
        assert by_model["claude-haiku-4-5"]["calls"] == 2
        assert by_model["claude-opus-4-8"]["input_tokens"] == 3000
        assert by_model["claude-opus-4-8"]["calls"] == 1

    def test_captures_cache_and_reasoning_detail(self):
        handler = StatsCallbackHandler()
        handler.on_llm_end(
            _llm_result(
                "claude-sonnet-5",
                10_000,
                2_000,
                cache_read=6_000,
                cache_creation_5m=1_000,
                reasoning=1_500,
            )
        )

        usage = handler.get_model_usage()["claude-sonnet-5"]
        assert usage["cache_read_tokens"] == 6_000
        assert usage["cache_creation_5m_tokens"] == 1_000
        assert usage["reasoning_tokens"] == 1_500

    def test_preserves_aggregate_totals(self):
        """The live display reads the flat totals — they must keep working."""
        handler = StatsCallbackHandler()
        handler.on_llm_end(_llm_result("claude-haiku-4-5", 1000, 200))
        handler.on_llm_end(_llm_result("claude-opus-4-8", 3000, 400))

        stats = handler.get_stats()
        assert stats["tokens_in"] == 4000
        assert stats["tokens_out"] == 600

    def test_tolerates_missing_usage_metadata(self):
        handler = StatsCallbackHandler()
        message = AIMessage(content="", response_metadata={"model_name": "x"})
        handler.on_llm_end(LLMResult(generations=[[ChatGeneration(message=message)]]))
        assert handler.get_model_usage() == {}

    def test_falls_back_to_unknown_model_label(self):
        handler = StatsCallbackHandler()
        message = AIMessage(
            content="",
            usage_metadata={
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
            },
        )
        handler.on_llm_end(LLMResult(generations=[[ChatGeneration(message=message)]]))
        assert list(handler.get_model_usage()) == ["unknown"]


@pytest.mark.unit
class TestPriceResolution:
    def test_exact_match(self):
        assert resolve_price("claude-haiku-4-5") == (1.0, 5.0)

    def test_dated_snapshot_matches_by_prefix(self):
        """Providers return dated IDs (claude-haiku-4-5-20251001)."""
        assert resolve_price("claude-haiku-4-5-20251001") == (1.0, 5.0)

    def test_prefers_longest_prefix(self):
        # "claude-opus-4-8" must not be shadowed by a shorter "claude-opus" key.
        assert resolve_price("claude-opus-4-8") == (5.0, 25.0)

    def test_unknown_model_has_no_price(self):
        assert resolve_price("gpt-5.5") is None

    def test_env_override_supplies_missing_price(self, monkeypatch):
        monkeypatch.setenv(
            "TRADINGAGENTS_MODEL_PRICING", '{"gpt-5.5": [1.25, 10.0]}'
        )
        overrides = load_pricing_overrides()
        assert resolve_price("gpt-5.5", overrides) == (1.25, 10.0)

    def test_env_override_beats_builtin(self, monkeypatch):
        monkeypatch.setenv(
            "TRADINGAGENTS_MODEL_PRICING", '{"claude-haiku-4-5": [0.5, 2.5]}'
        )
        overrides = load_pricing_overrides()
        assert resolve_price("claude-haiku-4-5", overrides) == (0.5, 2.5)

    def test_malformed_override_is_rejected_loudly(self, monkeypatch):
        monkeypatch.setenv("TRADINGAGENTS_MODEL_PRICING", "not json")
        with pytest.raises(ValueError, match="TRADINGAGENTS_MODEL_PRICING"):
            load_pricing_overrides()

    def test_unset_override_is_empty(self, monkeypatch):
        monkeypatch.delenv("TRADINGAGENTS_MODEL_PRICING", raising=False)
        assert load_pricing_overrides() == {}


@pytest.mark.unit
class TestCostEstimation:
    def test_plain_input_and_output(self):
        usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
        # Haiku: $1 in + $5 out
        assert estimate_cost("claude-haiku-4-5", usage) == pytest.approx(6.0)

    def test_cache_reads_are_not_double_counted(self):
        """input_tokens already includes cache_read; billing both is a 10x error."""
        usage = {
            "input_tokens": 1_000_000,
            "output_tokens": 0,
            "cache_read_tokens": 900_000,
        }
        # 100k uncached @ $1/M + 900k cache-read @ $0.10/M = 0.10 + 0.09
        assert estimate_cost("claude-haiku-4-5", usage) == pytest.approx(0.19)

    def test_cache_creation_premiums(self):
        usage = {
            "input_tokens": 2_000_000,
            "output_tokens": 0,
            "cache_creation_5m_tokens": 1_000_000,
            "cache_creation_1h_tokens": 1_000_000,
        }
        # 0 uncached + 1M @ 1.25x $1 + 1M @ 2x $1
        assert estimate_cost("claude-haiku-4-5", usage) == pytest.approx(3.25)

    def test_reasoning_tokens_are_not_billed_twice(self):
        """Thinking tokens are already inside output_tokens."""
        usage = {
            "input_tokens": 0,
            "output_tokens": 1_000_000,
            "reasoning_tokens": 800_000,
        }
        assert estimate_cost("claude-haiku-4-5", usage) == pytest.approx(5.0)

    def test_unknown_model_returns_none(self):
        assert estimate_cost("gpt-5.5", {"input_tokens": 100, "output_tokens": 100}) is None


@pytest.mark.unit
class TestUsageRows:
    def test_rows_carry_cost_and_total(self):
        by_model = {
            "claude-haiku-4-5": {
                "calls": 12,
                "input_tokens": 1_000_000,
                "output_tokens": 100_000,
            },
            "claude-opus-4-8": {
                "calls": 2,
                "input_tokens": 150_000,
                "output_tokens": 4_000,
            },
        }
        rows, total = build_usage_rows(by_model)

        assert [r["model"] for r in rows] == ["claude-haiku-4-5", "claude-opus-4-8"]
        assert rows[0]["cost"] == pytest.approx(1.5)   # $1.00 in + $0.50 out
        assert rows[1]["cost"] == pytest.approx(0.85)  # $0.75 in + $0.10 out
        assert total == pytest.approx(2.35)

    def test_rows_sorted_by_cost_descending(self):
        by_model = {
            "claude-opus-4-8": {"calls": 1, "input_tokens": 1000, "output_tokens": 0},
            "claude-haiku-4-5": {"calls": 1, "input_tokens": 10_000_000, "output_tokens": 0},
        }
        rows, _ = build_usage_rows(by_model)
        assert rows[0]["model"] == "claude-haiku-4-5"

    def test_unpriced_model_yields_partial_total(self):
        by_model = {
            "gpt-5.5": {"calls": 1, "input_tokens": 1000, "output_tokens": 100},
            "claude-haiku-4-5": {"calls": 1, "input_tokens": 1_000_000, "output_tokens": 0},
        }
        rows, total = build_usage_rows(by_model)

        unpriced = next(r for r in rows if r["model"] == "gpt-5.5")
        assert unpriced["cost"] is None
        # Total covers only the models we can price.
        assert total == pytest.approx(1.0)

    def test_empty_usage(self):
        assert build_usage_rows({}) == ([], 0.0)

    def test_unpriced_models_listed(self):
        rows, _ = build_usage_rows(
            {"gpt-5.5": {"calls": 1, "input_tokens": 10, "output_tokens": 1}}
        )
        assert unpriced_models(rows) == ["gpt-5.5"]


@pytest.mark.unit
class TestUsageFlag:
    def test_explicit_flag_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("TRADINGAGENTS_SHOW_USAGE", "true")
        assert usage_enabled(False) is False

    def test_env_enables_when_flag_omitted(self, monkeypatch):
        monkeypatch.setenv("TRADINGAGENTS_SHOW_USAGE", "yes")
        assert usage_enabled(None) is True

    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv("TRADINGAGENTS_SHOW_USAGE", raising=False)
        assert usage_enabled(None) is False


@pytest.mark.unit
class TestRendering:
    def test_table_renders_costs_and_placeholder(self):
        from rich.console import Console

        rows, total = build_usage_rows({
            "claude-haiku-4-5": {"calls": 12, "input_tokens": 1_000_000, "output_tokens": 100_000},
            "gpt-5.5": {"calls": 2, "input_tokens": 150_000, "output_tokens": 4_000},
        })
        console = Console(width=120, record=True)
        console.print(render_usage_table(rows, total))
        out = console.export_text()

        assert "claude-haiku-4-5" in out
        assert "$1.5000" in out
        assert "—" in out  # unpriced model
        assert "Total (priced models only)" in out
