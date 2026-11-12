from __future__ import annotations

from datetime import UTC
from typing import Any, TypedDict

from pydantic_ai._instructions import AgentInstructions
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.toolsets import FunctionToolset

from hondo.chat import ChatApiError, ChatClient, ChatStyle, is_public
from hondo.context import current_scope
from hondo.store.base import SummaryStore, SummaryView

_SEARCH_MAX_HITS = 50


class SummaryHit(TypedDict):
    summary: int
    date: str  # When the thread started.
    channel: str
    link: str  # The thread.
    text: str  # Only the part of the summary around the matched words.


class SummaryPage(TypedDict):
    hits: list[SummaryHit]
    matched: int  # All matching summaries, hits included.


class SummaryRead(TypedDict):
    summary: int
    date: str  # When the thread started.
    updated: str
    channel: str
    link: str  # The thread.
    text: str


SUMMARY_RULES = """\
<thread_summaries>
Hondo keeps a short summary of each channel thread it took part in: what the \
thread was about, what people found or decided, and what is still open. Each \
summary links to its thread. Summaries from public channels show in every \
channel; summaries from a private channel, or from a channel shared with \
another organization, show only in that channel. Direct messages get no summary.

- Search the summaries (summary_search) when a question may touch something the \
team discussed before. summary_read shows a whole summary.
- Search finds a summary only by its words. Use the words people would have \
used, and name the services.
- A summary is shorter than its thread and can leave out details. Link to the \
thread when you use it.
- There is no tool to save a note. The summary of this thread keeps what people \
say here.
</thread_summaries>
"""


class SummaryCapability(AbstractCapability[Any]):
    def __init__(self, store: SummaryStore, client: ChatClient, style: ChatStyle) -> None:
        self.store = store
        self.client = client
        self.style = style

    def get_toolset(self) -> FunctionToolset[Any]:
        toolset: FunctionToolset[Any] = FunctionToolset(id="hondo_summaries")
        toolset.add_function(self._summary_search, takes_ctx=False, name="summary_search")
        toolset.add_function(self._summary_read, takes_ctx=False, name="summary_read")
        return toolset

    def get_instructions(self) -> AgentInstructions[Any]:
        return [self._summary_rules]

    def _summary_rules(self) -> str:
        return SUMMARY_RULES

    async def _here(self) -> SummaryView:
        scope = current_scope()
        info = await self.client.channel_info(scope.channel)
        if info is None:
            raise ChatApiError("Slack did not say what kind of conversation this is")
        # A channel shared with another organization sees only its own
        # summaries: the other organization must not read this team's threads.
        return SummaryView(
            team=scope.key.team, channel=scope.channel, include_public=not info.is_ext_shared
        )

    async def _still_public(self, view: SummaryView, channel: str) -> bool:
        # The stored flag is only a first filter: a channel can become private
        # or shared after its summaries were written. Unknown counts as private.
        if channel == view.channel:
            return True
        return is_public(await self.client.channel_info(channel))

    async def _summary_search(self, words: list[str], limit: int = 10) -> SummaryPage:
        """Search the summaries of earlier threads.

        A hit shows only the text around the matched words; summary_read(summary)
        shows the whole summary. When a search finds nothing, try other words
        people may have used ("outage", "down", "502").

        Args:
            words: Words or phrases to find. Case is ignored, and a word also
                matches other forms ("timeouts" finds "timeout"). Summaries
                with more of the words rank first.
            limit: Maximum number of hits, up to 50.
        """
        terms = [word.strip() for word in words if word.strip()]
        if not terms:
            raise ModelRetry("Pass at least one word to search for.")
        view = await self._here()
        found = await self.store.search_summaries(view, terms, min(max(limit, 1), _SEARCH_MAX_HITS))
        public = {
            channel: await self._still_public(view, channel)
            for channel in {match.channel for match in found.matches}
        }
        hits = [
            SummaryHit(
                summary=match.id,
                date=self.style.format_time(match.thread_ts),
                channel=self.style.channel_mention(match.channel),
                link=self.style.message_link(match.channel, match.thread_ts),
                text=match.snippet,
            )
            for match in found.matches
            if public[match.channel]
        ]
        hidden = len(found.matches) - len(hits)
        return SummaryPage(hits=hits, matched=found.total - hidden)

    async def _summary_read(self, summary: int) -> SummaryRead | str:
        """Read one thread summary.

        Args:
            summary: The summary number from summary_search.
        """
        view = await self._here()
        found = await self.store.get_summary(view, summary)
        if found is None or not await self._still_public(view, found.channel):
            return f"There is no summary {summary} that this conversation can see."
        return SummaryRead(
            summary=found.id,
            date=self.style.format_time(found.thread_ts),
            updated=found.updated_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            channel=self.style.channel_mention(found.channel),
            link=self.style.message_link(found.channel, found.thread_ts),
            text=found.text,
        )
