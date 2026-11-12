from __future__ import annotations

import asyncio
import contextlib
import faulthandler
import os
import re
import signal
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from dbos import DBOS, DBOSConfig, Queue, SetWorkflowID
from pydantic_ai import Agent
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.app.async_app import AsyncApp

from hondo.chat import ChatClient, ChatStyle
from hondo.config import EngagePolicy, RuntimeConfig, SlackConfig
from hondo.mcp.maintenance import prune_call_journals
from hondo.mcp.store import MCPStore
from hondo.models import Event, SessionKey
from hondo.runtime.dispatcher import Dispatcher
from hondo.runtime.durable import LIVE_STATUSES, DBOSBackend, summary_workflow_id
from hondo.runtime.gateway import Gateway
from hondo.runtime.model_selection import ModelSelector
from hondo.runtime.runner import SessionRunner
from hondo.runtime.summarizer import ThreadSummarizer
from hondo.store.base import Store
from hondo.users import UserDirectory

SESSION_QUEUE = "hondo_sessions"
SUMMARY_WORKFLOW = "hondo.summarize_thread"

logger = structlog.get_logger("hondo.serve")


async def aserve(
    agent: Agent[Any, Any],
    *,
    store: Store,
    slack: SlackConfig,
    bot_token: str,
    client: ChatClient,
    style: ChatStyle,
    directory: UserDirectory,
    engage: EngagePolicy | None = None,
    runtime: RuntimeConfig | None = None,
    model_selector: ModelSelector | None = None,
    mcp_store: MCPStore | None = None,
    summarizer: ThreadSummarizer | None = None,
    decide_approval: Callable[[Event], Awaitable[None]] | None = None,
    screen: Callable[[SessionKey, Event], Awaitable[bool]] | None = None,
) -> None:
    """Run the full teammate: gateway -> dispatcher -> DBOS-durable runs, Socket Mode ingress.

    Durability lives in DBOS on the same Postgres as the store: session runs are
    workflows on a partitioned queue (one live run per thread), steers are durable
    sends into the live run's mailbox, and crash recovery replays recorded steps.
    The process entrypoint configures logging and observability before calling this.

    `bot_token` is resolved from the workspaces table by the caller — `slack`
    only carries the app-level pieces (mode, app_token) that belong to our
    Slack app rather than to any workspace.
    """
    if slack.mode != "socket":
        raise NotImplementedError("Socket Mode only for now; HTTP mode arrives later.")
    # The caller attaches DBOSDurability before launch so its model and MCP
    # steps are registered before DBOS starts recovery.

    if hasattr(faulthandler, "register") and hasattr(signal, "SIGUSR1"):
        faulthandler.register(signal.SIGUSR1, all_threads=True)
        logger.info("stack-dump signal armed", hint=f"kill -USR1 {os.getpid()}")

    runtime = runtime if runtime is not None else RuntimeConfig()

    await store.connect()

    dbos_config: DBOSConfig = {
        "name": "hondo",
        "system_database_url": store.dsn,
        "run_admin_server": False,
        "dbos_system_schema": runtime.system_schema,
    }
    if runtime.application_version is not None:
        dbos_config["application_version"] = f"{runtime.application_version}:listening-channels-v43"
    DBOS(config=dbos_config)

    queue = Queue(
        SESSION_QUEUE,
        concurrency=1,  # per partition: one live run per Slack thread, fleet-wide
        partition_queue=True,
        polling_interval_sec=runtime.queue_polling_interval,
    )
    runner = SessionRunner(
        agent,
        store=store,
        client=client,
        style=style,
        directory=directory,
        runtime=runtime,
        model_selector=model_selector,
        start_summary=(
            summary_starter(summarizer, runtime.max_recovery_attempts)
            if summarizer is not None
            else None
        ),
        screen=screen,
    )

    @DBOS.workflow(name="hondo.drive_session", max_recovery_attempts=runtime.max_recovery_attempts)
    async def drive_session(key_json: dict[str, Any], event_json: dict[str, Any]) -> str:
        return await runner.drive(key_json, event_json)

    backend = DBOSBackend(queue, drive_session)
    dispatcher = Dispatcher(
        store,
        engage if engage is not None else EngagePolicy(),
        backend,
        client=client,
    )
    gateway = Gateway(store)

    DBOS.launch()  # recovers pending runs, starts the queue runner

    async def maintain_forever() -> None:
        calls_cursor = ""
        # Cancel stale runs before reconciliation so their messages can be redispatched.
        while True:
            try:
                await dispatcher.sweep_stale_runs()
                await cancel_stale_summaries()
                await dispatcher.reconcile(runtime.reconcile_interval)
                await dispatcher.sweep_failed_runs()
                await dispatcher.deliver_run_notices()
                deleted = await store.delete_run_snapshots_older_than(
                    runtime.snapshot_max_age_seconds
                )
                if deleted:
                    logger.info("expired run snapshots deleted", count=deleted)
                if mcp_store is not None:
                    calls_cursor = await prune_call_journals(mcp_store, calls_cursor)
            except Exception:
                logger.exception("maintenance sweep failed; retrying next interval")
            await asyncio.sleep(runtime.reconcile_interval)

    maintenance = asyncio.create_task(maintain_forever())

    app = _build_ingress_app(bot_token)

    async def on_event(body: dict[str, Any]) -> None:
        event = await gateway.handle(body)
        if event is not None:
            await dispatcher.dispatch(event)

    app.event("app_mention")(on_event)
    app.event("message")(on_event)

    async def on_action(ack: Callable[[], Awaitable[None]], body: dict[str, Any]) -> None:
        await ack()
        event = await gateway.handle_action(body)
        if event is None or decide_approval is None:
            return
        try:
            await decide_approval(event)
        except Exception:
            logger.exception("approval click failed", approval_id=event.payload["approval_id"])

    app.action(re.compile(r"^approval_(approve|reject)$"))(on_action)

    handler = AsyncSocketModeHandler(app, slack.app_token.get_secret_value())
    try:
        await handler.start_async()
    finally:
        # Graceful shutdown: stop ingress; in-flight workflows recover on the
        # next launch from their recorded steps.
        maintenance.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await maintenance
        await handler.close_async()
        DBOS.destroy()


