from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from weakref import WeakValueDictionary

import structlog

from hondo.chat import ChatClient
from hondo.config import EngagePolicy
from hondo.models import (
    Event,
    Intent,
    PendingFailure,
    SessionKey,
    SteerMail,
    SteerPriority,
    ThreadFollow,
)
from hondo.runtime.durable import RunBackend
from hondo.runtime.router import route
from hondo.store.base import Store

logger = structlog.get_logger("hondo.dispatcher")

# Delivery attempts per dead-run notice before giving up: a permanently
# failing post (deleted channel, revoked scope) must not retry forever.
_MAX_NOTICE_ATTEMPTS = 10


class Dispatcher:
    """The fresh-run-vs-steer decision for each ingress event.

    ENGAGE becomes an enqueued drive-session workflow (the partitioned queue
    serializes runs per thread; the deterministic workflow id absorbs Slack
    redeliveries). STEER becomes a ledger insert the live run claims at its
    next model request. Nothing can strand silently: if the target finishes
    without claiming the mail — the insert raced the run's end — the
    post-insert status check (and, for the residual window, the `reconcile`
    sweep) enqueues a fresh run that drains it from the store.
    """

    def __init__(
        self,
        store: Store,
        policy: EngagePolicy,
        backend: RunBackend,
        *,
        client: ChatClient | None = None,
    ) -> None:
        self._store = store
        self._policy = policy
        self._backend = backend
        self._client = client
        # Per-process cache over the durable notice outbox: skips a store
        # round-trip per already-recorded run on every sweep tick.
        self._recorded: set[str] = set()
        # Slack runs each event's handler concurrently. Without this lock, each
        # message of a burst can see no live run and start its own run.
        self._session_locks: WeakValueDictionary[SessionKey, asyncio.Lock] = WeakValueDictionary()

    async def dispatch(self, event: Event) -> None:
        key = SessionKey.from_event(event)
        lock = self._session_locks.setdefault(key, asyncio.Lock())
        async with lock:
            await self._dispatch(key, event)

    async def _dispatch(self, key: SessionKey, event: Event) -> None:
        session = await self._store.get_session(key)
        session_exists = session is not None
        addressed = event.mentions_bot or event.is_dm
        priority = SteerPriority.ASAP if addressed else SteerPriority.WHEN_IDLE
        mention_only = not addressed and (
            (session is not None and ThreadFollow.mention_only(session.context))
            or await self._store.mention_only_channel(event.team, event.channel)
        )
        listening = (
            not addressed
            and not mention_only
            and not session_exists
            and await self._store.listening_goal(event.team, event.channel) is not None
        )

        if (
            route(
                event,
                self._policy,
                session_exists=session_exists,
                mention_only=mention_only,
                listening=listening,
            )
            is Intent.ENGAGE
        ):
            # Durable engagement at acceptance, before any run outcome: a failed
            # first run must not leave the thread cold, or later unaddressed
            # replies get dropped. A message heard only by listening gets its
            # session when its run passes the screen.
            if not listening:
                await self._store.ensure_session(key)
            live = await self._backend.live_workflow_id(key)
            if live is not None:
                await self._steer(key, event, priority, live)
            else:
                await self._backend.enqueue_run(key, event)
                logger.info("run enqueued", session=str(key), event_id=event.event_id)
            return

        live = await self._backend.live_workflow_id(key)
        would_steer = route(
            event,
            self._policy,
            session_exists=session_exists,
            run_live=live is not None,
            mention_only=mention_only,
        )
        if would_steer is Intent.STEER and live is not None:
            await self._steer(key, event, priority, live)
        else:
            # Cold thread, or policy says no: the event stays in the log;
            # nothing further to do.
            logger.debug("event not actionable", session=str(key), event_id=event.event_id)

    async def _steer(
        self, key: SessionKey, event: Event, priority: SteerPriority, workflow_id: str
    ) -> None:
        mail = await self._store.insert_mail(key, event, priority, target_workflow_id=workflow_id)
        if mail is None:
            return  # already consumed: the reply reached a run
        logger.info(
            "message queued for live run",
            session=str(key),
            event_id=event.event_id,
            workflow_id=workflow_id,
            priority=priority.value,
        )
        # Insert-races-run-end: the target may have passed its last claim point.
        # If it is already terminal and the mail unconsumed, hand the reply to a
        # fresh run (idempotent workflow id).
        if await self._backend.workflow_finished(workflow_id):
            await self._redispatch_if_unconsumed(mail)

    async def sweep_failed_runs(self) -> None:
        """Surface runs that died: a system notice in the thread, loud log.

        Covers both failure shapes — an uncaught exception (DBOS status ERROR)
        and exhausted crash recovery (MAX_RECOVERY_ATTEMPTS_EXCEEDED). Either
        would otherwise fail *silently*: the user asks and then hears nothing,
        forever — the poison-event lesson from dogfooding. The run stays
        resumable by hand; this sweep only makes it visible. The notice goes
        through the durable outbox: recorded once per run (restarts never
        re-announce history), delivered by deliver_run_notices.
        """
        for workflow_id in await self._backend.failed_workflow_ids():
            if workflow_id in self._recorded:
                continue
            # Order matters twice over. Pending persists before the notice
            # records, so a transient store error retries on the next tick
            # instead of being masked by an already-recorded notice. And the
            # notice is the durable been-handled gate: ERROR workflows stay
            # listed forever, and re-persisting after a restart would
            # resurrect a record a later run already consumed.
            try:
                if not await self._store.run_notice_recorded(workflow_id) and (
                    await self._persist_pending(workflow_id, "crashed")
                ):
                    await self._store.record_run_notice(
                        workflow_id,
                        "⚠️ Hondo hit an error and couldn't finish this. Mention it again to retry.",
                    )
                    logger.error("run failed; resumable by hand", workflow_id=workflow_id)
                self._recorded.add(workflow_id)
            except Exception:
                logger.exception(
                    "failed-run sweep could not persist; will retry", workflow_id=workflow_id
                )

    async def sweep_stale_runs(self) -> None:
        """Clear runs wedged by a version-changing deployment.

        A workflow enqueued or interrupted on another application version will
        never be dequeued or recovered by this process, yet it looks live: the
        dispatcher would steer replies into it forever, and a stale PENDING
        run holds its partition's one-run slot so fresh runs cannot start.
        Cancelling it is what un-wedges the thread — CANCELLED is terminal, so
        the steer reconciler redispatches its unconsumed mail to a fresh run
        and liveness checks stop routing to it. The interrupted request itself
        is not replayed (old-version step records don't transfer); the notice
        asks for a re-mention, same as a crashed run.

        The notice is recorded *before* the cancel: once cancelled the run
        stops looking stale, so recording after would lose the notice to a
        crash in between. A crash after the record but before the cancel just
        re-runs the sweep — the record is idempotent.
        """
        for workflow_id in await self._backend.stale_workflow_ids():
            # Persist must land before the cancel: CANCELLED drops the run
            # from future stale scans, so a swallowed failure here would lose
            # the trigger permanently. Skipping keeps the run stale — retried.
            try:
                announce = await self._persist_pending(workflow_id, "interrupted by a deployment")
            except Exception:
                logger.exception(
                    "stale-run sweep could not persist; will retry", workflow_id=workflow_id
                )
                continue
            if announce:
                await self._store.record_run_notice(
                    workflow_id,
                    "⚠️ Hondo was updated mid-task and couldn't finish this. "
                    "Mention it again to retry.",
                )
            try:
                await self._backend.cancel_workflow(workflow_id)
            except Exception:
                logger.exception("stale-run cancel failed", workflow_id=workflow_id)
                continue
            logger.warning("stale-version run cancelled", workflow_id=workflow_id)

    async def deliver_run_notices(self) -> None:
        """Drain the dead-run notice outbox into Slack threads.

        At-least-once: a crash between post and mark can repeat one notice —
        the acceptable dual of never losing the only signal a thread gets that
        its run died. Attempts are bounded in the store; a post that keeps
        failing is eventually abandoned (loudly).
        """
        for notice in await self._store.take_pending_run_notices(_MAX_NOTICE_ATTEMPTS):
            event_id = notice.workflow_id.rsplit("#", 1)[-1]
            event = await self._store.get_event(event_id)
            if event is not None and self._client is not None:
                try:
                    await self._client.post_notice(
                        channel=event.channel,
                        text=notice.text,
                        thread_ts=None if event.is_dm else (event.thread_ts or event.ts),
                    )
                except Exception:
                    logger.warning(
                        "dead-run notice post failed; will retry",
                        workflow_id=notice.workflow_id,
                        event_id=event_id,
                        exc_info=True,
                    )
                    continue  # stays pending; retried until the attempt cap
            await self._store.mark_run_notice_posted(notice.workflow_id)
            logger.info("dead run announced", workflow_id=notice.workflow_id, event_id=event_id)

    async def reconcile(self, min_age_seconds: float) -> None:
        """Sweep the steer ledger for mail stranded by the residual insert race.

        Covers the window the post-insert check can't: the target was observed
        live, but its run-end drain had already passed when the insert landed.
        serve() runs this on a timer.
        """
        for mail in await self._store.stale_unconsumed_mail(min_age_seconds):
            try:
                target_gone = mail.target_workflow_id is None or (
                    await self._backend.workflow_finished(mail.target_workflow_id)
                )
                if target_gone:
                    await self._redispatch_if_unconsumed(mail)
            except Exception:
                logger.exception("steer reconcile failed", mail_id=mail.id, event_id=mail.event_id)

    async def _persist_pending(self, workflow_id: str, reason: str) -> bool:
        """Leave a dead run's pending-failure record for the thread's next run,
        and say whether the thread gets a notice.

        Hard crashes never reach the runner's in-process failure handler, so
        the sweeps write the record here. Idempotent (merge by workflow id)
        and raising: callers must not flip their durable done-marker on a
        failed write, or the trigger is lost for good — the sweep's next tick
        is the retry. A run for a message that Hondo only heard by listening,
        in a thread it never joined, leaves nothing: nobody asked Hondo.
        """
        event = await self._store.get_event(workflow_id.rsplit("#", 1)[-1])
        if event is None:
            logger.warning("dead run has no logged event", workflow_id=workflow_id)
            return True
        key = SessionKey.from_event(event)
        if not (event.mentions_bot or event.is_dm) and await self._store.get_session(key) is None:
            logger.warning("dead run of a listened message; no notice", workflow_id=workflow_id)
            return False
        await self._store.ensure_session(key)
        failure = PendingFailure(
            workflow_id=workflow_id,
            event_id=event.event_id,
            reason=reason,
            failed_at=datetime.now(UTC).isoformat(),
        )
        await self._store.merge_context_key(key, PendingFailure.CONTEXT_KEY, failure.to_patch())
        return True

    async def _redispatch_if_unconsumed(self, mail: SteerMail) -> None:
        current = await self._store.mail_for_event(mail.event_id)
        if current is None or current.consumed:
            return
        event = await self._store.get_event(mail.event_id)
        if event is None:
            logger.warning(
                "stranded mail has no logged event; leaving for next run",
                mail_id=mail.id,
                event_id=mail.event_id,
            )
            return
        await self._backend.enqueue_run(mail.key, event)
