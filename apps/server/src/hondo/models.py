from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from hondo.chat import ChatFile

NO_REPLY = "no_reply"


@dataclass(frozen=True)
class NoReply:
    """The output of a run that ends without a post."""

    reason: str = ""


# Bumps are one-way: loads filter session_messages rows to this version, so a
# bump starts sessions fresh — but rolling the constant back would resurrect
# the pre-bump transcript with every turn since missing. Never decrement.
HISTORY_SCHEMA_VERSION = 1


class WorkspaceRecord(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    slack_team_id: str
    name: str
    status: str = "active"
    bot_token_encrypted: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


class EventKind(enum.StrEnum):
    MESSAGE = "message"
    APP_MENTION = "app_mention"
    REACTION_ADDED = "reaction_added"
    INTERACTION = "interaction"
    TASK_COMPLETED = "task_completed"
    SCHEDULE_TICK = "schedule_tick"


class Intent(enum.StrEnum):
    ENGAGE = "engage"
    STEER = "steer"
    CONTROL = "control"
    OBSERVE = "observe"


class SessionState(enum.StrEnum):
    IDLE = "idle"
    RUNNING = "running"


class SteerPriority(enum.StrEnum):
    """Maps 1:1 onto pydantic-ai's enqueue priority."""

    ASAP = "asap"
    WHEN_IDLE = "when_idle"


class TodoStatus(enum.StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class Todo(BaseModel):
    model_config = ConfigDict(frozen=True)

    # Short imperative step, e.g. "Verify health checks".
    content: str
    status: TodoStatus = TodoStatus.PENDING
    # Present-continuous label shown on the card while in progress,
    # e.g. "Verifying health checks". Falls back to `content` when empty.
    active_form: str = ""


class Event(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str
    kind: EventKind
    team: str
    channel: str
    ts: str
    user: str | None = None
    thread_ts: str | None = None
    text: str = ""
    is_dm: bool = False
    mentions_bot: bool = False
    # The bot's own Slack user id, from the delivery's `authorizations` —
    # identity for the model ("You are @Hondo"), not an event property.
    bot_user: str | None = None
    files: tuple[ChatFile, ...] = ()
    payload: dict[str, Any] = Field(default_factory=dict)


class SessionKey(BaseModel):
    model_config = ConfigDict(frozen=True)

    team: str
    channel: str
    thread_root: str

    @classmethod
    def from_event(cls, event: Event) -> SessionKey:
        # DMs are one rolling conversation keyed by the channel itself;
        # everywhere else the session roots at the thread's first message.
        root = event.channel if event.is_dm else (event.thread_ts or event.ts)
        return cls(team=event.team, channel=event.channel, thread_root=root)


class Session(BaseModel):
    key: SessionKey
    state: SessionState = SessionState.IDLE
    # Everyone seen in this session (spoke or was mentioned): user id → display
    # name. Message text carries expanded names only; this map keeps the ids
    # the model needs to write mentions back.
    roster: dict[str, str] = Field(default_factory=dict)
    # Event whose run last persisted this session.
    last_event_id: str | None = None
    # Generic per-session slot, keyed by name; call sites use typed accessors
    # such as PendingFailure, never raw dict reads.
    context: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PendingFailure(BaseModel):
    """A run that died before saving history: pointer, not copy.

    The trigger text lives in the events log and the run's claimed steers in
    steer_mail (via claimed_by); this record only identifies them and labels
    the failure for the next run's prompt.
    """

    model_config = ConfigDict(frozen=True)

    workflow_id: str
    event_id: str
    # Internal label ("crashed", "TimeoutError") — never exception-message
    # content (it can carry secrets) and never rendered to the model: the
    # [runtime] note states only that a run failed, without a cause.
    reason: str
    failed_at: str  # ISO 8601 UTC

    CONTEXT_KEY: ClassVar[str] = "pending"

    @classmethod
    def list_from_context(cls, context: dict[str, Any]) -> list[PendingFailure]:
        records = context.get(cls.CONTEXT_KEY, {})
        failures = [
            cls.model_validate({"workflow_id": workflow_id, **record})
            for workflow_id, record in records.items()
        ]
        return sorted(failures, key=lambda failure: failure.failed_at)

    def to_patch(self) -> dict[str, Any]:
        return {
            self.workflow_id: {
                "event_id": self.event_id,
                "reason": self.reason,
                "failed_at": self.failed_at,
            }
        }


class ThreadFollow:
    """The thread switch people set in Slack: answer only when mentioned."""

    CONTEXT_KEY: ClassVar[str] = "follow"

    @classmethod
    def mention_only(cls, context: dict[str, Any]) -> bool:
        return context.get(cls.CONTEXT_KEY, {}).get("mention_only") is True

    @staticmethod
    def patch(*, mention_only: bool) -> dict[str, Any]:
        return {"mention_only": mention_only}


class RunNotice(BaseModel):
    """A dead-run announcement owed to a thread: one notice-outbox row."""

    model_config = ConfigDict(frozen=True)

    workflow_id: str
    text: str = ""


class SteerMail(BaseModel):
    """A message for a live run: the ack ledger behind DBOS steer sends.

    The send itself is durable (DBOS mailbox); this row exists so the
    reconciler can spot a steer whose target workflow finished without
    consuming it and re-dispatch a fresh run.
    """

    model_config = ConfigDict(frozen=True)

    id: int
    key: SessionKey
    event_id: str
    user: str | None = None
    text: str = ""
    files: tuple[ChatFile, ...] = ()
    ts: str
    priority: SteerPriority = SteerPriority.ASAP
    # Workflow id the steer was last sent to; None before any send.
    target_workflow_id: str | None = None
    consumed: bool = False
