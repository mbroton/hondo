"""Programmatic Alembic runner for `hondo migrate`, server boot, and store.connect().

Raw-SQL migrations live in hondo/migrations with the application. This
module owns the mechanics: build the Alembic config pointing at the packaged
scripts, open a synchronous connection (SQLAlchemy + psycopg — both already
present via dbos), and upgrade to head. Async callers run it in a thread.
"""

from __future__ import annotations

from importlib.resources import files

import structlog

logger = structlog.get_logger("hondo.store.migrate")


def _sync_url(dsn: str) -> str:
    """Normalize a Postgres DSN to an explicit SQLAlchemy+psycopg URL."""
    for prefix in ("postgresql://", "postgres://"):
        if dsn.startswith(prefix):
            return "postgresql+psycopg://" + dsn.removeprefix(prefix)
    return dsn


def upgrade_to_head(dsn: str) -> None:
    """Apply all pending migrations. Safe under concurrent callers (advisory lock)."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine

    config = Config()
    config.set_main_option("script_location", str(files("hondo") / "migrations"))
    engine = create_engine(_sync_url(dsn))
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            connection.commit()
    finally:
        engine.dispose()
    logger.debug("migrations at head")
