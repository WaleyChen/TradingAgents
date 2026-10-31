"""CLI entrypoint: ``tradingagents-api`` / ``python -m api``."""

from __future__ import annotations

import os


def run() -> None:
    """Start the uvicorn server."""
    import uvicorn

    host = os.environ.get("TRADINGAGENTS_API_HOST", "0.0.0.0")
    port = int(os.environ.get("TRADINGAGENTS_API_PORT", "8000"))
    # Single worker: in-memory JobManager is process-local.
    uvicorn.run(
        "api.app:create_app",
        factory=True,
        host=host,
        port=port,
        workers=1,
        log_level=os.environ.get("TRADINGAGENTS_API_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    run()
