"""Dispatcher unit tests: the enqueue-vs-steer decision over fake store/backend."""

import asyncio
from typing import Any

import pytest

from hondo.config import EngagePolicy
from hondo.models import (
    Event,
    PendingFailure,
    Session,
    SessionKey,
    SessionState,
    SteerPriority,
    ThreadFollow,
)
from hondo.runtime.dispatcher import Dispatcher
from hondo.runtime.durable import run_workflow_id
from tests.fakes import FakeBackend, FakeSlack, FakeStore, make_event


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def dispatcher(store: FakeStore, backend: FakeBackend) -> Dispatcher:
    return Dispatcher(store, EngagePolicy(), backend)


ROOT = "1700000000.000100"


def thread_reply(event_id: str, text: str) -> Event:
    return make_event(
        event_id=event_id,
        kind="message",
        ts="1700000000.000200",
        text=text,
        thread_ts=ROOT,
        mentions_bot=False,
    )


async def test_engage_when_idle_enqueues_run(dispatcher: Dispatcher, backend: FakeBackend) -> None:
    event = make_event()
    await dispatcher.dispatch(event)

    assert len(backend.enqueued) == 1
    _, enqueued_event, workflow_id = backend.enqueued[0]
    assert enqueued_event.event_id == event.event_id
    assert workflow_id == run_workflow_id(SessionKey.from_event(event), event.event_id)


async def test_engage_marks_the_session_durably_before_the_run(
    dispatcher: Dispatcher, store: FakeStore, backend: FakeBackend
) -> None:
    """Accepting a cold mention creates an idle session before its run executes."""
    event = make_event()
    key = SessionKey.from_event(event)

    await dispatcher.dispatch(event)

    session = store.sessions[key]
    assert session.state is SessionState.IDLE
    assert session.context == {}
    assert store.saved_sessions == []
    assert len(backend.enqueued) == 1


class RacingBackend(FakeBackend):
    """Liveness reads return after a yield, like a database round-trip, and an
    enqueued run turns live like a real one."""

    async def live_workflow_id(self, key: SessionKey) -> str | None:
        live = self.live.get(key)
        await asyncio.sleep(0)
        return live

    async def enqueue_run(self, key: SessionKey, event: Event) -> str:
        workflow_id = await super().enqueue_run(key, event)
        self.live[key] = workflow_id
        return workflow_id


async def test_concurrent_burst_in_one_thread_starts_one_run(store: FakeStore) -> None:
    """Slack runs event handlers concurrently: the burst must still start one run."""
    backend = RacingBackend()
    dispatcher = Dispatcher(store, EngagePolicy(), backend)
    burst = [
        thread_reply("Ev-1", "check the deploy"),
        thread_reply("Ev-2", "hello?"),
        make_event(event_id="Ev-3", ts="1700000000.000300", thread_ts=ROOT, text="<@UBOT>"),
    ]
    await store.ensure_session(SessionKey.from_event(burst[0]))  # Hondo is in the thread

    await asyncio.gather(*(dispatcher.dispatch(event) for event in burst))

    assert [event.event_id for _, event, _ in backend.enqueued] == ["Ev-1"]
    run = backend.enqueued[0][2]
    for event_id in ("Ev-2", "Ev-3"):
        mail = await store.mail_for_event(event_id)
        assert mail is not None and mail.target_workflow_id == run


async def test_duplicate_redelivery_enqueues_once(
    dispatcher: Dispatcher, backend: FakeBackend
) -> None:
    event = make_event()
    await dispatcher.dispatch(event)
    await dispatcher.dispatch(event)

    assert len(backend.enqueued) == 1


async def test_engage_when_live_steers_instead(
    dispatcher: Dispatcher, store: FakeStore, backend: FakeBackend
) -> None:
    event = make_event()
    backend.live[SessionKey.from_event(event)] = "wf-live"

    await dispatcher.dispatch(event)

    assert backend.enqueued == []
    stored = await store.mail_for_event(event.event_id)
    assert stored is not None and not stored.consumed
    assert stored.priority is SteerPriority.ASAP
    assert stored.target_workflow_id == "wf-live"