def summary_starter(
    summarizer: ThreadSummarizer, max_recovery_attempts: int
) -> Callable[[SessionKey, str], Awaitable[None]]:
    @DBOS.workflow(name=SUMMARY_WORKFLOW, max_recovery_attempts=max_recovery_attempts)
    async def summarize_thread(key_json: dict[str, Any], workflow_id: str) -> None:
        await summarizer.summarize(SessionKey.model_validate(key_json), workflow_id)

    # A workflow of its own, on no queue: the thread's next run never waits
    # for it. Two summaries of one thread can overlap; a save only moves forward.
    async def start(key: SessionKey, event_id: str) -> None:
        workflow_id = summary_workflow_id(key, event_id)
        with SetWorkflowID(workflow_id):
            await DBOS.start_workflow_async(
                summarize_thread, key.model_dump(mode="json"), workflow_id
            )

    return start


async def cancel_stale_summaries() -> None:
    """Cancel summaries that a deploy to a new version interrupted.

    They never resume, yet they look pending, and the dashboard keeps the
    provider key of a pending workflow. Nothing is owed to the thread: its
    next run starts a new summary.
    """
    statuses = await DBOS.list_workflows_async(
        name=SUMMARY_WORKFLOW, status=list(LIVE_STATUSES), load_input=False, load_output=False
    )
    for status in statuses:
        if status.app_version is not None and status.app_version != DBOS.application_version:
            await DBOS.cancel_workflow_async(status.workflow_id)
            logger.warning("stale-version summary cancelled", workflow_id=status.workflow_id)


def _build_ingress_app(bot_token: str) -> AsyncApp:
    return AsyncApp(
        token=bot_token,
        # Socket Mode does not verify request signatures.
        signing_secret="unused-in-socket-mode",
        # The ACK is the durability handoff: Slack redelivers un-acked
        # envelopes, so the listener (append + dispatch) must complete before
        # Bolt responds. The default acks first and runs the listener after —
        # a crash in that window would lose the event permanently.
        process_before_response=True,
    )


def serve(
    agent: Agent[Any, Any],
    *,
    store: Store,
    slack: SlackConfig,
    bot_token: str,
    client: ChatClient,
    style: ChatStyle,
    directory: UserDirectory,
    engage: EngagePolicy | None = None,
    runtime: RuntimeConfig | None = None,
    model_selector: ModelSelector | None = None,
) -> None:
    asyncio.run(
        aserve(
            agent,
            store=store,
            slack=slack,
            bot_token=bot_token,
            client=client,
            style=style,
            directory=directory,
            engage=engage,
            runtime=runtime,
            model_selector=model_selector,
        )
    )
