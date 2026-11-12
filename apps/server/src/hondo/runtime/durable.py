"""DBOS glue: id schemes, in-workflow guards, and the queue backend.

The engine's durability comes from DBOS (workflows + steps + partitioned queue
over Postgres). Everything DBOS-specific that the runner and dispatcher need is
concentrated here so the rest of the runtime stays testable without a DBOS
launch: `step()` degrades to a plain call outside a workflow, and the
dispatcher talks to a `RunBackend` protocol that tests fake.

Steering deliberately does NOT use the DBOS send/recv mailbox: the run only
ever polls at node boundaries, so a step that claims mail from the steer
ledger is just as durable (the claim is a recorded step output), one recorded
operation instead of two, and has no blocking-wait path.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, Protocol

from dbos import DBOS, Queue, SetEnqueueOptions, SetWorkflowID, StepOptions

from hondo.models import Event, SessionKey

# Statuses meaning "this workflow will make no further progress by itself".
TERMINAL_STATUSES = frozenset({"SUCCESS", "ERROR", "CANCELLED", "MAX_RECOVERY_ATTEMPTS_EXCEEDED"})
LIVE_STATUSES = ("ENQUEUED", "PENDING")

# The drive-session workflow signature: (key_json, event_json) -> final output.
DriveFn = Callable[[dict[str, Any], dict[str, Any]], Coroutine[Any, Any, str]]


def partition_key(key: SessionKey) -> str:
    """Queue partition per session: one live run per Slack thread, fleet-wide."""
    return f"{key.team}:{key.channel}:{key.thread_root}"


def run_workflow_id(key: SessionKey, event_id: str) -> str:
    """Deterministic workflow id: enqueue idempotency for event redeliveries."""
    return f"{partition_key(key)}#{event_id}"


def screen_usage_id(key: SessionKey, event_id: str) -> str:
    # The usage row of the screen, apart from the row of the run that follows it.
    return f"{partition_key(key)}/screen#{event_id}"


def summary_workflow_id(key: SessionKey, event_id: str) -> str:
    # Under the team prefix, so the dashboard keeps the provider key that a
    # pending summary needs. Not under the run prefix, so the dispatcher never
    # takes a summary for the thread's live run.
    return f"{partition_key(key)}/summary#{event_id}"


def in_workflow() -> bool:
    return DBOS.workflow_id is not None


async def step(name: str, fn: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
    """Run `fn` as a named DBOS step inside a workflow; as a plain call outside.

    Inside a workflow this makes the call durable: executed once, its result
    recorded, and returned from the record on crash-recovery replay. Outside
    (unit tests, bring-your-own-loop users) it is just an awaited call.
    """
    if in_workflow():
        return await DBOS.run_step_async(StepOptions(name=name), fn, *args, **kwargs)
    return await fn(*args, **kwargs)


async def sleep(seconds: float) -> None:
    """Durable inside a workflow: crash recovery sleeps only the remainder."""
    if in_workflow():
        await DBOS.sleep_async(seconds)
    else:
        await asyncio.sleep(seconds)


async def recv(topic: str, timeout: float) -> Any:
    """Wait for a message on the topic, or None after the timeout.

    Durable: a recovered workflow waits only the remainder and gets the same
    message. Only a workflow can wait, because only a workflow has an address.
    """
    if not in_workflow():
        raise RuntimeError("a durable wait needs a workflow")
    return await DBOS.recv_async(topic, timeout_seconds=timeout)


async def notify(workflow_id: str, topic: str) -> None:
    """Wake the workflow that waits on the topic. Repeated calls send one message."""
    await DBOS.send_async(workflow_id, True, topic, idempotency_key=f"{workflow_id}:{topic}")


def step_token() -> str | None:
    """Identity of the running step, for a store write inside it: the same
    when recovery runs the step again (see claim_token), and distinct across
    steps. None outside a step."""
    workflow_id, step_id = DBOS.workflow_id, DBOS.step_id
    if workflow_id is None or step_id is None:
        return None
    return f"{workflow_id}:{step_id}"


def claim_token(step_name: str) -> str | None:
    """Deterministic identity for a store write performed inside the named step.

    A step's own commit and DBOS's record of its output are two Postgres
    transactions; a kill between them replays the step against state the
    first commit already changed. Writes stamped with this token can be
    re-read idempotently on that replay: it is stable across crash recovery
    (workflow id + step name) and distinct across steps. None outside a
    workflow — there is no replay to survive.
    """
    workflow_id = DBOS.workflow_id
    return f"{workflow_id}:{step_name}" if workflow_id is not None else None


class RunBackend(Protocol):
    """What the dispatcher needs from the durable-execution layer."""

    async def enqueue_run(self, key: SessionKey, event: Event) -> str:
        """Enqueue a drive-session workflow; idempotent per (key, event). Returns its id."""
        ...

    async def live_workflow_id(self, key: SessionKey) -> str | None:
        """The id of the session's enqueued-or-running workflow, if any."""
        ...

    async def workflow_finished(self, workflow_id: str) -> bool:
        """True if the workflow exists and is in a terminal status."""
        ...

    async def failed_workflow_ids(self) -> list[str]:
        """Runs that died: uncaught exception, or exhausted crash-recovery attempts."""
        ...

    async def stale_workflow_ids(self) -> list[str]:
        """Live-looking runs no executor will ever pick up (see DBOSBackend)."""
        ...

    async def cancel_workflow(self, workflow_id: str) -> None:
        """Move a workflow to CANCELLED — terminal, so liveness checks release it."""
        ...


