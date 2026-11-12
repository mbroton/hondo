from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field

from hondo.models import Event, SessionKey, Todo


@dataclass
class TodoState:
    """The run's live task list and the card message it renders to."""

    items: list[Todo] = field(default_factory=list)
    card_ref: str | None = None


@dataclass
class WaitClock:
    """Time the run spends waiting for a person, which its time limits do not count."""

    since: float | None = None
    total: float = 0.0
    resumed_at: float = 0.0

    @contextmanager
    def waiting(self) -> Iterator[None]:
        self.since = time.monotonic()
        try:
            yield
        finally:
            self.resumed_at = time.monotonic()
            self.total += self.resumed_at - self.since
            self.since = None


@dataclass(frozen=True)
class RunScope:
    key: SessionKey
    event: Event
    # Shared, mutable within the run: user id → display name for everyone seen
    # in this session. The runner fills it while building prompts; instructions
    # and tools read it. Persisted on the Session row at run end.
    roster: dict[str, str] = field(default_factory=dict)
    # Shared mutable run state written by the write_todos tool.
    todos: TodoState = field(default_factory=TodoState)
    wait: WaitClock = field(default_factory=WaitClock)

    @property
    def bot_user(self) -> str | None:
        return self.event.bot_user

    @property
    def channel(self) -> str:
        return self.key.channel

    @property
    def thread_root(self) -> str:
        return self.key.thread_root

    @property
    def is_dm(self) -> bool:
        return self.event.is_dm

    @property
    def requester(self) -> str | None:
        return self.event.user

    @property
    def reply_thread_ts(self) -> str | None:
        # DMs are not threaded; channel replies always go in the session's thread.
        return None if self.is_dm else self.thread_root


_scope: ContextVar[RunScope | None] = ContextVar("hondo_run_scope", default=None)


def current_scope() -> RunScope:
    scope = _scope.get()
    if scope is None:
        raise RuntimeError(
            "No Hondo run scope is active. Hondo tools and instructions can only "
            "run inside a session driven by the Hondo runtime."
        )
    return scope


def get_scope() -> RunScope | None:
    return _scope.get()


def set_scope(scope: RunScope) -> Token[RunScope | None]:
    return _scope.set(scope)


def reset_scope(token: Token[RunScope | None]) -> None:
    _scope.reset(token)
