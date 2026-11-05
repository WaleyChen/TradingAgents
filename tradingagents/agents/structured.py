"""Shared helpers for invoking an agent with structured output and a graceful fallback.

The Portfolio Manager, Trader, and Research Manager all follow the same
canonical pattern:

1. At agent creation, wrap the LLM with ``with_structured_output(Schema)``
   so the model returns a typed Pydantic instance. If the provider does
   not support structured output (rare; mostly older Ollama models), the
   wrap is skipped and the agent uses free-text generation instead.
2. At invocation, run the structured call and render the result back to
   markdown. If the structured call itself fails for any reason
   (malformed JSON from a weak model, transient provider issue), fall
   back to a plain ``llm.invoke`` so the pipeline never blocks.

Centralising the pattern here keeps the agent factories small and ensures
all three agents log the same warnings when fallback fires.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Schema-only structured output binds exactly one tool (the schema itself), so a
# model that reaches for a search tool emits an unknown tool call and the whole
# structured attempt is discarded for a free-text retry. Agents on this path
# state the constraint explicitly rather than relying on the binding alone
# (#1130).
NO_EXTERNAL_TOOLS = (
    "Use only the evidence provided in this prompt. Do not call external tools "
    "or search the web; if something is missing, say so explicitly."
)

# The decision agents write the text a customer reads: one analyst's view, not
# a record of the internal debate.
ONE_VOICE = (
    "Write as a single analyst giving one view. Never name the internal roles "
    "(researchers, managers, trader, risk analysts) and never use the words bull case, "
    "bear case, aggressive, conservative, debate, rebuttal or rebutted, or say that "
    "one side won or an argument went unanswered. Use the inputs as evidence only: "
    "state each point and the conclusion directly, as your own analysis."
)

# A customer cannot check a bare number, so every figure says where it is from.
SOURCED_NUMBERS = (
    "Give every figure its source and as-of date, for example (Yahoo Finance, 2026-11-02) "
    "or (10-Q, quarter ended 2026-06-30). Mark a figure that came from your own "
    "calculation as derived, and a figure the data did not confirm as unverified."
)

# A sharp move the data does not explain is a question, not a finding.
UNEXPLAINED = (
    "When a figure moves sharply and the data you were given does not say why, call it unexplained: do not "
    "blame competition, demand or any other cause, and do not build your conclusion on a cause you have not "
    "seen. Say what could explain it and where to look, such as the earnings release or the notes to the filing."
)

# Social posts are weak, unaccountable evidence: use them as a small supporting signal.
NO_HANDLES = (
    "Treat social-media sentiment as a small supporting signal, never the main basis "
    "for a conclusion. Do not quote, name or cite any user, handle, username or account."
)


def bind_structured(llm: Any, schema: type[T], agent_name: str) -> Any | None:
    """Return ``llm.with_structured_output(schema)`` or ``None`` if unsupported.

    Logs a warning when the binding fails so the user understands the agent
    will use free-text generation for every call instead of one-shot fallback.
    """
    try:
        return llm.with_structured_output(schema)
    except (NotImplementedError, AttributeError) as exc:
        logger.warning(
            "%s: provider does not support with_structured_output (%s); "
            "falling back to free-text generation",
            agent_name, exc,
        )
        return None


def invoke_structured(structured_llm: Any | None, prompt: Any, agent_name: str) -> T | None:
    """Run the structured call; ``None`` when there is none or it fails.

    ``prompt`` is whatever the underlying LLM accepts (a string for chat
    invocations, a list of message dicts for chat models that take that
    shape), so a caller can forward the same value to its free-text fallback.
    """
    if structured_llm is None:
        return None
    try:
        result = structured_llm.invoke(prompt)
        if result is None:
            # A thinking model can answer in plain text instead of calling
            # the tool, leaving the parser with nothing to return. Treat it
            # as a structured miss and fall back, with a clear reason.
            raise ValueError("structured output returned no parsed result")
        return result
    except Exception as exc:
        logger.warning(
            "%s: structured-output invocation failed (%s); retrying once as free text",
            agent_name, exc,
        )
        return None


def invoke_structured_or_freetext(
    structured_llm: Any | None,
    plain_llm: Any,
    prompt: Any,
    render: Callable[[T], str],
    agent_name: str,
) -> str:
    """Run the structured call and render to markdown; fall back to free-text on any failure."""
    result = invoke_structured(structured_llm, prompt, agent_name)
    if result is not None:
        return render(result)
    return plain_llm.invoke(prompt).content
