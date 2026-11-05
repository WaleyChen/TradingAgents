"""The context schema as a JSON Schema file the Rails app reads.

Generated from the models that validate requests, so it cannot drift from them.
It covers shape only: the cross-field checks (period lengths, quarters adding up
to the year, a balanced balance sheet) are in ``check_financials``, and
``POST /api/v1/context/check`` runs them without a model call.

Regenerate with ``python -m tradingagents.context.schema``.
"""

from __future__ import annotations

import json
from pathlib import Path

from tradingagents.context.models import Context

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "docs" / "context.schema.json"


def context_json_schema() -> dict:
    return Context.model_json_schema()


def write_schema(path: Path = SCHEMA_PATH) -> None:
    path.write_text(json.dumps(context_json_schema(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    write_schema()
    print(f"wrote {SCHEMA_PATH}")
