"""PostgresStore steer-ledger tests (the DBOS rebuild's store surface)."""

from __future__ import annotations

from hondo.models import Event, EventKind, SessionKey, SteerPriority
from hondo.store.postgres import PostgresStore

KEY = SessionKey(team="T01", channel="C01", thread_root="1700000000.000100")


def event(event_id: str, *, ts: str = "1700000000.000200", text: str = "hello") -> Event:
    return Event(
        event_id=event_id,
        kind=EventKind.MESSAGE,
        team=KEY.team,
        channel=KEY.channel,
        ts=ts,
        user="U01",
        thread_ts=KEY.thread_root,
        text=text,
    )


async def test_insert_mail_roundtrip(store: PostgresStore) -> None:
    mail = await store.insert_mail(
        KEY, event("Ev-m1"), SteerPriority.ASAP, target_workflow_id="wf-1"
    )
    assert mail is not None
    assert mail.event_id == "Ev-m1"
    assert mail.target_workflow_id == "wf-1"
    assert not mail.consumed

    fetched = await store.mail_for_event("Ev-m1")
    assert fetched is not None and fetched.id == mail.id


async def test_insert_mail_dedupes_and_retargets(store: PostgresStore) -> None:
    first = await store.insert_mail(
        KEY, event("Ev-m2"), SteerPriority.ASAP, target_workflow_id="wf-old"
    )
    assert first is not None
    again = await store.insert_mail(
        KEY, event("Ev-m2"), SteerPriority.ASAP, target_workflow_id="wf-new"
    )
    assert again is not None
    assert again.id == first.id
    assert again.target_workflow_id == "wf-new"


async def test_insert_mail_returns_none_once_consumed(store: PostgresStore) -> None:
    mail = await store.insert_mail(KEY, event("Ev-m3"), SteerPriority.ASAP, target_workflow_id=None)
    assert mail is not None
    await store.consume_mail([mail.id])

    assert (
        await store.insert_mail(KEY, event("Ev-m3"), SteerPriority.ASAP, target_workflow_id="wf-x")
        is None
    )
    fetched = await store.mail_for_event("Ev-m3")
    assert fetched is not None and fetched.consumed


async def test_unconsumed_consume_roundtrip(store: PostgresStore) -> None:
    a = await store.insert_mail(KEY, event("Ev-m4"), SteerPriority.ASAP, target_workflow_id=None)
    b = await store.insert_mail(
        KEY, event("Ev-m5"), SteerPriority.WHEN_IDLE, target_workflow_id=None
    )
    assert a is not None and b is not None

    pending = await store.unconsumed_mail(KEY)
    assert [m.id for m in pending] == [a.id, b.id]
    assert pending[1].priority is SteerPriority.WHEN_IDLE

    await store.consume_mail([a.id])
    remaining = await store.unconsumed_mail(KEY)
    assert [m.id for m in remaining] == [b.id]


async def test_stale_unconsumed_mail_filters_by_age(store: PostgresStore) -> None:
    mail = await store.insert_mail(
        KEY, event("Ev-m6"), SteerPriority.ASAP, target_workflow_id="wf-1"
    )
    assert mail is not None

    fresh = await store.stale_unconsumed_mail(60)
    assert all(m.id != mail.id for m in fresh)

    stale = await store.stale_unconsumed_mail(0)
    assert any(m.id == mail.id for m in stale)

    await store.consume_mail([mail.id])
    assert all(m.id != mail.id for m in await store.stale_unconsumed_mail(0))


async def test_claim_mail_consumes_and_returns_oldest_first(store: PostgresStore) -> None:
    await store.insert_mail(KEY, event("Ev-t1"), SteerPriority.ASAP, target_workflow_id=None)
    await store.insert_mail(KEY, event("Ev-t2"), SteerPriority.WHEN_IDLE, target_workflow_id=None)

    claimed = await store.claim_mail(KEY, token="wf#Ev-t0:claim_mailbox")

    assert [m.event_id for m in claimed] == ["Ev-t1", "Ev-t2"]
    assert await store.unconsumed_mail(KEY) == []


