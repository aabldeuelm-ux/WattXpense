"""The WattXpense application.

Everything the browser needs is mounted under `/api`. The app is usable with no model and
no hardware attached: the AI layer degrades to a written summary of the figures, and
simulated readings stay labelled as simulated everywhere they appear.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.app.config import get_settings
from backend.app.database import init_db
from backend.app.routers import ai, analytics, billing, core

VERSION = "1.0.0"


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Creating the schema here rather than at import time keeps a test that drops the
    # tables mid-session from being undone by an import order detail.
    init_db()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="WattXpense",
        description=(
            "Home energy tracking with projections, tariff-aware costing, and an optional "
            "local model. Simulated data is always labelled as simulated."
        ),
        version=VERSION,
        lifespan=lifespan,
    )

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(core.router)
    app.include_router(analytics.router)
    app.include_router(billing.router)
    app.include_router(ai.router)

    _mount_dashboard(app)

    return app


def _mount_dashboard(app: FastAPI) -> None:
    """Serve the built dashboard from the same origin as the API.

    The catch-all is registered last so it can only ever be reached by a path no router
    claimed. Unknown `/api/...` paths get a JSON 404 rather than the dashboard HTML, so a
    mistyped endpoint fails loudly instead of handing the frontend a page it cannot parse.
    """
    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"

    @app.get("/api/{unknown:path}", include_in_schema=False)
    def unknown_api_path(unknown: str) -> JSONResponse:
        return JSONResponse({"detail": f"Unknown API path: /api/{unknown}"}, status_code=404)

    if not dist.is_dir():
        @app.get("/{path:path}", include_in_schema=False)
        def dashboard_missing(path: str) -> JSONResponse:
            return JSONResponse(
                {
                    "detail": (
                        "The dashboard has not been built yet. Run `cd frontend && npx vite build`, "
                        "or use `npm run dev` for frontend development."
                    )
                },
                status_code=404,
            )
        return

    app.mount("/", StaticFiles(directory=str(dist), html=True), name="dashboard")


app = create_app()
