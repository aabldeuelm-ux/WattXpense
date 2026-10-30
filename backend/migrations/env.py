import os
import sys
import logging

from alembic import context
from sqlalchemy import engine_from_config, pool
from logging.config import fileConfig

# this is the Alembic Config object, which provides
# access to the values within the .ini file.
config = context.config

# Interpret the config file for Python logging.
# This line configures logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add the model's metadata to the target metadata so that 'autogenerate' can detect
# added table columns/changes
from backend.app.config import normalize_database_url
from backend.app.models import Base
target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# config.get_main_option("sqlalchemy.url")
# etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This mode is getting URL-agnostic SQL script output.
    URL is given as a string resource. No DBAPI is required.
    """
    url = os.environ.get("WATTXPENSE_DATABASE_URL")
    if not url:
        print("ERROR: WATTXPENSE_DATABASE_URL environment variable not set.")
        sys.exit(1)
    url = normalize_database_url(url)
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this mode we need to connect to the database engine.
    """
    url = os.environ.get("WATTXPENSE_DATABASE_URL")
    if not url:
        print("ERROR: WATTXPENSE_DATABASE_URL environment variable not set.")
        sys.exit(1)
    url = normalize_database_url(url)

    connectable = engine_from_config(
        {"sqlalchemy": {"url": url}},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()
