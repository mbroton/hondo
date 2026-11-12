from typing import Any

from hondo.chat import ChatFile
from hondo.models import EventKind
from hondo.runtime.gateway import Gateway, normalize_event
from tests.fakes import FakeStore


def mention_body(event_id: str = "Ev001", **event_overrides: Any) -> dict[str, Any]:
    event = {
        "type": "app_mention",
        "channel": "C01",
        "user": "U01",
        "ts": "1700000000.000100",
        "text": "<@B01> hello",
        **event_overrides,
    }
    return {"event_id": event_id, "team_id": "T01", "event": event}


def dm_body(event_id: str = "Ev002", **event_overrides: Any) -> dict[str, Any]:
    event = {
        "type": "message",
        "channel_type": "im",
        "channel": "D01",
        "user": "U01",
        "ts": "1700000000.000200",
        "text": "hi there",
        **event_overrides,
    }
    return {"event_id": event_id, "team_id": "T01", "event": event}


def thread_reply_body(
    event_id: str = "Ev003", *, authorized_bot: str | None = "B01", **event_overrides: Any
) -> dict[str, Any]:
    event = {
        "type": "message",
        "channel_type": "channel",
        "channel": "C01",
        "user": "U02",
        "ts": "1700000000.000300",
        "thread_ts": "1700000000.000100",
        "text": "what about staging?",
        **event_overrides,
    }
    body: dict[str, Any] = {"event_id": event_id, "team_id": "T01", "event": event}
    if authorized_bot is not None:
        body["authorizations"] = [{"user_id": authorized_bot}]
    return body


def test_normalize_app_mention() -> None:
    event = normalize_event(mention_body(thread_ts="1699999999.000001"))
    assert event is not None
    assert event.kind is EventKind.APP_MENTION
    assert event.mentions_bot and not event.is_dm
    assert event.thread_ts == "1699999999.000001"
    assert event.payload["type"] == "app_mention"


def test_normalize_dm() -> None:
    event = normalize_event(dm_body())
    assert event is not None
    assert event.kind is EventKind.MESSAGE
    assert event.is_dm and not event.mentions_bot


def test_normalize_thread_reply() -> None:
    event = normalize_event(thread_reply_body())
    assert event is not None
    assert event.kind is EventKind.MESSAGE
    assert not event.is_dm and not event.mentions_bot
    assert event.thread_ts == "1700000000.000100"


def test_thread_reply_mention_twin_is_dropped() -> None:
    # Slack fires both app_mention and message (distinct event_ids) for a mentioned
    # thread reply; the message twin must not become a second event.
    body = thread_reply_body(text="<@B01> what about staging?")
    assert normalize_event(body) is None


def test_thread_reply_mentioning_someone_else_is_kept() -> None:
    body = thread_reply_body(text="<@U99> what about staging?")
    assert normalize_event(body) is not None


def test_thread_reply_kept_when_bot_id_unresolvable() -> None:
    body = thread_reply_body(authorized_bot=None, text="<@B01> what about staging?")
    assert normalize_event(body) is not None


def top_level_body(**event_overrides: Any) -> dict[str, Any]:
    body = thread_reply_body(**event_overrides)
    del body["event"]["thread_ts"]
    return body


def test_top_level_mention_twin_is_dropped() -> None:
    assert normalize_event(top_level_body(text="<@B01> what about staging?")) is None


async def test_gateway_keeps_top_level_messages_only_in_listening_channels() -> None:
    store = FakeStore()
    store.listening_goals[("T01", "C02")] = "Answer questions about deploys."
    gateway = Gateway(store)

    assert await gateway.handle(top_level_body()) is None
    assert await store.get_event("Ev003") is None

    event = await gateway.handle(top_level_body(channel="C02"))
    assert event is not None and event.thread_ts is None and not event.mentions_bot
    assert await store.get_event("Ev003") is not None


def test_normalize_drops_bot_messages() -> None:
    assert normalize_event(dm_body(bot_id="B01")) is None
    assert normalize_event(thread_reply_body(bot_id="B01")) is None


def test_normalize_drops_subtypes() -> None:
    assert normalize_event(dm_body(subtype="message_changed")) is None
    assert normalize_event(thread_reply_body(subtype="message_changed")) is None


def test_replies_with_a_file_or_sent_to_the_channel_are_kept() -> None:
    for subtype in ("file_share", "thread_broadcast"):
        event = normalize_event(thread_reply_body(subtype=subtype))
        assert event is not None
        assert event.thread_ts == "1700000000.000100" and event.user == "U02"
    assert normalize_event(dm_body(subtype="file_share")) is not None
    assert normalize_event(dm_body(subtype="file_share", text="")) is None
    assert normalize_event(thread_reply_body(subtype="file_share", text=" ")) is None
    assert normalize_event(thread_reply_body(subtype="file_share", bot_id="B02")) is None


def test_a_message_with_only_a_file_is_kept_with_its_files() -> None:
    image = {
        "id": "F01",
        "name": "alert.png",
        "mimetype": "image/png",
        "size": 14957,
        "url_private": "https://files.slack.com/files-pri/T01-F01/alert.png",
    }
    event = normalize_event(dm_body(subtype="file_share", text="", files=[image]))
    assert event is not None
    assert event.files == (
        ChatFile("alert.png", "https://files.slack.com/files-pri/T01-F01/alert.png", "image/png"),
    )


def test_normalize_drops_malformed_payloads() -> None:
    assert normalize_event({}) is None
    body = mention_body()
    del body["event_id"]
    assert normalize_event(body) is None


async def test_gateway_appends_and_returns_the_event() -> None:
    store = FakeStore()
    gateway = Gateway(store)

    event = await gateway.handle(mention_body())
    assert event is not None and event.event_id == "Ev001"
    assert await store.get_event("Ev001") is not None


async def test_gateway_returns_redeliveries_for_dispatch_without_duplicating() -> None:
    store = FakeStore()
    gateway = Gateway(store)

    first = await gateway.handle(mention_body())
    second = await gateway.handle(mention_body())  # Slack delivers at-least-once
    assert first is not None and second is not None  # both dispatch; downstream is idempotent
    assert len(store.events) == 1


async def test_gateway_ignores_irrelevant_events() -> None:
    store = FakeStore()
    gateway = Gateway(store)

    assert await gateway.handle(dm_body(bot_id="B01")) is None
    assert await store.get_event("Ev002") is None