async def test_steer_already_consumed_is_a_noop(
    dispatcher: Dispatcher, store: FakeStore, backend: FakeBackend
) -> None:
    event = make_event()
    key = SessionKey.from_event(event)
    backend.live[key] = "wf-live"
    mail = await store.insert_mail(key, event, SteerPriority.ASAP, target_workflow_id="wf-old")
    assert mail is not None
    await store.consume_mail([mail.id])

    await dispatcher.dispatch(event)

    assert backend.enqueued == []
    stored = await store.mail_for_event(event.event_id)
    assert stored is not None and stored.consumed  # untouched


async def test_insert_racing_run_end_redispatches_fresh_run(
    dispatcher: Dispatcher, store: FakeStore, backend: FakeBackend
) -> None:
    event = make_event()
    key = SessionKey.from_event(event)
    await store.append_event(event)  # the gateway always appends before dispatch
    backend.live[key] = "wf-live"
    backend.finished.add("wf-live")  # completed between the liveness check and the send

    await dispatcher.dispatch(event)

    assert len(backend.enqueued) == 1  # a fresh run will drain the mail from the store
    assert backend.enqueued[0][2] == run_workflow_id(key, event.event_id)


async def test_unaddressed_thread_reply_steers_live_run_when_idle_priority(
    dispatcher: Dispatcher, store: FakeStore, backend: FakeBackend
) -> None:
    key = SessionKey.from_event(make_event(ts=ROOT))
    await store.save_session(Session(key=key))
    backend.live[key] = "wf-live"

    await dispatcher.dispatch(thread_reply("Ev2", "also check staging"))

    stored = await store.mail_for_event("Ev2")
    assert stored is not None and stored.priority is SteerPriority.WHEN_IDLE
    assert backend.enqueued == []


async def test_unaddressed_thread_reply_with_no_live_run_wakes_the_session(
    dispatcher: Dispatcher, store: FakeStore, backend: FakeBackend
) -> None:
    # Engaged-thread following (default policy): a session exists, no live
    # run — the reply starts a fresh run instead of being dropped.
    key = SessionKey.from_event(make_event(ts=ROOT))
    await store.save_session(Session(key=key))

    await dispatcher.dispatch(thread_reply("Ev2", "anyone?"))

    assert await store.mail_for_event("Ev2") is None  # a run, not a steer
    assert len(backend.enqueued) == 1
    assert backend.enqueued[0][1].event_id == "Ev2"


async def test_unaddressed_thread_reply_in_cold_thread_is_dropped(
    dispatcher: Dispatcher, store: FakeStore, backend: FakeBackend
) -> None:
    await dispatcher.dispatch(thread_reply("Ev2", "anyone?"))

    assert await store.mail_for_event("Ev2") is None
    assert backend.enqueued == []


async def test_unaddressed_reply_after_failed_first_run_is_not_dropped(
    store: FakeStore, backend: FakeBackend
) -> None:
    """Durable engagement lets an ambient reply wake a failed first-run thread."""
    dispatcher = Dispatcher(store, EngagePolicy(), backend)
    mention = make_event(ts=ROOT)

    await dispatcher.dispatch(mention)
    await dispatcher.dispatch(thread_reply("Ev2", "continue after the failure"))

    assert store.saved_sessions == []  # neither fake enqueue executed a run
    assert [event.event_id for _, event, _ in backend.enqueued] == [mention.event_id, "Ev2"]


async def _mention_only_thread(store: FakeStore) -> SessionKey:
    key = SessionKey.from_event(make_event(ts=ROOT))
    await store.save_session(Session(key=key))
    await store.merge_context_key(
        key, ThreadFollow.CONTEXT_KEY, ThreadFollow.patch(mention_only=True)
    )
    return key


async def _mention_only_channel(store: FakeStore) -> SessionKey:
    key = SessionKey.from_event(make_event(ts=ROOT))
    await store.save_session(Session(key=key))
    store.mention_only_channels.add(("T01", "C01"))
    return key


@pytest.mark.parametrize("setup", [_mention_only_thread, _mention_only_channel])
@pytest.mark.parametrize("live", [False, True])
async def test_unaddressed_reply_in_mention_only_thread_does_nothing(
    dispatcher: Dispatcher, store: FakeStore, backend: FakeBackend, setup: Any, live: bool
) -> None:
    key = await setup(store)
    if live:
        backend.live[key] = "wf-live"

    await dispatcher.dispatch(thread_reply("Ev2", "also check staging"))

    assert await store.mail_for_event("Ev2") is None
    assert backend.enqueued == []


