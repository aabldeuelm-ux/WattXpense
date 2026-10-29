"""Vercel Python entrypoint for WattXpense FastAPI backend.

 Exposes the ``app`` FastAPI instance at module level so Vercel's
 serverless Python runtime can import it directly without relative
 package imports.

Example ``vercel.json`` entry:
    {
      "services": {
        "backend": {
          "root": "backend",
          "framework": "fastapi"
        }
      }
    }
"""

import uvicorn  # noqa: F401 – kept for warm-start compatibility
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.app.config import get_settings
from backend.app.routers import ai, analytics, auth, billing, core


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = get_settings()

    app = FastAPI(
        title="WattXpense",
        description=(
            "Home energy tracking with projections, tariff-aware costing, and an optional "
            "local model. Simulated data is always labelled as simulated."
        ),
        version="1.0.0",
    )

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    app.include_router(auth.router)
    app.include_router(core.router)
    app.include_router(analytics.router)
    app.include_router(billing.router)
    app.include_router(ai.router)

    return app


# Module-level app instance – Vercel imports this directly
app = create_app()