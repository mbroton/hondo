import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from testcontainers.postgres import PostgresContainer

from hondo.api.store import ControlStore
from hondo.chat import ChatMessage, ChatUser
from hondo.config import Config
from hondo.logging import configure_logging
from hondo.store.migrate import upgrade_to_head
from hondo.store.postgres import PostgresStore


@pytest.fixture(autouse=True)
def _isolate_config_from_local_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Config applies its env/.env sources even under model_validate, so any
    # test-built Config silently inherits the developer's real environment
    # (e.g. HONDO_CRYPTO__TOKEN_KEY breaking the missing-key seed test).
    # Tests must be fully determined by the values they pass explicitly.
    monkeypatch.setitem(Config.model_config, "env_file", None)
    for name in list(os.environ):
        if name.startswith("HONDO_"):
            monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def _configure_logging() -> None:
    # Tests must log like the product does. Unconfigured structlog renders
    # exceptions with RichTracebackFormatter(show_locals=True), and one
    # exc_info log with agent-sized locals costs ~15s of CPU while blocking
    # the loop (see hondo.logging) — enough to blow the integration tests'
    # timing budget and slow the failure-path suites to a crawl.
    # Per test, not per session: test_logging.py resets structlog in its
    # finalizers, which would silently revert every test after it.
    configure_logging()


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    """DSN for a test-only Postgres: a throwaway container, torn down after the session.

    Deliberately not the dev database from docker-compose — a live bot on a shared
    database claims test events via NOTIFY and races the suite. Set
    HONDO_TEST_DATABASE_URL to reuse an external database (e.g. a CI service
    container) instead of starting one.
    """
    override = os.environ.get("HONDO_TEST_DATABASE_URL")
    if override:
        upgrade_to_head(override)
        yield override
        return
    with PostgresContainer("pgvector/pgvector:pg16", driver=None) as pg:
        dsn = pg.get_connection_url()
        upgrade_to_head(dsn)
        yield dsn


@pytest.fixture
async def store(pg_dsn: str) -> AsyncIterator[PostgresStore]:
    s = PostgresStore(pg_dsn)
    await s.connect(run_migrations=False, pool_min_size=1, pool_max_size=1)
    await s._connected_pool.execute(
        # RESTART IDENTITY: tests assert summary ids, so each test starts the
        # id sequence fresh.
        "TRUNCATE events, sessions, session_messages, steer_mail, run_notices,"
        " run_snapshots, thread_summaries, usage_events, users, auth_sessions,"
        " workspaces RESTART IDENTITY CASCADE"
    )
    yield s
    await s.aclose()


@pytest.fixture
async def control_store(pg_dsn: str) -> AsyncIterator[ControlStore]:
    store = ControlStore(pg_dsn)
    await store.connect(run_migrations=False, pool_min_size=1, pool_max_size=1)
    await store._connected_pool.execute(
        "TRUNCATE auth_sessions, users, workspaces RESTART IDENTITY CASCADE"
    )
    yield store
    await store.aclose()


class FakeSlackClient:
    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []
        self.reactions: list[dict[str, Any]] = []
        self.thread_messages: list[ChatMessage] = []

    async def post_message(self, *, channel: str, text: str, thread_ts: str | None = None) -> str:
        return self._record_post(channel=channel, text=text, thread_ts=thread_ts, blocks=None)

    async def post_notice(self, *, channel: str, text: str, thread_ts: str | None = None) -> str:
        blocks = [{"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}]
        return self._record_post(channel=channel, text=text, thread_ts=thread_ts, blocks=blocks)

    def _record_post(
        self, *, channel: str, text: str, thread_ts: str | None, blocks: list[dict[str, Any]] | None
    ) -> str:
        ts = f"{1700000001 + len(self.posted) + 1}.000000"
        self.posted.append(
            {"channel": channel, "text": text, "thread_ts": thread_ts, "blocks": blocks, "ts": ts}
        )
        return ts

    async def add_reaction(self, *, channel: str, message_ref: str, name: str) -> None:
        self.reactions.append({"channel": channel, "name": name, "message_ref": message_ref})

    async def fetch_thread(
        self, *, channel: str, root: str, limit: int | None = None
    ) -> list[ChatMessage]:
        return self.thread_messages

    async def fetch_history(self, *, channel: str, limit: int | None = None) -> list[ChatMessage]:
        # Like Slack, history returns newest-first (unlike replies, which is oldest-first).
        return self.thread_messages

    async def user_info(self, *, user_id: str) -> ChatUser:
        if user_id == "U01":
            return ChatUser(id="U01", name="jane", real_name="Jane Doe", is_bot=False)
        raise RuntimeError(f"user_info: no such user {user_id}")


@pytest.fixture
def slack_client() -> FakeSlackClient:
    return FakeSlackClient()
