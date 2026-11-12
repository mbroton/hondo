"""In-memory doubles for runtime unit tests: a protocol-complete Store, a Slack
client, and a RunBackend.

Deliberately not imported by conftest — test modules import what they need directly.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import SecretStr

from hondo.agent_settings import AgentModel
from hondo.chat import ChannelInfo, ChatHistoryPage, ChatMessage, ChatUser
from hondo.config import SlackConfig
from hondo.models import (
    HISTORY_SCHEMA_VERSION,
    Event,
    RunNotice,
    Session,
    SessionKey,
    SteerMail,
    SteerPriority,
    WorkspaceRecord,
)
from hondo.runtime.durable import RunBackend, run_workflow_id
from hondo.store.base import RunSnapshotMetadata, Store


def make_slack_config(**overrides: object) -> SlackConfig:
    defaults: dict[str, object] = {
        "app_token": SecretStr("xapp-test"),
    }
    return SlackConfig(**{**defaults, **overrides})  # type: ignore[arg-type]


class FakeStore:
    """Mirrors PostgresStore semantics in memory."""

    def __init__(self) -> None:
        self.dsn = "postgresql://test:test@localhost/test"
        self.workspaces: dict[str, WorkspaceRecord] = {}
        self.agent_models: dict[str, AgentModel] = {}
        self.provider_keys: dict[tuple[UUID, UUID], str] = {}
        self.mention_only_channels: set[tuple[str, str]] = set()
        self.listening_goals: dict[tuple[str, str], str] = {}
        self.events: dict[str, Event] = {}
        self.event_received: dict[str, float] = {}
        self.sessions: dict[SessionKey, Session] = {}
        self.saved_sessions: list[Session] = []
        self.session_messages: dict[SessionKey, list[tuple[str, int, int, dict[str, Any]]]] = {}
        self.run_snapshots: dict[str, tuple[SessionKey, int, dict[str, Any], datetime]] = {}
        self.mail: dict[int, SteerMail] = {}
        self.consumed: set[int] = set()
        self.mail_created: dict[int, float] = {}
        self.claims: dict[str, set[int]] = {}
        self.run_notices: dict[str, dict[str, Any]] = {}
        # Keyed like the usage_events unique constraint, so a re-recorded row
        # replaces its predecessor exactly as ON CONFLICT DO UPDATE would.
        self.usage_events: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._mail_seq = 0

    async def connect(self) -> None:
        pass

    async def aclose(self) -> None:
        pass

    # --- workspaces --------------------------------------------------------------

    async def get_installed_workspace(self) -> WorkspaceRecord | None:
        workspace = next(iter(self.workspaces.values()), None)
        return workspace.model_copy(deep=True) if workspace is not None else None

    async def get_agent_model(self, team: str) -> AgentModel | None:
        return self.agent_models.get(team)

    async def mention_only_channel(self, team: str, channel: str) -> bool:
        return (team, channel) in self.mention_only_channels

    async def listening_goal(self, team: str, channel: str) -> str | None:
        return self.listening_goals.get((team, channel))

    async def get_provider_key(self, workspace_id: UUID, connection_id: UUID) -> str | None:
        return self.provider_keys.get((workspace_id, connection_id))

    def seed_workspace(self, record: WorkspaceRecord) -> None:
        now = datetime.now(UTC)
        self.workspaces[record.slack_team_id] = record.model_copy(
            update={"created_at": now, "updated_at": now}, deep=True
        )

    # --- event log ---------------------------------------------------------------

    async def append_event(self, event: Event) -> bool:
        if event.event_id in self.events:
            return False
        self.events[event.event_id] = event
        self.event_received[event.event_id] = time.monotonic()
        return True

    async def get_event(self, event_id: str) -> Event | None:
        return self.events.get(event_id)

    # --- sessions ------------------------------------------------------------------

    async def get_session(self, key: SessionKey) -> Session | None:
        session = self.sessions.get(key)
        return session.model_copy(deep=True) if session is not None else None

    async def ensure_session(self, key: SessionKey) -> None:
        self.sessions.setdefault(key, Session(key=key))

    async def save_session(self, session: Session) -> None:
        self._store_session(session)

    async def merge_context_key(self, key: SessionKey, name: str, patch: dict[str, Any]) -> None:
        session = self.sessions.get(key)
        if session is None:
            return
        stored = session.model_copy(deep=True)
        current = stored.context.get(name, {})
        stored.context[name] = {**current, **deepcopy(patch)}
        self.sessions[key] = stored

    async def get_session_messages(
        self, key: SessionKey, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        current = [
            deepcopy(message)
            for _, _, version, message in self.session_messages.get(key, [])
            if version == HISTORY_SCHEMA_VERSION
        ]
        return current[-limit:] if limit is not None else current

    async def append_session_messages(
        self, key: SessionKey, *, run_id: str, messages: list[dict[str, Any]]
    ) -> None:
        rows = self.session_messages.setdefault(key, [])
        written = {(stored_run_id, seq) for stored_run_id, seq, _, _ in rows}
        for seq, message in enumerate(messages):
            if (run_id, seq) not in written:
                rows.append((run_id, seq, HISTORY_SCHEMA_VERSION, deepcopy(message)))

    async def save_session_run(
        self,
        session: Session,
        *,
        run_id: str,
        messages: list[dict[str, Any]],
        clear_context_entries: tuple[str, list[str]] | None = None,
    ) -> None:
        await self.append_session_messages(session.key, run_id=run_id, messages=messages)
        self._store_session(session, clear_context_entries=clear_context_entries)
        if clear_context_entries is not None:
            _, entry_keys = clear_context_entries
            for workflow_id in entry_keys:
                self.run_snapshots.pop(workflow_id, None)

    def _store_session(
        self, session: Session, *, clear_context_entries: tuple[str, list[str]] | None = None
    ) -> None:
        existing = self.sessions.get(session.key)
        context = deepcopy(existing.context) if existing is not None else {}
        if clear_context_entries is not None:
            name, entry_keys = clear_context_entries
            entries = {k: v for k, v in context.get(name, {}).items() if k not in entry_keys}
            if entries:
                context[name] = entries
            else:
                context.pop(name, None)
        stored = session.model_copy(update={"context": context}, deep=True)
        self.sessions[session.key] = stored
        self.saved_sessions.append(stored.model_copy(deep=True))

    # --- run snapshots ------------------------------------------------------------

    async def save_run_snapshot(
        self, workflow_id: str, key: SessionKey, payload: dict[str, Any]
    ) -> None:
        existing = self.run_snapshots.get(workflow_id)
        created_at = existing[3] if existing is not None else datetime.now(UTC)
        self.run_snapshots[workflow_id] = (
            key,
            HISTORY_SCHEMA_VERSION,
            deepcopy(payload),
            created_at,
        )

    async def list_run_snapshots(self, workflow_ids: Sequence[str]) -> list[RunSnapshotMetadata]:
        return [
            RunSnapshotMetadata(workflow_id=workflow_id, created_at=row[3])
            for workflow_id in workflow_ids
            if (row := self.run_snapshots.get(workflow_id)) is not None
            and row[1] == HISTORY_SCHEMA_VERSION
        ]

    async def get_run_snapshots(self, workflow_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        return {
            workflow_id: deepcopy(row[2])
            for workflow_id in workflow_ids
            if (row := self.run_snapshots.get(workflow_id)) is not None
            and row[1] == HISTORY_SCHEMA_VERSION
        }

    async def delete_run_snapshots_older_than(self, max_age_seconds: float) -> int:
        cutoff = datetime.now(UTC) - timedelta(seconds=max_age_seconds)
        expired = [
            workflow_id
            for workflow_id, (_, _, _, created_at) in self.run_snapshots.items()
            if created_at < cutoff
        ]
        for workflow_id in expired:
            del self.run_snapshots[workflow_id]
        return len(expired)

    # --- steer ledger -----------------------------------------------------------------

    async def insert_mail(
        self,
        key: SessionKey,
        event: Event,
        priority: SteerPriority,
        *,
        target_workflow_id: str | None,
    ) -> SteerMail | None:
        existing = next((m for m in self.mail.values() if m.event_id == event.event_id), None)
        if existing is not None:
            if existing.id in self.consumed:
                return None
            retargeted = existing.model_copy(update={"target_workflow_id": target_workflow_id})
            self.mail[existing.id] = retargeted
            return retargeted
        self._mail_seq += 1
        mail = SteerMail(
            id=self._mail_seq,
            key=key,
            event_id=event.event_id,
            user=event.user,
            text=event.text,
            files=event.files,
            ts=event.ts,
            priority=priority,
            target_workflow_id=target_workflow_id,
        )
        self.mail[mail.id] = mail
        self.mail_created[mail.id] = time.monotonic()
        return mail

    async def mail_for_event(self, event_id: str) -> SteerMail | None:
        mail = next((m for m in self.mail.values() if m.event_id == event_id), None)
        if mail is None:
            return None
        return mail.model_copy(update={"consumed": mail.id in self.consumed})

    async def mail_claimed_by(self, workflow_id: str) -> list[SteerMail]:
        prefix = f"{workflow_id}:"
        claimed_ids = {
            mail_id
            for token, mail_ids in self.claims.items()
            if token.startswith(prefix)
            for mail_id in mail_ids
        }
        return [
            self.mail[mail_id].model_copy(update={"consumed": True})
            for mail_id in sorted(claimed_ids)
        ]

    async def unconsumed_mail(self, key: SessionKey) -> list[SteerMail]:
        return [
            mail
            for mail_id, mail in sorted(self.mail.items())
            if mail_id not in self.consumed and mail.key == key
        ]

    async def stale_unconsumed_mail(self, min_age_seconds: float) -> list[SteerMail]:
        now = time.monotonic()
        return [
            mail
            for mail_id, mail in sorted(self.mail.items())
            if mail_id not in self.consumed
            and now - self.mail_created.get(mail_id, now) >= min_age_seconds
        ]

    async def consume_mail(self, mail_ids: Sequence[int]) -> None:
        self.consumed.update(mail_ids)

    async def waiting_message_ages(
        self, key: SessionKey, event_id: str
    ) -> tuple[float, float] | None:
        arrivals = [
            self.mail_created[mail_id]
            for mail_id, mail in self.mail.items()
            if mail.key == key and mail_id not in self.consumed
        ]
        if event_id in self.event_received:
            arrivals.append(self.event_received[event_id])
        if not arrivals:
            return None
        now = time.monotonic()
        return now - min(arrivals), now - max(arrivals)

    async def claim_mail(self, key: SessionKey, *, token: str | None) -> list[SteerMail]:
        claimed_ids = [
            mail_id
            for mail_id, mail in sorted(self.mail.items(), key=lambda item: (item[1].ts, item[0]))
            if mail.key == key
            and (
                mail_id not in self.consumed
                or (token is not None and mail_id in self.claims.get(token, set()))
            )
        ]
        self.consumed.update(claimed_ids)
        if token is not None:
            self.claims.setdefault(token, set()).update(claimed_ids)
        return [self.mail[i].model_copy(update={"consumed": True}) for i in claimed_ids]

    # --- usage events ----------------------------------------------------------------

    async def record_usage(self, rows: Sequence[dict[str, Any]]) -> None:
        for row in rows:
            slot = (row["workflow_id"], row["kind"], row["resource"])
            self.usage_events[slot] = deepcopy(row)

    # --- run notices -----------------------------------------------------------------

    async def record_run_notice(self, workflow_id: str, text: str) -> bool:
        fresh = workflow_id not in self.run_notices
        self.run_notices.setdefault(workflow_id, {"text": text, "attempts": 0, "posted": False})
        return fresh

    async def run_notice_recorded(self, workflow_id: str) -> bool:
        return workflow_id in self.run_notices

    async def take_pending_run_notices(self, max_attempts: int) -> list[RunNotice]:
        taken: list[RunNotice] = []
        for workflow_id, notice in sorted(self.run_notices.items()):
            if not notice["posted"] and notice["attempts"] < max_attempts:
                notice["attempts"] += 1
                taken.append(RunNotice(workflow_id=workflow_id, text=notice["text"]))
        return taken

    async def mark_run_notice_posted(self, workflow_id: str) -> None:
        self.run_notices[workflow_id]["posted"] = True


def _protocol_check(store: FakeStore) -> Store:
    # Compile-time only: pyright verifies FakeStore satisfies the Store protocol.
    return store


class FakeBackend:
    """RunBackend double: records enqueues; liveness is scripted by tests."""

    def __init__(self) -> None:
        self.enqueued: list[tuple[SessionKey, Event, str]] = []
        self.live: dict[SessionKey, str] = {}
        self.finished: set[str] = set()
        self.failed: list[str] = []
        self.stale: list[str] = []
        self.cancelled: list[str] = []

    async def enqueue_run(self, key: SessionKey, event: Event) -> str:
        workflow_id = run_workflow_id(key, event.event_id)
        if all(existing != workflow_id for _, _, existing in self.enqueued):
            self.enqueued.append((key, event, workflow_id))
        return workflow_id

    async def live_workflow_id(self, key: SessionKey) -> str | None:
        return self.live.get(key)

    async def workflow_finished(self, workflow_id: str) -> bool:
        return workflow_id in self.finished

    async def failed_workflow_ids(self) -> list[str]:
        return list(self.failed)

    async def stale_workflow_ids(self) -> list[str]:
        return [w for w in self.stale if w not in self.cancelled]

    async def cancel_workflow(self, workflow_id: str) -> None:
        self.cancelled.append(workflow_id)
        self.finished.add(workflow_id)  # CANCELLED is terminal


def _backend_protocol_check(backend: FakeBackend) -> RunBackend:
    return backend


class FakeSlack:
    """Chat client double modeling one Slack conversation.

    Models a single conversation: posted messages are mirrored into
    `thread_messages`, so later reads see them — just like real Slack.
    `channel_messages` holds the channel's top-level history for fetch_history.
    """

    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.reactions: list[dict[str, Any]] = []
        self.statuses: list[dict[str, Any]] = []
        self.thread_messages: list[ChatMessage] = []
        self.channel_messages: list[ChatMessage] = []
        # Conversations missing here are public channels.
        self.channels: dict[str, ChannelInfo | None] = {}
        self.thread_fetches: list[dict[str, Any]] = []
        self.users: dict[str, ChatUser] = {
            "U01": ChatUser(id="U01", name="jane", real_name="Jane Doe", is_bot=False),
            "UBOT": ChatUser(id="UBOT", name="Hondo", real_name="Hondo", is_bot=True),
        }
        self._ts = 0

    async def post_message(self, *, channel: str, text: str, thread_ts: str | None = None) -> str:
        return self._record_post(channel=channel, text=text, thread_ts=thread_ts, blocks=None)

    async def post_notice(self, *, channel: str, text: str, thread_ts: str | None = None) -> str:
        blocks = [{"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}]
        return self._record_post(channel=channel, text=text, thread_ts=thread_ts, blocks=blocks)

    async def update_message(self, *, channel: str, message_ref: str, text: str) -> None:
        self.updates.append({"channel": channel, "message_ref": message_ref, "text": text})

    def _record_post(
        self, *, channel: str, text: str, thread_ts: str | None, blocks: list[dict[str, Any]] | None
    ) -> str:
        self._ts += 1
        ts = f"9000000{self._ts:03d}.000000"
        self.posted.append(
            {
                "channel": channel,
                "text": text,
                "thread_ts": thread_ts,
                "ts": ts,
                "blocks": blocks,
            }
        )
        self.thread_messages.append(ChatMessage(author="B01", text=text, ref=ts))
        return ts

    async def add_reaction(self, *, channel: str, message_ref: str, name: str) -> None:
        self.reactions.append({"channel": channel, "name": name, "message_ref": message_ref})

    async def set_status(self, *, channel: str, thread_ts: str, text: str) -> None:
        # `posts` orders each status against the posted messages.
        self.statuses.append(
            {"channel": channel, "thread_ts": thread_ts, "text": text, "posts": len(self.posted)}
        )

    async def fetch_thread(
        self, *, channel: str, root: str, limit: int | None = None, oldest: str | None = None
    ) -> list[ChatMessage]:
        self.thread_fetches.append({"channel": channel, "root": root, "oldest": oldest})
        messages = [m for m in self.thread_messages if oldest is None or m.ref > oldest]
        return messages if limit is None else messages[:limit]

    async def fetch_history(
        self,
        *,
        channel: str,
        limit: int | None = None,
        oldest: str | None = None,
        latest: str | None = None,
    ) -> ChatHistoryPage:
        # Real-Slack semantics: newest `limit` messages of the bounded range,
        # returned oldest-first, has_more when the range held more.
        in_range = sorted(
            (
                m
                for m in self.channel_messages
                if (oldest is None or m.ref > oldest) and (latest is None or m.ref < latest)
            ),
            key=lambda m: m.ref,
        )
        taken = in_range if limit is None else in_range[max(len(in_range) - limit, 0) :]
        return ChatHistoryPage(messages=taken, has_more=len(taken) < len(in_range))

    async def user_info(self, *, user_id: str) -> ChatUser:
        if user_id not in self.users:
            raise RuntimeError(f"user_info: no such user {user_id}")
        return self.users[user_id]

    async def channel_info(self, channel: str) -> ChannelInfo | None:
        if channel.startswith("D"):
            return ChannelInfo(is_channel=False, is_private=True, is_ext_shared=False)
        return self.channels.get(
            channel, ChannelInfo(is_channel=True, is_private=False, is_ext_shared=False)
        )


def make_event(**overrides: object) -> Event:
    defaults: dict[str, object] = {
        "event_id": "Ev001",
        "kind": "app_mention",
        "team": "T01",
        "channel": "C01",
        "ts": "1700000000.000100",
        "user": "U01",
        "text": "<@UBOT> what is our deploy process?",
        "mentions_bot": True,
        "bot_user": "UBOT",
    }
    return Event(**{**defaults, **overrides})  # type: ignore[arg-type]
