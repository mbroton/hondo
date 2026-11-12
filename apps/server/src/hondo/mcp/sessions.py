from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Self
from uuid import UUID

from mcp import ClientSession

from hondo.mcp.auth import MCPAuth
from hondo.mcp.models import MCPConnection, MCPError

CLOSE_TIMEOUT = 5


@dataclass(frozen=True)
class OpenSession:
    session: ClientSession
    auth: MCPAuth


type Connect = Callable[[MCPConnection], AbstractAsyncContextManager[OpenSession]]


@dataclass(frozen=True)
class HeldSession:
    opened: OpenSession
    task: asyncio.Task[None]
    stop: asyncio.Event

    async def send[T](self, request: Awaitable[T]) -> T:
        return await _unless_ended(asyncio.ensure_future(request), self.task)


class MCPSessions:
    """The open MCP sessions of one agent run, one per connection.

    Like other MCP clients, a run initializes a server once, then sends each
    call on that session. The last user to leave closes the sessions: the run
    when it ends, or a call made outside a run.
    """

    def __init__(self, connect: Connect) -> None:
        self.connect = connect
        self._users = 0
        self._held: dict[UUID, HeldSession] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        # A session's auth reports the requests of one call at a time.
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> Self:
        self._users += 1
        return self

    async def __aexit__(self, *args: Any) -> None:
        self._users -= 1
        if self._users == 0:
            await self._close()

    @asynccontextmanager
    async def lease[T](
        self, saved: MCPConnection, first: Callable[[ClientSession], Awaitable[T]]
    ) -> AsyncIterator[tuple[HeldSession, T]]:
        """Hold the connection's session for one call, after its first request.

        The first request must only read: on a reused session it can go twice.
        """
        async with self, self._lock:
            held = self._held.get(saved.id)
            if held is not None and held.task.done():
                held = None
            while True:
                fresh = held is None
                if held is None:
                    held = await self._open(saved)
                try:
                    result = await held.send(first(held.opened.session))
                    break
                except BaseException as exc:
                    await self._discard(saved, held)
                    # A server can end a session between calls, and not every
                    # server says so with a 404. A new session gets one try.
                    if fresh or not isinstance(exc, Exception):
                        raise
                    held = None
            try:
                yield held, result
            except BaseException:
                # The session can still send a request that this call gave up
                # on. Close it before the call records what happened.
                await self._discard(saved, held)
                raise

    async def _discard(self, saved: MCPConnection, held: HeldSession) -> None:
        self._held.pop(saved.id, None)
        held.stop.set()
        try:
            await asyncio.wait((held.task,), timeout=CLOSE_TIMEOUT)
        finally:
            held.task.cancel()

    async def _open(self, saved: MCPConnection) -> HeldSession:
        ready: asyncio.Future[OpenSession] = asyncio.get_running_loop().create_future()
        stop = asyncio.Event()
        task = asyncio.create_task(self._hold(saved, ready, stop))
        self._tasks.add(task)
        task.add_done_callback(self._ended)
        try:
            opened = await _unless_ended(ready, task)
        except BaseException:
            task.cancel()
            raise
        held = HeldSession(opened, task, stop)
        self._held[saved.id] = held
        return held

    async def _hold(
        self, saved: MCPConnection, ready: asyncio.Future[OpenSession], stop: asyncio.Event
    ) -> None:
        # The SDK's task groups must close in the task that opened them, so a
        # session stays in its own task, not in the call that needed it first.
        async with self.connect(saved) as opened:
            if not ready.done():
                ready.set_result(opened)
                await stop.wait()

    def _ended(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        # A call that waits on this session raises the error itself. A session
        # that fails between calls is replaced by the next call.
        if not task.cancelled():
            task.exception()

    async def _close(self) -> None:
        for held in self._held.values():
            held.stop.set()
        self._held.clear()
        tasks = set(self._tasks)
        if not tasks:
            return
        try:
            await asyncio.wait(tasks, timeout=CLOSE_TIMEOUT)
        finally:
            for task in tasks:
                task.cancel()


async def _unless_ended[T](work: asyncio.Future[T], session: asyncio.Task[None]) -> T:
    """Wait for work on a session, or raise the error that ended the session.

    The SDK sends each request from the session's task, so an error while
    sending (a refused token, a lost connection) ends that task and does not
    reach the request.
    """
    try:
        await asyncio.wait((work, session), return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        _drop(work)
        raise
    if work.done():
        return work.result()
    _drop(work)
    error = None if session.cancelled() else session.exception()
    raise error or MCPError("connection_unavailable")


def _drop(work: asyncio.Future[Any]) -> None:
    work.cancel()
    work.add_done_callback(lambda done: done.cancelled() or done.exception())
