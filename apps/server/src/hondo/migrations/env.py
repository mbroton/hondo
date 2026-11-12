"""Alembic environment: raw-SQL migrations over a plain connection.

No ORM metadata and no autogenerate: Hondo's SQL is hand-written. The caller
(hondo.store.migrate) passes an open connection via `config.attributes`; the
advisory lock serializes concurrent workers racing to migrate on startup.
"""

from __future__ import annotations

from alembic import context

MIGRATION_LOCK_KEY = int.from_bytes(b"Hndo")


def run_migrations_online() -> None:
    connection = context.config.attributes.get("connection")
    if connection is None:
        raise RuntimeError(
            "hondo migrations run programmatically; use `hondo migrate` or "
            "hondo.store.migrate.upgrade_to_head()"
        )
    context.configure(connection=connection)
    with context.begin_transaction():
        connection.exec_driver_sql("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_KEY,))
        context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("offline mode is not supported; hondo migrations need a live database")
run_migrations_online()
