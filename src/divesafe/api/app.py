"""FastAPI application factory. Assessment endpoints are added in a later phase."""

from __future__ import annotations

from fastapi import FastAPI

from divesafe import __version__
from divesafe.config import configure_logging, get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="DiveSafe AI",
        version=__version__,
        description=(
            "Decision support only. The final decision always belongs to the diver, "
            "dive master or dive leader."
        ),
    )

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app
