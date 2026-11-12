from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable

import structlog

from hondo.chat import ChatClient

logger = structlog.get_logger("hondo.progress")

# Slack removes the status 2 minutes after the last call, and whenever
# anything posts in the thread: also the task card and tools, which the
# reporter does not see. A short timer brings it back soon after.
STATUS_REFRESH_SECONDS = 3.0
# A post waits for a status call in flight, and the run's end waits for the
# clear while it holds its thread's queue slot.
STATUS_CALL_TIMEOUT_SECONDS = 3.0


# In a channel thread Slack shows a fixed "<app name> is working" and not this
# text, so there is nothing to gain from a more detailed one.
STATUS_TEXT = "is working…"


class ProgressReporter:
    """Conversation-shaped progress for one engaged session run.

    Messages are append-only, and there are no reactions. The acknowledgment
    is the model's own opening line ("on it, …", mandated by the writing
    rules), posted like any other narration; the final answer is the last
    message. Failures are posted via `post_notice` as muted system notes in
    the third person — the adapter owns their rendering. Each post runs as a
    recorded workflow step, so nothing double-posts on crash recovery.

    While the run works, a transient status line under the thread says so.
    It is not a message, and it is never recorded: it lives only as long as
    `keep_status` keeps sending it.
    """

    def __init__(
        self,
        client: ChatClient,
        *,
        channel: str,
        thread_ts: str | None,
    ) -> None:
        self._client = client
        self._channel = channel
        self._thread_ts = thread_ts
        self._posted = asyncio.Event()
        # A status call and a post never overlap: a status that reaches Slack
        # after the post that should clear it shows below that post.
        self._sending = asyncio.Lock()
        self._status_sent = False

    async def post(self, text: str) -> str | None:
        """Post one message (interim narration or the final answer); returns its ts."""
        async with self._sending:
            ts = await self._client.post_message(
                channel=self._channel, text=text, thread_ts=self._thread_ts
            )
        # Slack clears the status when Hondo posts: send it again at once.
        self._posted.set()
        return ts

    async def post_notice(self, text: str) -> str | None:
        """Post a system notice, visually distinct from the agent's messages.

        The adapter owns its platform-specific rendering.
        """
        return await self._client.post_notice(
            channel=self._channel,
            text=text,
            thread_ts=self._thread_ts,
        )

    async def keep_status(self, working: Callable[[], bool]) -> None:
        """Send the working line while `working()` is true, on a timer and after each post.

        Runs until cancelled. When Hondo dies, the sends stop and Slack removes
        the status by itself. A DM has no thread, so it gets no status.
        """
        if self._thread_ts is None:
            return
        warned = False
        while True:
            self._posted.clear()
            async with self._sending:
                if working():
                    self._status_sent = True
                    try:
                        async with asyncio.timeout(STATUS_CALL_TIMEOUT_SECONDS):
                            await self._client.set_status(
                                channel=self._channel,
                                thread_ts=self._thread_ts,
                                text=STATUS_TEXT,
                            )
                    except Exception:
                        if not warned:
                            logger.warning("working status not shown", exc_info=True)
                            warned = True
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._posted.wait(), STATUS_REFRESH_SECONDS)

    async def clear_status(self) -> None:
        """Remove the working line if one was sent. Never raises."""
        if self._thread_ts is None or not self._status_sent:
            return
        try:
            async with asyncio.timeout(STATUS_CALL_TIMEOUT_SECONDS):
                await self._client.set_status(
                    channel=self._channel, thread_ts=self._thread_ts, text=""
                )
        except Exception:
            logger.warning("working status not cleared", exc_info=True)
