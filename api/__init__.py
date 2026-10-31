"""HTTP API for TradingAgents (deployable via Docker for Rails and other clients)."""

__all__ = ["create_app"]


def create_app():
    from api.app import create_app as _create_app

    return _create_app()
