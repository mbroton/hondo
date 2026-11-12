from __future__ import annotations

from typing import Any

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import FunctionToolset

from hondo.context import current_scope
from hondo.models import ThreadFollow
from hondo.store.base import Store


class ThreadFollowCapability(AbstractCapability[Any]):
    """The thread switch people set in Slack: answer only when mentioned, or follow again."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def get_toolset(self) -> FunctionToolset[Any]:
        toolset: FunctionToolset[Any] = FunctionToolset(id="hondo_follow")
        toolset.add_function(self._set_mention_only, takes_ctx=False, name="set_mention_only")
        return toolset

    async def _set_mention_only(self, mention_only: bool) -> str:
        """Set whether you answer in this thread only when someone mentions you.

        Use it when someone asks you to stay out of this thread unless they
        mention you (mention_only=true), or to follow the thread again
        (mention_only=false). While it is on, you do not see thread messages
        that do not mention you. Confirm the change in one short line.
        """
        scope = current_scope()
        if scope.is_dm:
            return "Not changed: in a direct message, every message is for you."
        if not mention_only and await self.store.mention_only_channel(
            scope.key.team, scope.channel
        ):
            return (
                "Not changed: an admin set this channel so you answer only when "
                "mentioned. Only an admin can change it, in Hondo's settings."
            )
        await self.store.ensure_session(scope.key)
        await self.store.merge_context_key(
            scope.key, ThreadFollow.CONTEXT_KEY, ThreadFollow.patch(mention_only=mention_only)
        )
        if mention_only:
            return "Done: in this thread you now answer only messages that mention you."
        return "Done: you follow this thread again and judge each reply."