@pytest.mark.parametrize("setup", [_mention_only_thread, _mention_only_channel])
async def test_mention_in_mention_only_thread_still_reaches_the_live_run(
    dispatcher: Dispatcher, store: FakeStore, backend: FakeBackend, setup: Any
) -> None:
    key = await setup(store)
    backend.live[key] = "wf-live"

    await dispatcher.dispatch(make_event(event_id="Ev2", ts="1700000000.000200", thread_ts=ROOT))

    stored = await store.mail_for_event("Ev2")
    assert stored is not None and stored.priority is SteerPriority.ASAP


async def test_mention_only_channel_leaves_other_channels_alone(
    dispatcher: Dispatcher, store: FakeStore, backend: FakeBackend
) -> None:
    key = SessionKey.from_event(make_event(ts=ROOT))
    await store.save_session(Session(key=key))
    store.mention_only_channels.add(("T01", "C99"))

    await dispatcher.dispatch(thread_reply("Ev2", "anyone?"))

    assert [event.event_id for _, event, _ in backend.enqueued] == ["Ev2"]


def top_level(event_id: str, text: str) -> Event:
    return make_event(event_id=event_id, kind="message", text=text, mentions_bot=False)


@pytest.mark.parametrize(
    "event", [top_level("Ev2", "how do I deploy?"), thread_reply("Ev2", "how do I deploy?")]
)
async def test_listening_channel_starts_a_run_without_a_session(
    dispatcher: Dispatcher, store: FakeStore, backend: FakeBackend, event: Event
) -> None:
    store.listening_goals[("T01", "C01")] = "Answer questions about deploys."

    await dispatcher.dispatch(event)

    assert [enqueued.event_id for _, enqueued, _ in backend.enqueued] == ["Ev2"]
    assert store.sessions == {}


async def test_reply_during_the_screen_waits_for_that_run_without_a_session(
    dispatcher: Dispatcher, store: FakeStore, backend: FakeBackend
) -> None:
    store.listening_goals[("T01", "C01")] = "Answer questions about deploys."
    key = SessionKey.from_event(make_event(ts=ROOT))
    backend.live[key] = "wf-screen"

    await dispatcher.dispatch(thread_reply("Ev2", "me too"))

    stored = await store.mail_for_event("Ev2")
    assert stored is not None
    assert stored.priority is SteerPriority.WHEN_IDLE
    assert stored.target_workflow_id == "wf-screen"
    assert backend.enqueued == []
    assert store.sessions == {}


async def test_unaddressed_top_level_message_elsewhere_is_dropped(
    dispatcher: Dispatcher, store: FakeStore, backend: FakeBackend
) -> None:
    store.listening_goals[("T01", "C99")] = "Answer questions about deploys."

    await dispatcher.dispatch(top_level("Ev2", "how do I deploy?"))

    assert backend.enqueued == []


async def test_mention_only_channel_does_not_listen(
    dispatcher: Dispatcher, store: FakeStore, backend: FakeBackend
) -> None:
    store.listening_goals[("T01", "C01")] = "Answer questions about deploys."
    store.mention_only_channels.add(("T01", "C01"))

    await dispatcher.dispatch(top_level("Ev2", "how do I deploy?"))

    assert backend.enqueued == []


async def test_mention_in_listening_channel_still_marks_the_session(
    dispatcher: Dispatcher, store: FakeStore, backend: FakeBackend
) -> None:
    store.listening_goals[("T01", "C01")] = "Answer questions about deploys."
    event = make_event()

    await dispatcher.dispatch(event)

    assert SessionKey.from_event(event) in store.sessions
    assert len(backend.enqueued) == 1


async def test_reconcile_redispatches_mail_whose_target_finished(
    dispatcher: Dispatcher, store: FakeStore, backend: FakeBackend
) -> None:
    event = make_event()
    key = SessionKey.from_event(event)
    await store.append_event(event)
    mail = await store.insert_mail(key, event, SteerPriority.ASAP, target_workflow_id="wf-dead")
    assert mail is not None
    backend.finished.add("wf-dead")

    await dispatcher.reconcile(0)

    assert len(backend.enqueued) == 1
    assert backend.enqueued[0][2] == run_workflow_id(key, event.event_id)


async def test_reconcile_leaves_mail_whose_target_is_live(
    dispatcher: Dispatcher, store: FakeStore, backend: FakeBackend
) -> None:
    event = make_event()
    key = SessionKey.from_event(event)
    await store.append_event(event)
    await store.insert_mail(key, event, SteerPriority.ASAP, target_workflow_id="wf-live")

    await dispatcher.reconcile(0)

    assert backend.enqueued == []


