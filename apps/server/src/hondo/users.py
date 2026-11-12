"""Slack user identity for the model: id → display name, cached in-process.

The model reads and writes conversations, so identity flows both ways:
incoming text has raw `<@U…>` mentions expanded to `@Name` (natural to read,
especially for cheaper models), while the session roster keeps the ids the
model needs to *write* a mention back. Everything here is best-effort — a
failed lookup renders the raw mention instead of failing the run.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable

import structlog

from hondo.chat import ChatClient, ChatUser

logger = structlog.get_logger("hondo.users")

# Slack user/bot-user mentions in message text: `<@U…>` / `<@W…>`, with an
# optional legacy `|handle` suffix.
MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")

# Names change rarely; failures shouldn't hammer the API on every message.
_SUCCESS_TTL_SECONDS = 3600.0
_FAILURE_TTL_SECONDS = 60.0


class UserDirectory:
    """Resolve chat user ids with an in-process TTL cache."""

    def __init__(self, client: ChatClient) -> None:
        self._client = client
        self._cache: dict[str, tuple[float, ChatUser | None]] = {}

    async def info(self, user_id: str) -> ChatUser | None:
        now = time.monotonic()
        cached = self._cache.get(user_id)
        if cached is not None and cached[0] > now:
            return cached[1]
        try:
            info = await self._client.user_info(user_id=user_id)
        except Exception:
            logger.warning("user lookup failed", user_id=user_id, exc_info=True)
            self._cache[user_id] = (now + _FAILURE_TTL_SECONDS, None)
            return None
        self._cache[user_id] = (now + _SUCCESS_TTL_SECONDS, info)
        return info

    async def name(self, user_id: str) -> str | None:
        info = await self.info(user_id)
        return info.name if info is not None else None

    async def resolve_many(self, user_ids: Iterable[str]) -> dict[str, str]:
        """Resolve a batch of ids; failures are simply absent from the result."""
        names: dict[str, str] = {}
        for user_id in user_ids:
            resolved = await self.name(user_id)
            if resolved is not None:
                names[user_id] = resolved
        return names


def mention_ids(texts: Iterable[str]) -> set[str]:
    """All user ids mentioned as `<@U…>` across the given texts."""
    ids: set[str] = set()
    for text in texts:
        ids.update(MENTION_RE.findall(text))
    return ids


def expand_mentions(text: str, names: dict[str, str]) -> str:
    """Rewrite `<@U…>` mentions as `@Name` using a resolved name map.

    Pure — resolution happens elsewhere (in a durable step). Unresolved
    mentions stay in raw form: still meaningful to the model, and the
    lookup_user tool gives it a way to chase them.
    """
    return MENTION_RE.sub(
        lambda match: f"@{names[match.group(1)]}" if match.group(1) in names else match.group(0),
        text,
    )
