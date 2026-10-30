"""Database URL handling.

The deployed runtime only bundles psycopg2, but Supabase hands out bare
``postgresql://`` URLs which SQLAlchemy 2.1 maps to psycopg 3. These tests pin
the normalisation that keeps production from failing at import time.
"""

from __future__ import annotations

import pytest

from backend.app.config import normalize_database_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("postgres://u:p@h:5432/db", "postgresql+psycopg2://u:p@h:5432/db"),
        ("postgresql://u:p@h:5432/db", "postgresql+psycopg2://u:p@h:5432/db"),
        ("  postgresql://u:p@h/db  ", "postgresql+psycopg2://u:p@h/db"),
        ("postgresql+psycopg2://u:p@h/db", "postgresql+psycopg2://u:p@h/db"),
        ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("sqlite:///:memory:", "sqlite:///:memory:"),
        ("", ""),
    ],
)
def test_normalize_database_url(raw: str, expected: str) -> None:
    assert normalize_database_url(raw) == expected
