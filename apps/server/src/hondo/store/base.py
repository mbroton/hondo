from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, TypedDict
from uuid import UUID

from hondo.agent_settings import AgentModel
from hondo.models import (
    Event,
    RunNotice,
    Session,
    SessionKey,
    SteerMail,
    SteerPriority,
    WorkspaceRecord,
)


class RunSnapshotMetadata(TypedDict):
    workflow_id: str
    created_at: datetime


class Store(Protocol):
    """Hondo's product data: the event log, session histories, and the steer ledger.

    Queueing, redelivery, and one-live-run-per-thread are DBOS's job (see
    runtime/durable.py); nothing here is a lock or a lease.
    """

    @property
    def dsn(self) -> str:
        """Postgres connection URL shared by the product store and DBOS."""
        ...

    async def connect(self) -> None:
        """Open connections and apply pending migrations. Must be idempotent."""
        ...

    async def aclose(self) -> None: ...

    # --- workspaces ------------------------------------------------------------

    async def get_installed_workspace(self) -> WorkspaceRecord | None:
        """Get the one workspace this deployment serves, in any status."""
        ...

    async def get_agent_model(self, team: str) -> AgentModel | None:
        """Get the default agent's selected model without its provider key."""
        ...

    async def mention_only_channel(self, team: str, channel: str) -> bool:
        """Whether an admin set this channel to answer only when mentioned."""
        ...

    async def listening_goal(self, team: str, channel: str) -> str | None:
        """The goal an admin set for listening in this channel, or None."""
        ...

    async def get_provider_key(self, workspace_id: UUID, connection_id: UUID) -> str | None:
        """Get an encrypted provider key scoped to its workspace and connection."""
        ...

    # --- event log -------------------------------------------------------------

    async def append_event(self, event: Event) -> bool:
        """Append to the durable event log. Returns False if event_id was already
        seen (Slack delivers at-least-once); callers still dispatch on False —
        downstream enqueue/send are idempotent, and the first delivery may have
        crashed before dispatching.
        """
        ...

    async def get_event(self, event_id: str) -> Event | None: ...

    # --- sessions ---------------------------------------------------------------

    async def ensure_session(self, key: SessionKey) -> None:
        """Create an idle session if absent; never change an existing row.
        Idempotent under Slack redelivery.
        """
        ...

    async def get_session(self, key: SessionKey) -> Session | None:
        """The session's state, roster, and last persisted event."""
        ...

    async def save_session(self, session: Session) -> None:
        """Upsert state, roster, and last_event_id by SessionKey."""
        ...

    async def merge_context_key(self, key: SessionKey, name: str, patch: dict[str, Any]) -> None:
        """Atomically merge patch into context[name]. The row must exist."""
        ...

    async def get_session_messages(
        self, key: SessionKey, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """The session's message history, oldest first — only rows written at the
        current HISTORY_SCHEMA_VERSION. A schema bump therefore starts sessions
        fresh automatically: old-version rows simply stop loading. With a limit,
        returns the newest `limit` messages (still oldest first): long sessions
        must not be re-read in full every run.
        """
        ...

    async def append_session_messages(
        self, key: SessionKey, *, run_id: str, messages: list[dict[str, Any]]
    ) -> None:
        """Append one run's message delta, seq 0..n-1, at HISTORY_SCHEMA_VERSION.
        Idempotent per (run_id, seq) — ON CONFLICT DO NOTHING — so a crash-replayed
        save step converges instead of duplicating. No-op for an empty list.
        """
        ...

    async def save_session_run(
        self,
        session: Session,
        *,
        run_id: str,
        messages: list[dict[str, Any]],
        clear_context_entries: tuple[str, list[str]] | None = None,
    ) -> None:
        """Persist one run's outcome — message delta plus session metadata —
        atomically. A failure must never commit messages the session row doesn't
        acknowledge: orphaned rows would resurface in later loads as history no
        run answered against. Append semantics are append_session_messages'.

        clear_context_entries=(name, entry_keys) removes exactly those entries
        from context[name] in the same transaction — entry-level, not the whole
        key, so a record merged by a sweep while this run was live survives the
        clear instead of being consumed unseen. Snapshots for entry_keys are
        deleted in that transaction too.
        """
        ...

    # --- run snapshots ---------------------------------------------------------

    async def save_run_snapshot(
        self, workflow_id: str, key: SessionKey, payload: dict[str, Any]
    ) -> None:
        """Upsert a failed run's snapshot at the current history schema version."""
        ...

    async def list_run_snapshots(self, workflow_ids: Sequence[str]) -> list[RunSnapshotMetadata]:
        """Current-version snapshot ids and creation times."""
        ...

    async def get_run_snapshots(self, workflow_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Current-version snapshot payloads, keyed by workflow id."""
        ...

    async def delete_run_snapshots_older_than(self, max_age_seconds: float) -> int:
        """Delete expired snapshots and return the number removed."""
        ...

    # --- steer ledger -----------------------------------------------------------

    async def insert_mail(
        self,
        key: SessionKey,
        event: Event,
        priority: SteerPriority,
        *,
        target_workflow_id: str | None,
    ) -> SteerMail | None:
        """Record a steer before it is sent; dedupes on event_id.

        Returns the (new or existing-unconsumed) row, updating its target to the
        given workflow id. Returns None if the event's mail was already consumed —
        the reply reached a run; there is nothing left to deliver.
        """
        ...

    async def mail_for_event(self, event_id: str) -> SteerMail | None: ...

    async def mail_claimed_by(self, workflow_id: str) -> list[SteerMail]:
        """Mail consumed by workflow steps, oldest first."""
        ...

    async def unconsumed_mail(self, key: SessionKey) -> list[SteerMail]:
        """All unconsumed mail for the session, oldest first."""
        ...

    async def stale_unconsumed_mail(self, min_age_seconds: float) -> list[SteerMail]:
        """Unconsumed mail older than min_age_seconds, across all sessions (reconciler)."""
        ...

    async def consume_mail(self, mail_ids: Sequence[int]) -> None:
        """Mark mail handled: included in a run or skipped while setup is incomplete.
        The reconciler ignores consumed mail.
        """
        ...

    async def waiting_message_ages(
        self, key: SessionKey, event_id: str
    ) -> tuple[float, float] | None:
        """Seconds since the first and the last arrival among the messages that
        wait for a run: the trigger event and the session's unconsumed mail.
        None when neither exists. Measured by the clock that stamped the
        arrivals, so process clock skew cannot shift them.
        """
        ...

    async def claim_mail(self, key: SessionKey, *, token: str | None) -> list[SteerMail]:
        """Atomically consume and return all unconsumed mail for the session, in
        Slack order (ts): concurrent deliveries can insert out of order.

        A non-None token makes the claim idempotent: consumed rows are stamped
        with it, and a repeat call with the same token returns them again (plus
        any newly arrived mail). A caller whose own checkpoint commits *after*
        the claim can therefore crash between the two commits and still see the
        claimed mail on replay instead of finding an already-consumed ledger.
        """
        ...

    # --- usage events -----------------------------------------------------------

    async def record_usage(self, rows: Sequence[dict[str, Any]]) -> None:
        """Upsert usage rows on (workflow_id, kind, resource) — DO UPDATE, not
        DO NOTHING: a failure-path partial row must converge to the resumed
        run's superset numbers, and identical replays write identical values.
        Row shape is usage.usage_rows_from_responses' output.
        """
        ...

    # --- run notices ------------------------------------------------------------
    # The dead-run announcement outbox: recorded once per run, delivered
    # at-least-once with bounded attempts. Restarts never re-announce a
    # delivered notice, and a crash between record and post never loses one.

    async def record_run_notice(self, workflow_id: str, text: str) -> bool:
        """Record that a dead run owes its thread a notice. Idempotent; the
        first recorded text wins. Returns whether this call was the first.
        """
        ...

    async def run_notice_recorded(self, workflow_id: str) -> bool:
        """Whether a notice was ever recorded for this workflow — the durable
        been-handled signal for sweep follow-up work (the pending-failure
        record), since in-process dedup dies with the process while ERROR
        workflows stay listed forever.
        """
        ...

    async def take_pending_run_notices(self, max_attempts: int) -> list[RunNotice]:
        """Undelivered notices, oldest first, incrementing their attempt count.
        Notices that have been taken max_attempts times stop being returned —
        a permanently failing post must not retry forever.
        """
        ...

    async def mark_run_notice_posted(self, workflow_id: str) -> None:
        """The notice reached the thread (or has nowhere to go); stop offering it."""
        ...


# --- thread summaries ------------------------------------------------------------


@dataclass(frozen=True)
class SummaryView:
    """The summaries that one conversation can see: its own channel's
    summaries, and the summaries of public channels if `include_public`.
    With no channel, only the public ones."""

    team: str
    channel: str | None
    include_public: bool


@dataclass(frozen=True)
class ThreadSummary:
    id: int
    channel: str
    thread_ts: str  # Root of the thread.
    text: str
    # The ts of the newest message that the text covers.
    covered_ts: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class SummaryMatch:
    id: int
    channel: str
    thread_ts: str
    snippet: str  # The part of the text around the matched words.


@dataclass(frozen=True)
class SummarySearch:
    matches: list[SummaryMatch]
    total: int  # All matching summaries, not only the returned ones.


class SummaryStore(Protocol):
    """One summary per channel thread that Hondo took part in."""

    async def search_summaries(
        self, view: SummaryView, words: Sequence[str], limit: int
    ) -> SummarySearch:
        """Summaries that contain any of the words or phrases, after stemming.
        Summaries with more of them rank first."""
        ...

    async def get_summary(self, view: SummaryView, summary_id: int) -> ThreadSummary | None: ...

    async def thread_summary(self, key: SessionKey) -> ThreadSummary | None: ...

    async def save_summary(
        self, key: SessionKey, *, private: bool, text: str, covered_ts: str
    ) -> bool:
        """Write the thread's summary. False when the stored summary already
        covers `covered_ts`: a save never goes back to an older thread. A
        private summary never becomes public again."""
        ...


# --- skills ---------------------------------------------------------------------


@dataclass(frozen=True)
class SkillSummary:
    name: str
    description: str


class SkillDraftOutcome(enum.StrEnum):
    SAVED = "saved"
    # The thread's skill is published; only an admin changes it.
    PUBLISHED = "published"
    NAME_TAKEN = "name_taken"
    LIMIT = "limit"


@dataclass(frozen=True)
class SkillDraft:
    outcome: SkillDraftOutcome
    workspace_id: UUID
    # The thread's skill; None when the thread has none.
    skill_id: UUID | None


class SkillStore(Protocol):
    """The workspace's skills, as the bot sees them: only published skills
    reach a run, and a thread can write one draft."""

    async def channel_skills(
        self, team: str, channel: str, *, listed_only: bool = False
    ) -> list[SkillSummary]:
        """Published skills that this channel can use, by name. listed_only
        leaves out skills that reach the channel only through all_channels."""
        ...

    async def read_skill(
        self, team: str, channel: str, name: str, *, listed_only: bool = False
    ) -> str | None:
        """The SKILL.md of a published skill that this channel can use. listed_only
        works as in channel_skills."""
        ...

    async def save_thread_draft(
        self, team: str, channel: str, thread_ts: str, content: str, token: str | None
    ) -> SkillDraft:
        """Create the thread's draft, or replace it while it is a draft. A new
        draft starts with only this channel. The content must parse. A write
        that already has this token is not done again."""
        ...
