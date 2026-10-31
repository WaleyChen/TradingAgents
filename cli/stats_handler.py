import threading
from collections import defaultdict
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.outputs import LLMResult

# Label used when a response carries usage but no identifiable model name.
UNKNOWN_MODEL = "unknown"


def _empty_usage() -> dict[str, int]:
    return {
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_5m_tokens": 0,
        "cache_creation_1h_tokens": 0,
        "reasoning_tokens": 0,
    }


def _model_name(message: Any, response: LLMResult) -> str:
    """Best-effort model identity for a response.

    Providers put the resolved model on the message's ``response_metadata``;
    some surface it only on the batch-level ``llm_output`` instead.
    """
    metadata = getattr(message, "response_metadata", None) or {}
    for key in ("model_name", "model"):
        name = metadata.get(key)
        if name:
            return str(name)

    llm_output = response.llm_output or {}
    for key in ("model_name", "model"):
        name = llm_output.get(key)
        if name:
            return str(name)

    return UNKNOWN_MODEL


class StatsCallbackHandler(BaseCallbackHandler):
    """Callback handler that tracks LLM calls, tool calls, and token usage.

    Usage is accumulated both as flat run totals (for the live display) and
    per model, so the deep/quick split can be priced separately afterwards.
    """

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self.llm_calls = 0
        self.tool_calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self._by_model: dict[str, dict[str, int]] = defaultdict(_empty_usage)

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        **kwargs: Any,
    ) -> None:
        """Increment LLM call counter when an LLM starts."""
        with self._lock:
            self.llm_calls += 1

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        **kwargs: Any,
    ) -> None:
        """Increment LLM call counter when a chat model starts."""
        with self._lock:
            self.llm_calls += 1

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        """Extract token usage from LLM response."""
        try:
            generation = response.generations[0][0]
        except (IndexError, TypeError):
            return

        usage_metadata = None
        if hasattr(generation, "message"):
            message = generation.message
            if isinstance(message, AIMessage) and hasattr(message, "usage_metadata"):
                usage_metadata = message.usage_metadata

        if not usage_metadata:
            return

        # ``input_tokens`` is the provider total and already includes any cache
        # reads/writes; the details are recorded alongside it (not subtracted)
        # so pricing can split them apart later.
        input_details = usage_metadata.get("input_token_details") or {}
        output_details = usage_metadata.get("output_token_details") or {}
        model = _model_name(message, response)

        with self._lock:
            self.tokens_in += usage_metadata.get("input_tokens", 0)
            self.tokens_out += usage_metadata.get("output_tokens", 0)

            usage = self._by_model[model]
            usage["calls"] += 1
            usage["input_tokens"] += usage_metadata.get("input_tokens", 0)
            usage["output_tokens"] += usage_metadata.get("output_tokens", 0)
            usage["cache_read_tokens"] += input_details.get("cache_read", 0) or 0
            usage["cache_creation_5m_tokens"] += (
                input_details.get("ephemeral_5m_input_tokens")
                or input_details.get("cache_creation")
                or 0
            )
            usage["cache_creation_1h_tokens"] += (
                input_details.get("ephemeral_1h_input_tokens") or 0
            )
            usage["reasoning_tokens"] += output_details.get("reasoning", 0) or 0

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        **kwargs: Any,
    ) -> None:
        """Increment tool call counter when a tool starts."""
        with self._lock:
            self.tool_calls += 1

    def get_stats(self) -> dict[str, Any]:
        """Return current statistics."""
        with self._lock:
            return {
                "llm_calls": self.llm_calls,
                "tool_calls": self.tool_calls,
                "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out,
            }

    def get_model_usage(self) -> dict[str, dict[str, int]]:
        """Return a copy of per-model token usage keyed by model name."""
        with self._lock:
            return {model: dict(usage) for model, usage in self._by_model.items()}
