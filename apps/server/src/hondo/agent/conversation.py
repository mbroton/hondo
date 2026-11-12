from __future__ import annotations

import re
import time
from dataclasses import replace
from typing import Any, NotRequired, TypedDict

import structlog
from pydantic_ai._instructions import AgentInstructions
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.toolsets import FunctionToolset

from hondo.chat import ChatApiError, ChatClient, ChatMessage, ChatStyle
from hondo.context import current_scope, get_scope
from hondo.slack import SlackStyle
from hondo.users import UserDirectory, expand_mentions, mention_ids

logger = structlog.get_logger("hondo.agent.conversation")

# Slack's per-request maximum for conversations.history. Forward reads fetch
# the whole requested range in one call (see _read_channel) — a range bigger
# than this is rejected back to the model rather than paged.
_HISTORY_FETCH_MAX = 999

# History calls per search: about 10,000 top-level messages. All runs in a
# workspace share Slack's rate limit for history calls.
_SEARCH_MAX_FETCHES = 10
# No new history read starts after this. With normal Slack latency, a search
# then returns its hits before the 60 s tool limit (ToolBoundary) discards them.
_SEARCH_SECONDS = 30.0
_SEARCH_MAX_HITS = 50
_SNIPPET_CHARS = 200
_SNIPPET_LEAD = 50  # Characters before the first match.


# Return types of the read tools. Precise shapes on purpose: pydantic-ai
# derives each tool's return schema from the annotation, which CodeMode (and
# `include_return_schema`) surface to the model — `dict[str, Any]` would erase
# the shape at the source.
class Message(TypedDict):
    user: str
    name: str
    ts: str
    text: str
    replies: NotRequired[int]  # Size of the thread this message started.
    files: NotRequired[list[str]]  # File names. The model sees only names here.


class ChannelMore(TypedDict):
    """read_channel arguments that continue a truncated range."""

    before: NotRequired[str]
    after: NotRequired[str]


class ThreadMore(TypedDict):
    """read_thread arguments that continue a truncated thread."""

    root: NotRequired[str]
    after: str


class ChannelPage(TypedDict):
    messages: list[Message]
    more: NotRequired[ChannelMore]  # Absent when the whole range was returned.


class ThreadPage(TypedDict):
    messages: list[Message]
    more: NotRequired[ThreadMore]  # Absent when the whole thread was returned.


class SearchHit(Message):
    # `text` holds only the part of the message around the first match.
    date: str
    link: str


class SearchMore(TypedDict):
    """search_channel arguments that continue into older history."""

    before: str


class SearchPage(TypedDict):
    hits: list[SearchHit]
    matched: int  # All matching messages in the searched range, hits included.
    searched_since: NotRequired[str]  # Date of the oldest message searched; set with "more".
    more: NotRequired[SearchMore]  # Absent when the whole range was searched.


