"""Engine, session, schema creation and additive migrations.

The engine is built at import time, which is why the test suite sets its environment
before importing anything from `backend.app`.

Existing installs are migrated by adding columns rather than recreating tables, so a
database written by an older build keeps its readings and its tariff.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app.config import get_settings
from backend.app.models import Base, Device, Reading, Tariff  # noqa: F401  (re-exported)

_settings = get_settings()

if _settings.data_dir and not _settings.database_url.startswith("sqlite"):
    _settings.data_dir.mkdir(parents=True, exist_ok=True)

engine: Engine = create_engine(
    _settings.database_url,
    future=True,
    # SQLite handles concurrent readers fine and serialises writers; a single connection
    # pool keeps the simulator's batch inserts from tripping over "database is locked".
    connect_args={"check_same_thread": False} if _settings.database_url.startswith("sqlite") else {},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, connection_record):  # pragma: no cover - driver hook
    """Keep SQLite honest about durability and concurrency."""
    if _settings.database_url.startswith("sqlite"):
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
    safe and costs one `PRAGMA table_info` per table.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        present = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            # A NOT NULL column with no default cannot be added to a populated SQLite
            # table, so those are left to a human rather than failing the boot.
            if not column.nullable and column.default is None and column.server_default is None:
                continue
            ddl_type = column.type.compile(dialect=engine.dialect)
            statement = f"ALTER TABLE {table.name} ADD COLUMN {column.name} {ddl_type}"
            if column.server_default is not None:
                statement += f" DEFAULT {column.server_default.arg!r}"
            with engine.begin() as connection:
                connection.execute(text(statement))


def init_db() -> None:
    """Create anything missing. Safe to call repeatedly."""
    settings = get_settings()
    if settings.database_url.startswith("sqlite:///"):
        path = settings.database_url.replace("sqlite:///", "", 1)
        if path and path != ":memory:":
            from pathlib import Path

            Path(path).parent.mkdir(parents=True, exist_ok=True)

    Base.metadata.create_all(bind=engine)
    _add_missing_columns()