async def test_reconcile_skips_mail_with_no_logged_event(
    dispatcher: Dispatcher, store: FakeStore, backend: FakeBackend
) -> None:
    event = make_event()
    key = SessionKey.from_event(event)
    await store.insert_mail(key, event, SteerPriority.ASAP, target_workflow_id=None)

    await dispatcher.reconcile(0)

    assert backend.enqueued == []


async def test_failed_run_sweep_posts_notice_once_and_logs(
    store: FakeStore, backend: FakeBackend
) -> None:
    slack = FakeSlack()
    dispatcher = Dispatcher(store, EngagePolicy(), backend, client=slack)
    event = make_event()
    key = SessionKey.from_event(event)
    await store.append_event(event)
    backend.failed.append(run_workflow_id(key, event.event_id))

    await dispatcher.sweep_failed_runs()
    await dispatcher.deliver_run_notices()
    await dispatcher.sweep_failed_runs()  # recorded once
    await dispatcher.deliver_run_notices()  # delivered once

    assert len(slack.posted) == 1
    notice = slack.posted[0]
    assert "hit an error" in notice["text"]
    assert notice["thread_ts"] == event.ts  # lands in the trigger's thread
    assert notice["blocks"] and notice["blocks"][0]["type"] == "context"
    assert slack.reactions == []


async def test_failed_run_sweep_persists_pending_record(
    dispatcher: Dispatcher, store: FakeStore, backend: FakeBackend
) -> None:
    """The hard-crash sweep records model-visible recovery context with its notice."""
    event = make_event()
    key = SessionKey.from_event(event)
    workflow_id = run_workflow_id(key, event.event_id)
    await store.append_event(event)
    backend.failed.append(workflow_id)

    await dispatcher.sweep_failed_runs()

    pending = PendingFailure.list_from_context(store.sessions[key].context)
    assert len(pending) == 1
    assert pending[0].workflow_id == workflow_id
    assert pending[0].event_id == event.event_id
    assert pending[0].reason == "crashed"
    assert "hit an error" in store.run_notices[workflow_id]["text"]


async def test_failed_run_sweep_posts_notice_for_ambient_triggers_too(
    store: FakeStore, backend: FakeBackend
) -> None:
    # An untagged reply in an engaged thread is often *for* the bot (it may be
    # answering the bot's own question) — a crashed run must be visible, and
    # the dead run also dropped its triggering message from history.
    slack = FakeSlack()
    dispatcher = Dispatcher(store, EngagePolicy(), backend, client=slack)
    event = thread_reply("Ev2", "anyone?")
    key = SessionKey.from_event(event)
    await store.save_session(Session(key=key))
    await store.append_event(event)
    backend.failed.append(run_workflow_id(key, event.event_id))

    await dispatcher.sweep_failed_runs()
    await dispatcher.deliver_run_notices()

    assert len(slack.posted) == 1 and "hit an error" in slack.posted[0]["text"]


@pytest.mark.parametrize("sweep", ["failed", "stale"])
async def test_dead_run_of_a_listened_message_leaves_no_trace(
    store: FakeStore, backend: FakeBackend, sweep: str
) -> None:
    slack = FakeSlack()
    dispatcher = Dispatcher(store, EngagePolicy(), backend, client=slack)
    event = make_event(event_id="Ev2", kind="message", mentions_bot=False, text="tacos?")
    key = SessionKey.from_event(event)
    await store.append_event(event)
    workflow_id = run_workflow_id(key, event.event_id)
    if sweep == "failed":
        backend.failed.append(workflow_id)
        await dispatcher.sweep_failed_runs()
    else:
        backend.stale.append(workflow_id)
        await dispatcher.sweep_stale_runs()
    await dispatcher.deliver_run_notices()

    assert slack.posted == []
    assert store.sessions == {}
    assert store.run_notices == {}
    if sweep == "stale":
        assert backend.cancelled == [workflow_id]


async def test_failed_run_sweep_survives_missing_event(backend: FakeBackend) -> None:
    dispatcher = Dispatcher(FakeStore(), EngagePolicy(), backend, client=FakeSlack())
    backend.failed.append("T01:C01:x#Ev-gone")
    await dispatcher.sweep_failed_runs()  # logs, no crash
    await dispatcher.deliver_run_notices()  # nowhere to post; marked delivered