class ConversationCapability(AbstractCapability[Any]):
    """Slack conversation tools and context for a single agent run."""

    def __init__(
        self,
        *,
        client: ChatClient,
        directory: UserDirectory,
        style: ChatStyle | None = None,
    ) -> None:
        self.client = client
        self.directory = directory
        self.style: ChatStyle = style if style is not None else SlackStyle()

    def get_toolset(self) -> FunctionToolset[Any]:
        toolset: FunctionToolset[Any] = FunctionToolset(id="hondo")
        toolset.add_function(self._add_reaction, takes_ctx=False, name="add_reaction")
        toolset.add_function(self._post_in_thread, takes_ctx=False, name="post_in_thread")
        toolset.add_function(self._read_thread, takes_ctx=False, name="read_thread")
        toolset.add_function(self._read_channel, takes_ctx=False, name="read_channel")
        toolset.add_function(self._search_channel, takes_ctx=False, name="search_channel")
        toolset.add_function(self._lookup_user, takes_ctx=False, name="lookup_user")
        return toolset

    def get_instructions(self) -> AgentInstructions[Any]:
        # Prompt order (after the app's persona, which pydantic-ai always puts
        # first): who you are, how to write here, where you are right now.
        # Each fragment wraps itself in an XML section tag (<identity>,
        # <slack_conventions>, <current_session>) so the composed prompt
        # separates concerns the way Anthropic's prompting guide recommends.
        # The deployment instructions use the same structure (see
        # hondo.deployment).
        # Identity leads because the model must recognize its own @name/id in
        # every transcript line. All are callables on purpose: pydantic-ai
        # sorts static strings before dynamic parts, so a bare writing_rules
        # string would jump ahead of the identity line. The rendered text is
        # still stable up to the per-run tail (identity doesn't change between
        # runs in a workspace), so implicit prefix caching is unaffected; only
        # an explicit *_cache_instructions breakpoint loses the rules block.
        return [
            self._identity,
            self._writing_rules,
            self._session_context,
        ]

    def _writing_rules(self) -> str:
        return self.style.writing_rules

    def _identity(self) -> str:
        """The bot's @name and user id, stated once, up front.

        The id matters as much as the name — the model must recognize a raw
        <@U…> of itself in transcripts and permalinks (dogfooding: a bot that
        only knew its display name couldn't tell `@bot hey` was for it).
        Handle facts only, no persona: who the bot *is* belongs to the app's
        instructions — a second "You are …" here would duplicate it.
        """
        scope = get_scope()
        if scope is None:
            return ""
        bot = scope.bot_user
        if not bot:
            return "<identity>\nYou are an AI teammate in this workspace.\n</identity>"
        bot_name = scope.roster.get(bot)
        who = f"@{bot_name} ({self.style.mention(bot)})" if bot_name else self.style.mention(bot)
        return (
            f"<identity>\nIn this workspace you go by {who}. A message "
            f"containing {self.style.mention(bot)} mentions you.\n</identity>"
        )

    async def _session_context(self) -> str:
        scope = get_scope()
        if scope is None:
            return ""
        if scope.is_dm:
            where = "a direct message conversation"
        else:
            where = f"a thread in channel {self.style.channel_mention(scope.channel)}"
            info = await self.client.channel_info(scope.channel)
            if info is not None and info.is_channel:
                kind = "private" if info.is_private else "public"
                shared = ", shared with another organization" if info.is_ext_shared else ""
                where = f"{where} ({kind}{shared})"
        situation = f"Right now you are replying in {where}."
        # Names in message text are display names only; this is where their
        # Slack ids live, so the model can mention people back.
        people = [
            f"{name} ({self.style.mention(uid)})"
            for uid, name in scope.roster.items()
            if uid != scope.bot_user
        ]
        if people:
            situation = f"{situation}\nPeople here: {', '.join(people)}."
        return f"<current_session>\n{situation}\n</current_session>"

    async def _add_reaction(self, emoji: str) -> str:
        """React to the Slack message that triggered this run.

        Args:
            emoji: Emoji name without colons, e.g. "eyes" or "white_check_mark".
        """
        scope = current_scope()
        name = emoji.strip(":")
        try:
            await self.client.add_reaction(
                channel=scope.channel, message_ref=scope.event.ts, name=name
            )
        except ChatApiError as exc:
            # Repeat reactions are routine — the progress reporter's own :eyes:, or a
            # redelivered run re-running its tool calls. The desired state holds either
            # way, and raising would fail the run and poison the event for redelivery.
            if str(exc) != "already_reacted":
                raise
        return f"Reacted with :{name}:"

    async def _post_in_thread(self, text: str) -> str:
        """Post an extra Slack message in the current conversation, before your final reply.

        Your final answer is posted automatically. Use this only for interim updates.

        Args:
            text: Message text in Slack mrkdwn.
        """
        scope = current_scope()
        await self.client.post_message(
            channel=scope.channel, text=text, thread_ts=scope.reply_thread_ts
        )
        return "Posted."

    async def _read_thread(
        self, root: str | None = None, after: str | None = None, limit: int = 20
    ) -> ThreadPage:
        """Read a thread's messages, oldest first.

        Use this to catch up on the current conversation, or, with `root`, to open
        another thread found via read_channel.

        Args:
            root: ts of a thread's root message (read_channel marks messages that
                started a thread with a "replies" count). Omit for the current
                conversation.
            after: A message ts from the thread: return only messages newer than it.
            limit: Maximum number of messages to return.

        If the thread holds more than `limit` messages, the result's "more" key gives
        the exact arguments for the next read_thread call; no "more" key means you
        have seen the whole thread.
        """
        scope = current_scope()
        if root is None and scope.is_dm:
            # DMs have no thread structure: read recent DM history instead.
            # read_channel is the tool for paging deeper into a DM.
            page = await self.client.fetch_history(channel=scope.channel, limit=limit, oldest=after)
            return {"messages": await self._resolve_rows(page.messages)}
        thread_root = root or scope.thread_root
        fetched = await self.client.fetch_thread(
            channel=scope.channel, root=thread_root, limit=limit + 1, oldest=after
        )
        result: ThreadPage = {"messages": await self._resolve_rows(fetched[:limit])}
        if len(fetched) > limit and result["messages"]:
            result["more"] = {"root": thread_root, "after": result["messages"][-1]["ts"]}
        return result

    async def _read_channel(
        self, before: str | None = None, after: str | None = None, limit: int = 20
    ) -> ChannelPage:
        """Read top-level messages from the current channel, oldest first.

        Use this when the conversation references channel context you haven't seen:
        an alert posted above, "as discussed earlier", a decision made outside this
        thread. To find older messages about a topic, search_channel costs far fewer
        tokens. Thread replies are not included: a message that started a thread
        carries a "replies" count, and read_thread(root=<its ts>) opens it.

        Args:
            before: A message ts: return only messages older than it. Omit both
                bounds for the most recent messages.
            after: A message ts: return only messages newer than it, e.g. this
                thread's root ts to see what happened in the channel after this
                thread started.
            limit: Maximum number of messages to return.

        If the range holds more than `limit` messages, the result's "more" key gives
        the exact arguments for the next read_channel call ({"after": ...} continues
        forward, {"before": ...} backward); no "more" key means you have seen the
        whole range.
        """
        scope = current_scope()
        if after is not None:
            # Forward read: "what happened after X" wants the messages right
            # after X, not the newest of the range — but Slack serves bounded
            # ranges newest-first. Fetch the whole range in one maximal request
            # and keep its oldest end.
            page = await self.client.fetch_history(
                channel=scope.channel, limit=_HISTORY_FETCH_MAX, oldest=after, latest=before
            )
            if page.has_more:
                raise ModelRetry(
                    f"The requested range holds over {_HISTORY_FETCH_MAX} messages. "
                    "Pass a `before` bound (or a later `after`) to narrow it."
                )
            truncated = len(page.messages) > limit
            kept = page.messages[:limit]
        else:
            page = await self.client.fetch_history(
                channel=scope.channel, limit=limit + 1, latest=before
            )
            truncated = page.has_more or len(page.messages) > limit
            kept = page.messages[-limit:] if limit else []
        result: ChannelPage = {"messages": await self._resolve_rows(kept)}
        if truncated and kept:
            if after is not None:
                more: ChannelMore = {"after": kept[-1].ref}
                if before is not None:
                    more["before"] = before
            else:
                more = {"before": kept[0].ref}
            result["more"] = more
        return result

    async def _search_channel(
        self,
        words: list[str],
        before: str | None = None,
        limit: int = 20,
    ) -> SearchPage:
        """Search the current channel's top-level messages for words.

        Use this to find a past discussion, incident, alert or decision. A hit
        shows only the text around the first matched word; read_thread(root=<its
        ts>) shows the whole message and its replies. A thread matches only by
        the words of its first message, so when a search finds nothing, try
        other words people may have used ("outage", "down", "502").

        Args:
            words: Words or phrases to find. Case is ignored, and a word also
                matches longer forms ("timeout" finds "timeouts"). Messages with
                more of the words rank first, then messages with more replies,
                then newer ones. A mention matches by the user id ("U0123ABCDEF").
            before: A message ts: search only older messages.
            limit: Maximum number of hits, up to 50.

        "matched" counts all matching messages in the searched range. When it is
        larger than the number of hits, add words or raise `limit`. One search
        reads up to about 10,000 messages, newest first. If older messages
        remain, the result's "more" key gives the exact arguments (with the same
        words) for the next search_channel call, and "searched_since" tells how
        far back this search went.
        """
        scope = current_scope()
        terms = {_unescape(word.strip()).casefold() for word in words} - {""}
        if not terms:
            raise ModelRetry("Pass at least one word to search for.")
        # A term matches only where no letter or digit comes before it. "_" can:
        # _italic_ in Slack, snake_case names in alerts.
        patterns = [re.compile(rf"(?<![^\W_]){re.escape(term)}") for term in terms]
        matches: list[tuple[int, ChatMessage]] = []
        oldest_read: str | None = None
        has_more = False
        started = time.monotonic()
        for _ in range(_SEARCH_MAX_FETCHES):
            if oldest_read is not None and time.monotonic() - started > _SEARCH_SECONDS:
                break
            try:
                # No `oldest` bound: only then does a page hold the newest end
                # of the range, so each next page continues right below.
                page = await self.client.fetch_history(
                    channel=scope.channel,
                    limit=_HISTORY_FETCH_MAX,
                    latest=oldest_read or before,
                )
            except Exception:
                if oldest_read is None:
                    raise
                # Keep what the earlier pages found; "more" continues from there.
                logger.warning("channel search stopped early", exc_info=True)
                has_more = True
                break
            for message in page.messages:
                text = _unescape(message.text).casefold()
                matched = sum(1 for pattern in patterns if pattern.search(text))
                if matched:
                    matches.append((matched, message))
            if page.messages:
                oldest_read = page.messages[0].ref
            has_more = page.has_more and bool(page.messages)
            if not has_more:
                break
        matches.sort(key=lambda m: (m[0], m[1].reply_count, m[1].ref), reverse=True)
        # Cut before name resolution: look up only the names the model sees.
        top = [
            replace(message, text=_snippet(message.text, patterns))
            for _, message in matches[: min(max(limit, 1), _SEARCH_MAX_HITS)]
        ]
        hits: list[SearchHit] = []
        for row in await self._resolve_rows(top):
            hit: SearchHit = {
                **row,
                "date": self.style.format_time(row["ts"]),
                "link": self.style.message_link(scope.channel, row["ts"]),
            }
            hits.append(hit)
        result: SearchPage = {"hits": hits, "matched": len(matches)}
        if has_more and oldest_read is not None:
            result["more"] = {"before": oldest_read}
            result["searched_since"] = self.style.format_time(oldest_read)
        return result

    async def _resolve_rows(self, messages: list[ChatMessage]) -> list[Message]:
        """Render ChatMessages for the model: display names resolved, mentions expanded."""
        names = await self.directory.resolve_many(
            {message.author for message in messages if message.author}
            | mention_ids(message.text for message in messages)
        )
        current_scope().roster.update(names)
        rows: list[Message] = []
        for message in messages:
            row: Message = {
                "user": message.author or "unknown",
                "name": names.get(message.author, ""),
                "ts": message.ref,
                "text": expand_mentions(message.text, names),
            }
            if message.reply_count:
                row["replies"] = message.reply_count
            if message.files:
                row["files"] = [file.name for file in message.files]
            rows.append(row)
        return rows

    async def _lookup_user(self, user_id: str) -> str:
        """Look up who a Slack user id refers to.

        Use when you see an id you can't place: in a pasted permalink, old
        history, or a mention that didn't resolve.

        Args:
            user_id: Slack user id, e.g. "U0123ABCDEF" (a raw <@U0123ABCDEF> works too).
        """
        cleaned = user_id.strip().removeprefix("<@").removesuffix(">").split("|")[0]
        info = await self.directory.info(cleaned)
        if info is None:
            return f"No Slack user found for '{user_id}'."
        scope = get_scope()
        if scope is not None:
            scope.roster[info.id] = info.name
        kind = "bot" if info.is_bot else "person"
        real = (
            f", real name {info.real_name}"
            if info.real_name and info.real_name != info.name
            else ""
        )
        return f"{self.style.mention(info.id)} is @{info.name} ({kind}{real})."