class DBOSBackend:
    """RunBackend over a partitioned DBOS queue."""

    def __init__(self, queue: Queue, drive: DriveFn) -> None:
        self._queue = queue
        self._drive = drive

    async def enqueue_run(self, key: SessionKey, event: Event) -> str:
        workflow_id = run_workflow_id(key, event.event_id)
        with (
            SetWorkflowID(workflow_id),
            SetEnqueueOptions(queue_partition_key=partition_key(key)),
        ):
            await self._queue.enqueue_async(
                self._drive, key.model_dump(mode="json"), event.model_dump(mode="json")
            )
        return workflow_id

    async def live_workflow_id(self, key: SessionKey) -> str | None:
        statuses = await DBOS.list_workflows_async(
            workflow_id_prefix=f"{partition_key(key)}#",
            status=list(LIVE_STATUSES),
            sort_desc=True,
            limit=1,
            load_input=False,
            load_output=False,
        )
        return statuses[0].workflow_id if statuses else None

    async def workflow_finished(self, workflow_id: str) -> bool:
        statuses = await DBOS.list_workflows_async(
            workflow_ids=[workflow_id], load_input=False, load_output=False
        )
        return bool(statuses) and statuses[0].status in TERMINAL_STATUSES

    async def failed_workflow_ids(self) -> list[str]:
        statuses = await DBOS.list_workflows_async(
            queue_name=self._queue.name,
            status=["ERROR", "MAX_RECOVERY_ATTEMPTS_EXCEEDED"],
            load_input=False,
            load_output=False,
        )
        return [s.workflow_id for s in statuses]

    async def stale_workflow_ids(self) -> list[str]:
        """Enqueued-or-pending workflows stamped with another application version.

        No executor will ever run them — DBOS dequeues and crash-recovers only
        workflows matching its own version — yet they look live: steers keep
        targeting them, and a stale PENDING run holds its partition's one-run
        slot, wedging the thread. Assumes a single Hondo process per database:
        during a rolling deploy this would flag the other instance's live runs
        as stale.
        """
        statuses = await DBOS.list_workflows_async(
            queue_name=self._queue.name,
            status=list(LIVE_STATUSES),
            load_input=False,
            load_output=False,
        )
        current = DBOS.application_version
        return [
            s.workflow_id
            for s in statuses
            if s.app_version is not None and s.app_version != current
        ]

    async def cancel_workflow(self, workflow_id: str) -> None:
        await DBOS.cancel_workflow_async(workflow_id)