async def test_claim_mail_replay_with_same_token_sees_the_same_mail(store: PostgresStore) -> None:
    # The crash window fix #4 closes: the claim's commit landed but the
    # caller's checkpoint didn't — re-executing the claim must return the
    # already-consumed rows, not an empty ledger.
    await store.insert_mail(KEY, event("Ev-t1"), SteerPriority.ASAP, target_workflow_id=None)
    first = await store.claim_mail(KEY, token="wf#Ev-t0:drain_mailbox:0")

    replay = await store.claim_mail(KEY, token="wf#Ev-t0:drain_mailbox:0")
    assert [m.id for m in replay] == [m.id for m in first]

    # A different step's claim sees nothing — the mail belongs to the first.
    assert await store.claim_mail(KEY, token="wf#Ev-t0:drain_mailbox:1") == []


async def test_claim_mail_replay_also_picks_up_newly_arrived_mail(store: PostgresStore) -> None:
    await store.insert_mail(KEY, event("Ev-t1"), SteerPriority.ASAP, target_workflow_id=None)
    await store.claim_mail(KEY, token="tok")
    await store.insert_mail(KEY, event("Ev-t2"), SteerPriority.ASAP, target_workflow_id=None)

    replay = await store.claim_mail(KEY, token="tok")

    assert [m.event_id for m in replay] == ["Ev-t1", "Ev-t2"]


async def test_claim_mail_without_token_consumes_exactly_once(store: PostgresStore) -> None:
    await store.insert_mail(KEY, event("Ev-t1"), SteerPriority.ASAP, target_workflow_id=None)

    assert [m.event_id for m in await store.claim_mail(KEY, token=None)] == ["Ev-t1"]
    assert await store.claim_mail(KEY, token=None) == []


async def test_run_notice_outbox_roundtrip(store: PostgresStore) -> None:
    await store.record_run_notice("wf-1", "boom")
    await store.record_run_notice("wf-1", "different text")  # idempotent; first wins

    pending = await store.take_pending_run_notices(max_attempts=5)
    assert [(n.workflow_id, n.text) for n in pending] == [("wf-1", "boom")]

    await store.mark_run_notice_posted("wf-1")
    assert await store.take_pending_run_notices(max_attempts=5) == []


async def test_run_notice_attempts_are_bounded(store: PostgresStore) -> None:
    # A permanently failing post (deleted channel) must not retry forever.
    await store.record_run_notice("wf-1", "boom")
    for _ in range(3):
        assert len(await store.take_pending_run_notices(max_attempts=3)) == 1
    assert await store.take_pending_run_notices(max_attempts=3) == []


async def test_connect_is_idempotent(store: PostgresStore) -> None:
    await store.connect()
    await store.connect()
    assert await store.get_event("Ev-never") is None


async def test_claim_mail_returns_slack_order_not_insert_order(store: PostgresStore) -> None:
    await store.insert_mail(
        KEY, event("Ev-late", ts="1700000000.000300"), SteerPriority.ASAP, target_workflow_id=None
    )
    await store.insert_mail(
        KEY, event("Ev-early", ts="1700000000.000200"), SteerPriority.ASAP, target_workflow_id=None
    )

    claimed = await store.claim_mail(KEY, token=None)

    assert [m.event_id for m in claimed] == ["Ev-early", "Ev-late"]


async def test_waiting_message_ages_cover_the_trigger_and_unconsumed_mail(
    store: PostgresStore,
) -> None:
    assert await store.waiting_message_ages(KEY, "Ev-trigger") is None

    await store.append_event(event("Ev-trigger"))
    await store._connected_pool.execute(
        "UPDATE events SET received_at = now() - interval '20 seconds' WHERE event_id = $1",
        "Ev-trigger",
    )
    ages = await store.waiting_message_ages(KEY, "Ev-trigger")
    assert ages is not None
    assert 19 < ages[0] < 30 and 19 < ages[1] < 30

    mail = await store.insert_mail(
        KEY, event("Ev-later"), SteerPriority.ASAP, target_workflow_id=None
    )
    assert mail is not None
    other = SessionKey(team=KEY.team, channel=KEY.channel, thread_root="1700000000.999999")
    await store.insert_mail(other, event("Ev-other"), SteerPriority.ASAP, target_workflow_id=None)
    ages = await store.waiting_message_ages(KEY, "Ev-trigger")
    assert ages is not None
    assert 19 < ages[0] < 30 and 0 <= ages[1] < 10

    await store.consume_mail([mail.id])
    ages = await store.waiting_message_ages(KEY, "Ev-trigger")
    assert ages is not None
    assert 19 < ages[1] < 30