async def test_failed_run_sweep_does_not_reannounce_after_restart(
    store: FakeStore, backend: FakeBackend
) -> None:
    # The notice outbox is durable (store), not per-process: a restart must
    # not re-post notices for every failed run in history.
    slack = FakeSlack()
    event = make_event()
    await store.append_event(event)
    backend.failed.append(run_workflow_id(SessionKey.from_event(event), event.event_id))

    first = Dispatcher(store, EngagePolicy(), backend, client=slack)
    await first.sweep_failed_runs()
    await first.deliver_run_notices()
    # Process restart: fresh dispatcher, same store and backend state.
    second = Dispatcher(store, EngagePolicy(), backend, client=slack)
    await second.sweep_failed_runs()
    await second.deliver_run_notices()

    assert len(slack.posted) == 1


async def test_notice_delivery_retries_until_slack_recovers(
    store: FakeStore, backend: FakeBackend
) -> None:
    # A crash or Slack outage between recording and posting must not lose the
    # thread's only signal that its run died: the notice stays pending and is
    # retried (bounded attempts), then delivered exactly once.
    class FlakySlack(FakeSlack):
        def __init__(self) -> None:
            super().__init__()
            self.fail = True

        async def post_notice(self, **kwargs: object) -> str:
            if self.fail:
                raise RuntimeError("slack down")
            return await super().post_notice(**kwargs)  # type: ignore[arg-type]

    slack = FlakySlack()
    dispatcher = Dispatcher(store, EngagePolicy(), backend, client=slack)
    event = make_event()
    await store.append_event(event)
    backend.failed.append(run_workflow_id(SessionKey.from_event(event), event.event_id))

    await dispatcher.sweep_failed_runs()
    await dispatcher.deliver_run_notices()  # post fails; stays pending
    assert slack.posted == []

    slack.fail = False
    await dispatcher.deliver_run_notices()
    await dispatcher.deliver_run_notices()  # delivered once, not again
    assert len(slack.posted) == 1 and "hit an error" in slack.posted[0]["text"]


async def test_stale_run_sweep_cancels_notifies_and_unwedges_the_thread(
    store: FakeStore, backend: FakeBackend
) -> None:
    # A deploy left a run on another application version: never dequeued or
    # recovered, yet holding the thread. The sweep cancels it (terminal), the
    # thread hears why, and the ordinary reconcile pass can then redispatch
    # the steer that was targeting it.
    slack = FakeSlack()
    dispatcher = Dispatcher(store, EngagePolicy(), backend, client=slack)
    event = make_event()
    key = SessionKey.from_event(event)
    await store.append_event(event)
    workflow_id = run_workflow_id(key, event.event_id)
    backend.stale.append(workflow_id)
    steer = thread_reply("Ev2", "any update?")
    await store.append_event(steer)
    await store.insert_mail(key, steer, SteerPriority.ASAP, target_workflow_id=workflow_id)

    await dispatcher.sweep_stale_runs()
    await dispatcher.deliver_run_notices()

    assert backend.cancelled == [workflow_id]
    assert len(slack.posted) == 1 and "updated mid-task" in slack.posted[0]["text"]

    await dispatcher.reconcile(0)  # cancelled is terminal: the steer moves on
    assert len(backend.enqueued) == 1
    assert backend.enqueued[0][1].event_id == "Ev2"

    await dispatcher.sweep_stale_runs()  # recorded and cancelled once
    await dispatcher.deliver_run_notices()
    assert backend.cancelled == [workflow_id]
    assert len(slack.posted) == 1


async def test_stale_run_sweep_persists_pending_record(
    dispatcher: Dispatcher, store: FakeStore, backend: FakeBackend
) -> None:
    """The deployment sweep records interrupted-run context before cancellation."""
    event = make_event()
    key = SessionKey.from_event(event)
    workflow_id = run_workflow_id(key, event.event_id)
    await store.append_event(event)
    backend.stale.append(workflow_id)

    await dispatcher.sweep_stale_runs()

    pending = PendingFailure.list_from_context(store.sessions[key].context)
    assert len(pending) == 1
    assert pending[0].workflow_id == workflow_id
    assert pending[0].event_id == event.event_id
    assert pending[0].reason == "interrupted by a deployment"
    assert "updated mid-task" in store.run_notices[workflow_id]["text"]
