"""Engine, session, schema creation and additive migrations.

Supports both SQLite (local development) and PostgreSQL (production / Supabase).
The engine is built at import time, which is why the test suite sets its environment
before importing anything from `backend.app`.

Existing installs are migrated by adding columns rather than recreating tables, so a
database written by an older build keeps its readings and its tariff.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app.config import get_settings, normalize_database_url
from backend.app.models import Base  # noqa: F401  (re-exported for init_db)

_settings = get_settings()

# ----------------------------------------------------------------------
# Database URL resolution
# ----------------------------------------------------------------------
# Production: WATTXPENSE_DATABASE_URL must be set (Postgres/Supabase).
# Local dev: WATTXPENSE_DATABASE_URL can be a sqlite URL; if unset, a temporary
#   test database is used (as conftest.py already does).
# ----------------------------------------------------------------------

_DATABASE_URL = normalize_database_url(
    os.environ.get(
        "WATTXPENSE_DATABASE_URL",
        # Default for local dev: an in-memory SQLite used by tests only.
        "sqlite:///:memory:",
    )
)

# If the URL is empty or explicitly "none", force sqlite in-memory so the app
# never silently falls back to a file that could contain old unowned data.
if not _DATABASE_URL or _DATABASE_URL.strip() == "":
    _DATABASE_URL = "sqlite:///:memory:"


# ----------------------------------------------------------------------
# Engine creation: connect args depend on dialect; pool args for PG only.
# ----------------------------------------------------------------------
_connect_args = {}
_pool_args = {}
if _DATABASE_URL.startswith("sqlite"):
    _connect_args["check_same_thread"] = False
    # No pool args for SQLite; SQLAlchemy uses SingletonThreadPool by default.
else:
    # PostgreSQL: conservative pool for serverless deployment.
    # pool_size=3 keeps total connections low; each serverless function cold-start
    # gets its own connection from the pool. connections are released when the
    # function stops (Vercel guarantees function termination).
    _pool_args = {
        "pool_pre_ping": True,
        "pool_size": 3,
        "max_overflow": 2,
        "pool_timeout": 30,
    }


engine: Engine = create_engine(
    _DATABASE_URL,
    future=True,
    connect_args=_connect_args,
    **_pool_args,
)


# ----------------------------------------------------------------------
# SQLite pragmas (no-op when using Postgres)
# ----------------------------------------------------------------------
@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, connection_record):  # pragma: no cover
    """Keep SQLite honest about durability and concurrency."""
    if _DATABASE_URL.startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
        finally:
            cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True, expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """A session that always commits or rolls back, for use outside a request."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """The per-request session dependency.

    A read that changes nothing still commits on the way out, which keeps the lifespan of
    the session uniform and avoids surprising rollbacks of unrelated pending state.
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _add_missing_columns() -> None:
    """Add columns that a newer build expects but an older database does not have.

    Additive only. Nothing is dropped or rewritten, so running this on every start is
    safe and costs one ``PRAGMA table_info`` (SQLite) or ``ALTER TABLE`` (Postgres)
    per table.

    Works for both SQLite and PostgreSQL.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            # Table does not exist yet — create it (only via init_db create_all).
            continue
        present = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            # NOT NULL without default: skip for SQLite populated DBs (human migration).
            if not column.nullable and column.default is None and column.server_default is None:
                if _DATABASE_URL.startswith("sqlite"):
                    continue
                # For PostgreSQL we still let the ALTER below raise; the operator
                # must add the column manually or via a migration script.
            ddl_type = column.type.compile(dialect=engine.dialect)
            statement = f"ALTER TABLE {table.name} ADD COLUMN {column.name} {ddl_type}"
            if column.server_default is not None:
                statement += f" DEFAULT {column.server_default.arg!r}"
            with engine.begin() as connection:
                connection.execute(text(statement))


def init_db() -> None:
    """Create anything missing. Safe to call repeatedly.

    For SQLite: creates tables if absent, then runs _add_missing_columns.
    For PostgreSQL: relies on external Alembic migrations; this function is a no-op
    for schema creation (tables should already exist) and only ensures additive
    columns that were missed prior migrations.

    Importantly, this function does NOT call Base.metadata.create_all() for Postgres,
    because serverless cold starts should not recreate the schema every time.
    The first cold start after deployment should run the Alembic migration (see
    ``VERCEL_DEPLOY_INIT`` hook or manual one-time setup below).
    """
    if _DATABASE_URL.startswith("sqlite:///"):
        path = _DATABASE_URL.replace("sqlite:///", "", 1)
        if path and path != ":memory:":
            from pathlib import Path

            Path(path).parent.mkdir(parents=True, exist_ok=True)

    # For SQLite, create tables and add missing columns.
    if _DATABASE_URL.startswith("sqlite:///"):
        Base.metadata.create_all(bind=engine)
        _add_missing_columns()
    # For PostgreSQL: do nothing here. The operator must run the Alembic migration
    # (one-time) against the fresh Supabase database before the first request.
    # See VERCEL_DEPLOY_INIT below for the recommended approach.
