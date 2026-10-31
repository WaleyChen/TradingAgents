"""Optional API-key authentication for the HTTP API."""

from __future__ import annotations

import os
import secrets

from fastapi import Header, HTTPException, status


def get_configured_api_key() -> str | None:
    """Return the configured API key, or None if auth is disabled."""
    key = os.environ.get("TRADINGAGENTS_API_KEY", "").strip()
    return key or None


def require_api_key(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """Enforce Bearer / X-API-Key auth when TRADINGAGENTS_API_KEY is set.

    When the env var is unset, the API is open (convenient for local Docker).
    Set a key before exposing the container on a network Rails can reach.
    """
    expected = get_configured_api_key()
    if expected is None:
        return

    provided: str | None = None
    if x_api_key:
        provided = x_api_key.strip()
    elif authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            provided = token.strip()

    if provided is None or not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
