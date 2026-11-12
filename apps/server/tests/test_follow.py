"""The thread switch: set_mention_only changes which replies reach a run."""

from collections.abc import Iterator
from contextlib import contextmanager

from hondo.agent.follow import ThreadFollowCapability
from hondo.config import EngagePolicy
from hondo.context import RunScope, reset_scope, set_scope
from hondo.models import Event, SessionKey
from hondo.runtime.dispatcher import Dispatcher
from tests.fakes import FakeBackend, FakeStore, make_event

ROOT = "1700000000.000100"


@contextmanager
def in_run(event: Event) -> Iterator[None]:
    token = set_scope(RunScope(key=SessionKey.from_event(event), event=event))
    try:
        yield
    finally:
        reset_scope(token)


def untagged_reply(event_id: str) -> Event:
    return make_event(
        event_id=event_id,
        kind="message",
        ts=f"1700000000.{event_id[-3:]}",
        text="the logs show a timeout",
        thread_ts=ROOT,
        mentions_bot=False,
    )


async def test_mention_only_stops_and_restarts_following_the_thread() -> None:
    store, backend = FakeStore(), FakeBackend()
    dispatcher = Dispatcher(store, EngagePolicy(), backend)
    follow = ThreadFollowCapability(store)
    trigger = make_event(ts=ROOT)
    await dispatcher.dispatch(trigger)

    with in_run(trigger):
        await follow._set_mention_only(True)
    await dispatcher.dispatch(untagged_reply("Ev101"))

    with in_run(trigger):
        await follow._set_mention_only(False)
    await dispatcher.dispatch(untagged_reply("Ev102"))

    assert [event.event_id for _, event, _ in backend.enqueued] == ["Ev001", "Ev102"]


async def test_admin_channel_setting_cannot_be_undone_from_a_thread() -> None:
    store, backend = FakeStore(), FakeBackend()
    dispatcher = Dispatcher(store, EngagePolicy(), backend)
    follow = ThreadFollowCapability(store)
    trigger = make_event(ts=ROOT)
    await dispatcher.dispatch(trigger)
    store.mention_only_channels.add(("T01", "C01"))

    with in_run(trigger):
        answer = await follow._set_mention_only(False)
    await dispatcher.dispatch(untagged_reply("Ev101"))

    assert answer.startswith("Not changed")
    assert [event.event_id for _, event, _ in backend.enqueued] == ["Ev001"]


async def test_direct_messages_have_no_switch() -> None:
    store = FakeStore()
    trigger = make_event(channel="D01", is_dm=True, kind="message", mentions_bot=False)

    with in_run(trigger):
        answer = await ThreadFollowCapability(store)._set_mention_only(True)

    assert answer.startswith("Not changed")
    assert store.sessions == {}
