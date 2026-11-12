import pytest
from pydantic import ValidationError

from hondo.models import Event, EventKind, SessionKey


def make_event(**overrides: object) -> Event:
    defaults: dict[str, object] = {
        "event_id": "Ev001",
        "kind": EventKind.MESSAGE,
        "team": "T01",
        "channel": "C01",
        "ts": "1700000000.000100",
        "user": "U01",
        "text": "hello",
    }
    return Event(**{**defaults, **overrides})  # type: ignore[arg-type]


def test_session_key_roots_at_top_level_message() -> None:
    event = make_event(kind=EventKind.APP_MENTION)
    key = SessionKey.from_event(event)
    assert key == SessionKey(team="T01", channel="C01", thread_root="1700000000.000100")


def test_session_key_roots_at_thread_for_replies() -> None:
    event = make_event(ts="1700000000.000200", thread_ts="1700000000.000100")
    key = SessionKey.from_event(event)
    assert key.thread_root == "1700000000.000100"


def test_dm_sessions_are_keyed_by_channel() -> None:
    event = make_event(channel="D01", is_dm=True, thread_ts="1700000000.000100")
    key = SessionKey.from_event(event)
    assert key.thread_root == "D01"


def test_replies_in_the_same_thread_share_a_key() -> None:
    root = make_event(kind=EventKind.APP_MENTION)
    reply = make_event(event_id="Ev002", ts="1700000000.000300", thread_ts="1700000000.000100")
    assert SessionKey.from_event(root) == SessionKey.from_event(reply)


def test_session_key_is_hashable_and_frozen() -> None:
    key = SessionKey(team="T01", channel="C01", thread_root="1.0")
    # pydantic generates __hash__ for frozen models at runtime; pyright can't see it
    assert {key: "value"}[key] == "value"  # pyright: ignore[reportUnhashable]
    with pytest.raises(ValidationError):
        key.team = "T02"  # type: ignore[misc]


def test_slack_event_is_frozen() -> None:
    event = make_event()
    with pytest.raises(ValidationError):
        event.text = "edited"  # type: ignore[misc]