def _snippet(text: str, patterns: list[re.Pattern[str]]) -> str:
    """The part of a raw message text around its first match."""
    if len(text) <= _SNIPPET_CHARS:
        return text
    searchable, raw_index = _searchable(text)
    first = min(
        (raw_index[match.start()] for pattern in patterns if (match := pattern.search(searchable))),
        default=0,
    )
    start = max(0, min(first - _SNIPPET_LEAD, len(text) - _SNIPPET_CHARS))
    end = start + _SNIPPET_CHARS
    # Slack escapes a typed "<" and ">", so they always open and close a
    # mention or a link. Keep one that the window splits whole when it holds
    # the match, and drop it otherwise.
    opened = text.rfind("<", 0, start)
    closed = text.find(">", opened) if opened != -1 else -1
    if closed >= start:
        start = opened if closed > first else closed + 1
    opened = text.rfind("<", start, end)
    closed = text.find(">", opened) if opened != -1 else -1
    if opened != -1 and (closed == -1 or closed >= end):
        end = closed + 1 if opened <= first < closed else opened
    return ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")


def _searchable(text: str) -> tuple[str, list[int]]:
    """_unescape(text).casefold(), and the index in `text` of each of its characters."""
    chars: list[str] = []
    raw_index: list[int] = []
    position = 0
    for entity in [*_ENTITY.finditer(text), None]:
        stop = entity.start() if entity else len(text)
        for index in range(position, stop):
            # Casefold can make a letter longer: "ß" becomes "ss".
            folded = text[index].casefold()
            chars.append(folded)
            raw_index += [index] * len(folded)
        if entity:
            chars.append(_ENTITY_CHARS[entity[1]])
            raw_index.append(entity.start())
            position = entity.end()
    return "".join(chars), raw_index


_ENTITY = re.compile(r"&(amp|lt|gt);")
_ENTITY_CHARS = {"amp": "&", "lt": "<", "gt": ">"}


def _unescape(text: str) -> str:
    """Undo Slack's escaping of &, < and > in message text."""
    return _ENTITY.sub(lambda entity: _ENTITY_CHARS[entity[1]], text)
