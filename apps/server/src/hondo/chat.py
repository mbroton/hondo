"""Chat contracts the core speaks; vendor adapters (slack.py) translate."""

from dataclasses import dataclass
from typing import Protocol, TypeGuard


class ChatApiError(Exception):
    """A chat platform API call failed; str(exc) is the platform's error code/text."""


@dataclass(frozen=True)
class ChatFile:
    name: str
    # Where the model's copy of the image downloads from; None for a file the
    # model cannot see.
    image_url: str | None = None
    image_type: str | None = None


@dataclass(frozen=True)
class ChatMessage:
    author: str  # Platform user id; "" when unknown.
    text: str
    ref: str  # Platform message handle (Slack: the ts).
    reply_count: int = 0  # Size of the thread this message started; 0 when none.
    files: tuple[ChatFile, ...] = ()


@dataclass(frozen=True)
class ChatHistoryPage:
    messages: list[ChatMessage]  # Oldest first.
    has_more: bool  # The requested range holds more messages than were returned.


@dataclass(frozen=True)
class ChatUser:
    id: str
    name: str  # Display name, never empty (falls back to id).
    real_name: str  # "" when unknown.
    is_bot: bool


@dataclass(frozen=True)
class ChannelInfo:
    is_channel: bool  # A public or private channel; not a direct or group message.
    is_private: bool
    is_ext_shared: bool  # Shared with another organization (Slack Connect).
    name: str | None = None


def is_public(info: ChannelInfo | None) -> TypeGuard[ChannelInfo]:
    """A public channel that no other organization shares. Unknown counts as private."""
    return info is not None and not info.is_private and not info.is_ext_shared


class ChatStyle(Protocol):
    """How to write things on this platform — prose and formatting only;
    sending is ChatClient's job.
    """

    @property
    def writing_rules(self) -> str:
        """System-prompt fragment: message conventions for this platform.

        Must wrap itself in an XML section tag (Slack uses
        <slack_conventions>) — every fragment of the composed prompt is a
        tagged section.
        """
        ...

    def mention(self, user_id: str) -> str:
        """Write "@someone" so it notifies."""
        ...

    def channel_mention(self, channel_id: str) -> str:
        """Reference a channel."""
        ...

    def message_link(self, channel: str, ts: str) -> str:
        """Link to a message."""
        ...

    def format_time(self, ts: str) -> str:
        """Render a platform message timestamp as human-readable UTC."""
        ...


class ChatClient(Protocol):
    """Generic outbound verbs; one adapter per platform implements them."""

    # Returns the posted message's ref, or None if the platform gave none.
    async def post_message(
        self, *, channel: str, text: str, thread_ts: str | None = None
    ) -> str | None: ...

    # Small, muted system note, visually distinct from the bot speaking.
    async def post_notice(
        self, *, channel: str, text: str, thread_ts: str | None = None
    ) -> str | None: ...

    # Edit a previously posted message in place. For mutable status surfaces
    # only — conversation messages stay append-only.
    # Raises ChatApiError on API failure.
    async def update_message(self, *, channel: str, message_ref: str, text: str) -> None: ...

    # Raises ChatApiError on API failure.
    async def add_reaction(self, *, channel: str, message_ref: str, name: str) -> None: ...

    # Transient "working" line under a thread, not a message; "" clears it.
    # Raises ChatApiError on API failure.
    async def set_status(self, *, channel: str, thread_ts: str, text: str) -> None: ...

    # Thread messages in chronological order, starting at the root — or, when
    # `oldest` is given, at the first message after it (exclusive).
    async def fetch_thread(
        self, *, channel: str, root: str, limit: int | None = None, oldest: str | None = None
    ) -> list[ChatMessage]: ...

    # Top-level channel messages within (oldest, latest), both bounds exclusive.
    # When has_more, only `limit` messages of the range were returned; without an
    # `oldest` bound they are guaranteed to be the range's newest.
    # Raises ChatApiError on API failure.
    async def fetch_history(
        self,
        *,
        channel: str,
        limit: int | None = None,
        oldest: str | None = None,
        latest: str | None = None,
    ) -> ChatHistoryPage: ...

    # Raises ChatApiError on API failure.
    async def user_info(self, *, user_id: str) -> ChatUser: ...

    # None when the platform did not answer.
    async def channel_info(self, channel: str) -> ChannelInfo | None: ...
