"""A fingerprint of the context the service parsed, so a caller can record what a report used."""

from __future__ import annotations

import hashlib
import json

from tradingagents.context.models import Context


def context_digest(context: Context) -> str:
    """sha256 of the parsed context as canonical JSON: sorted keys, no whitespace, nulls kept."""
    canonical = json.dumps(context.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
