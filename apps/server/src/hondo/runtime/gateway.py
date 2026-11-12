from __future__ import annotations

from typing import Any

import structlog

from hondo.models import Event, EventKind
from hondo.slack import message_files
from hondo.store.base import Store

logger = structlog.get_logger("hondo.gateway")

# Messages a person posted that carry a subtype: a reply with a file, and a
# reply also sent to the channel.
_PERSON_SUBTYPES = frozenset({"file_share", "thread_broadcast"})


def _authorized_bot_id(body: dict[str, Any]) -> str | None:
    authorizations = body.get("authorizations")
    if isinstance(authorizations, list) and authorizations:
        first = authorizations[0]
        if isinstance(first, dict):
            user_id = first.get("user_id")
            if isinstance(user_id, str) and user_id:
                return user_id
    return None


def normalize_event(body: dict[str, Any]) -> Event | None:
    """Normalize a Slack Events API payload into a Event, or None if irrelevant.

    Accepts app mentions, DM messages, and channel messages, also with a
    file or sent to the channel too. Bot messages and other message subtypes
    (edits, joins, ...) are dropped. Slack fires both `app_mention`
    and a channel `message` (with distinct event_ids) for a mentioned thread reply,
    so the `message` twin is dropped when the text mentions the authorized bot user;
    if the bot id cannot be resolved from `authorizations`, the reply is accepted
    (event-log dedupe cannot help here — the twins carry different event_ids).
    """
    event: dict[str, Any] = body.get("event") or {}
    if event.get("bot_id") or event.get("subtype") not in (None, *_PERSON_SUBTYPES):
        return None

    bot_id = _authorized_bot_id(body)
    event_type = event.get("type")
    if event_type == "app_mention":
        kind, is_dm, mentions_bot = EventKind.APP_MENTION, False, True
    elif event_type == "message" and event.get("channel_type") == "im":
        kind, is_dm, mentions_bot = EventKind.MESSAGE, True, False
    elif event_type == "message":
        if bot_id and f"<@{bot_id}>" in (event.get("text") or ""):
            return None  # mention twin: the app_mention event carries this message
        kind, is_dm, mentions_bot = EventKind.MESSAGE, False, False
    else:
        return None

    event_id = body.get("event_id")
    channel = event.get("channel")
    ts = event.get("ts")
    text = event.get("text") or ""
    files = message_files(event)
    if not (event_id and channel and ts and (text.strip() or files)):
        return None

    return Event(
        event_id=event_id,
        kind=kind,
        team=body.get("team_id") or "",
        channel=channel,
        ts=ts,
        user=event.get("user"),
        thread_ts=event.get("thread_ts"),
        text=text,
        is_dm=is_dm,
        mentions_bot=mentions_bot,
        bot_user=bot_id,
        files=files,
        payload=event,
    )


APPROVAL_ACTIONS = {"approval_approve": True, "approval_reject": False}


def normalize_action(body: dict[str, Any]) -> Event | None:
    """Normalize a click on an approval button into an INTERACTION event, or None.

    The payload carries the approval id that Hondo put on the button, whether
    the click approves, and the message that holds the button. The clicker is
    the user that Slack names.
    """
    actions = body.get("actions")
    team = (body.get("team") or {}).get("id")
    user = (body.get("user") or {}).get("id")
    channel = (body.get("channel") or {}).get("id")
    if body.get("type") != "block_actions" or not isinstance(actions, list) or len(actions) != 1:
        return None
    action = actions[0]
    if not isinstance(action, dict) or action.get("action_id") not in APPROVAL_ACTIONS:
        return None
    approval_id = action.get("value")
    ts = action.get("action_ts")
    message_ts = (body.get("container") or {}).get("message_ts")
    if not (
        isinstance(approval_id, str)
        and approval_id
        and team
        and user
        and channel
        and ts
        and isinstance(message_ts, str)
    ):
        return None
    return Event(
        event_id=f"action:{approval_id}:{user}:{ts}",
        kind=EventKind.INTERACTION,
        team=team,
        channel=channel,
        ts=ts,
        user=user,
        payload={
            "approval_id": approval_id,
            "approved": APPROVAL_ACTIONS[action["action_id"]],
            "message_ts": message_ts,
        },
    )


class Gateway:
    """Ingress plane: normalize and append to the durable event log.

    No LLM calls and no business logic. Returns the normalized event so the
    caller can dispatch it — including on duplicate deliveries (Slack is
    at-least-once): the first delivery may have crashed before dispatching, and
    everything downstream (enqueue by deterministic workflow id, send by
    idempotency key) absorbs replays.
    """

    def __init__(self, store: Store) -> None:
        self._store = store

    async def handle(self, body: dict[str, Any]) -> Event | None:
        event = normalize_event(body)
        if event is None:
            return None
        if (
            event.thread_ts is None
            and not (event.is_dm or event.mentions_bot)
            and await self._store.listening_goal(event.team, event.channel) is None
        ):
            return None
        logger.info(
            "message received",
            event_id=event.event_id,
            kind=event.kind.value,
            channel=event.channel,
            user=event.user,
            dm=event.is_dm,
        )
        if not await self._store.append_event(event):
            logger.debug("redispatching duplicate delivery", event_id=event.event_id)
        return event

    async def handle_action(self, body: dict[str, Any]) -> Event | None:
        """A button click. The approval row is its durable record, so the event log
        does not keep it."""
        event = normalize_action(body)
        if event is not None:
            logger.info(
                "approval click received",
                approval_id=event.payload["approval_id"],
                approved=event.payload["approved"],
                user=event.user,
            )
        return event
