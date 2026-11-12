import json
from datetime import datetime
from uuid import UUID

import asyncpg

from hondo.agent_settings import AgentModel
from hondo.chat import ChatFile
from hondo.models import (
    HISTORY_SCHEMA_VERSION,
    Event,
    EventKind,
    Session,
    SessionKey,
    SessionState,
    SteerPriority,
)
from hondo.store.base import SummaryView
from hondo.store.postgres import PostgresStore

EVENT = Event(
    event_id="Ev123",
    kind=EventKind.APP_MENTION,
    team="T01",
    channel="C01",
    ts="1700000000.000100",
    user="U01",
    thread_ts=None,
    text="<@B01> deploy the thing",
    mentions_bot=True,
    payload={"type": "app_mention", "blocks": [{"type": "rich_text"}]},
)

KEY = SessionKey(team="T01", channel="C01", thread_root="1700000000.000100")

HISTORY = [
    {"kind": "request", "parts": [{"part_kind": "user-prompt", "content": "deploy the thing"}]},
    {"kind": "response", "parts": [{"part_kind": "text", "content": "on it"}]},
]

SNAPSHOT = {
    "messages": HISTORY,
    "covers": ["WF1"],
    "delivered_steer_ids": [1, 2],
}


# --- workspaces ----------------------------------------------------------------


async def _insert_workspace(store: PostgresStore, team: str, *, status: str = "active") -> UUID:
    return await store._connected_pool.fetchval(
        """
        INSERT INTO workspaces (slack_team_id, name, status, bot_token_encrypted)
        VALUES ($1, $2, $3, 'encrypted-token') RETURNING id
        """,
        team,
        f"{team} workspace",
        status,
    )


async def test_workspace_reads(store: PostgresStore) -> None:
    assert await store.get_installed_workspace() is None
    workspace_id = await _insert_workspace(store, "T01", status="disabled")

    installed = await store.get_installed_workspace()
    assert installed is not None
    assert (
        installed.id,
        installed.slack_team_id,
        installed.name,
        installed.status,
        installed.bot_token_encrypted,
    ) == (workspace_id, "T01", "T01 workspace", "disabled", "encrypted-token")


async def test_agent_model_and_provider_key_are_scoped_to_the_workspace(
    store: PostgresStore,
) -> None:
    workspace_id = await _insert_workspace(store, "T01")
    other_id = await _insert_workspace(store, "T02")
    connection_id = await store._connected_pool.fetchval(
        """
        INSERT INTO provider_connections
            (workspace_id, provider, name, api_key_encrypted, check_status)
        VALUES ($1, 'openai', 'Production', 'key-ciphertext', 'valid') RETURNING id
        """,
        workspace_id,
    )
    await store._connected_pool.execute(
        """
        INSERT INTO agents (workspace_id, name, provider_connection_id, model_name)
        VALUES ($1, 'hondo', $2, 'gpt-5.2')
        """,
        workspace_id,
        connection_id,
    )

    assert await store.get_agent_model("T01") == AgentModel(
        connection_id=connection_id,
        workspace_id=workspace_id,
        provider="openai",
        model_name="gpt-5.2",
    )
    assert await store.get_agent_model("T02") is None
    assert await store.get_provider_key(workspace_id, connection_id) == "key-ciphertext"
    assert await store.get_provider_key(other_id, connection_id) is None


async def test_event_roundtrip(store: PostgresStore) -> None:
    assert await store.append_event(EVENT) is True
    assert await store.get_event("Ev123") == EVENT


async def test_files_roundtrip_with_events_and_mail(store: PostgresStore) -> None:
    files = (
        ChatFile("alert.png", "https://files.slack.com/files-pri/T01-F01/alert.png", "image/png"),
        ChatFile("report.pdf"),
    )
    event = EVENT.model_copy(update={"event_id": "Ev777", "files": files})
    assert await store.append_event(event) is True
    stored = await store.get_event("Ev777")
    assert stored is not None and stored.files == files
    await store.insert_mail(KEY, event, SteerPriority.ASAP, target_workflow_id=None)
    claimed = await store.claim_mail(KEY, token=None)
    assert [mail.files for mail in claimed] == [files]


async def test_get_missing_event_returns_none(store: PostgresStore) -> None:
    assert await store.get_event("Ev404") is None


async def test_duplicate_event_is_deduped(store: PostgresStore) -> None:
    assert await store.append_event(EVENT) is True
    redelivery = EVENT.model_copy(update={"text": "redelivered with different body"})
    assert await store.append_event(redelivery) is False
    stored = await store.get_event("Ev123")
    assert stored is not None
    assert stored.text == EVENT.text  # first write wins


async def test_get_missing_session_returns_none(store: PostgresStore) -> None:
    assert await store.get_session(KEY) is None


async def test_session_context_defaults_and_roundtrips(store: PostgresStore) -> None:
    await store.ensure_session(KEY)
    session = await store.get_session(KEY)
    assert session is not None
    assert session.context == {}

    await store.merge_context_key(KEY, "pending", {"WF1": {"event_id": "Ev1"}})
    updated = await store.get_session(KEY)
    assert updated is not None
    assert updated.context == {"pending": {"WF1": {"event_id": "Ev1"}}}


async def test_ensure_session_is_idempotent_and_preserves_existing_rows(
    store: PostgresStore,
) -> None:
    await store.ensure_session(KEY)
    created = await store.get_session(KEY)
    assert created is not None
    assert created.state is SessionState.IDLE

    await store.ensure_session(KEY)
    assert await store.get_session(KEY) == created

    await store.save_session(
        Session(
            key=KEY,
            state=SessionState.RUNNING,
            roster={"U01": "jane"},
            last_event_id="Ev123",
        )
    )
    await store.merge_context_key(KEY, "pending", {"WF1": {"event_id": "Ev123"}})
    existing = await store.get_session(KEY)
    assert existing is not None

    await store.ensure_session(KEY)
    assert await store.get_session(KEY) == existing


async def test_merge_context_key_merges_entries_and_preserves_other_keys(
    store: PostgresStore,
) -> None:
    await store.ensure_session(KEY)
    await store.merge_context_key(KEY, "todo", {"items": ["ship"]})
    await store.merge_context_key(KEY, "pending", {"WF1": {"event_id": "Ev1"}})
    await store.merge_context_key(KEY, "pending", {"WF2": {"event_id": "Ev2"}})
    await store.merge_context_key(KEY, "pending", {"WF1": {"event_id": "Ev1-new"}})

    session = await store.get_session(KEY)
    assert session is not None
    assert session.context == {
        "todo": {"items": ["ship"]},
        "pending": {
            "WF1": {"event_id": "Ev1-new"},
            "WF2": {"event_id": "Ev2"},
        },
    }


async def test_session_metadata_roundtrip(store: PostgresStore) -> None:
    await store.save_session(
        Session(
            key=KEY,
            state=SessionState.RUNNING,
            roster={"U01": "jane"},
            last_event_id="Ev123",
        )
    )
    loaded = await store.get_session(KEY)
    assert loaded is not None
    assert loaded.key == KEY
    assert loaded.state is SessionState.RUNNING
    assert loaded.roster == {"U01": "jane"}
    assert loaded.last_event_id == "Ev123"
    assert "history" not in loaded.model_dump()
    assert "history_version" not in loaded.model_dump()
    assert loaded.created_at is not None
    assert loaded.updated_at is not None


async def test_save_session_upserts(store: PostgresStore) -> None:
    await store.save_session(Session(key=KEY, state=SessionState.RUNNING))
    first = await store.get_session(KEY)
    assert first is not None

    await store.save_session(
        Session(key=KEY, state=SessionState.IDLE, roster={"U01": "jane"}, last_event_id="Ev2")
    )
    second = await store.get_session(KEY)
    assert second is not None
    assert second.state is SessionState.IDLE
    assert second.roster == {"U01": "jane"}
    assert second.last_event_id == "Ev2"
    assert second.created_at == first.created_at
    assert second.updated_at is not None and first.updated_at is not None
    assert second.updated_at >= first.updated_at


async def test_append_get_messages_preserves_order_across_runs(store: PostgresStore) -> None:
    await store.append_session_messages(key=KEY, run_id="run-1", messages=HISTORY)
    second_run = [
        {"kind": "request", "parts": [{"part_kind": "user-prompt", "content": "ship it"}]},
        {"kind": "response", "parts": [{"part_kind": "text", "content": "shipped"}]},
    ]
    await store.append_session_messages(key=KEY, run_id="run-2", messages=second_run)

    assert await store.get_session_messages(KEY) == [*HISTORY, *second_run]
    # A limit keeps the newest tail, still oldest first.
    assert await store.get_session_messages(KEY, limit=3) == [HISTORY[1], *second_run]


async def test_reappending_same_run_is_a_noop(store: PostgresStore) -> None:
    await store.append_session_messages(key=KEY, run_id="run-1", messages=HISTORY)
    changed = [{"kind": "request", "parts": []}, *HISTORY[1:]]
    await store.append_session_messages(key=KEY, run_id="run-1", messages=changed)

    assert await store.get_session_messages(KEY) == HISTORY


async def test_messages_from_another_schema_version_are_not_returned(
    store: PostgresStore,
) -> None:
    await store.append_session_messages(key=KEY, run_id="current", messages=HISTORY[:1])
    conn = await asyncpg.connect(store.dsn)
    try:
        await conn.execute(
            """
            INSERT INTO session_messages
                (team, channel, thread_root, run_id, seq, history_version, message)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            KEY.team,
            KEY.channel,
            KEY.thread_root,
            "old",
            0,
            HISTORY_SCHEMA_VERSION + 1,
            json.dumps(HISTORY[1]),
        )
    finally:
        await conn.close()

    assert await store.get_session_messages(KEY) == HISTORY[:1]


async def test_empty_message_append_is_a_noop(store: PostgresStore) -> None:
    await store.append_session_messages(key=KEY, run_id="empty", messages=[])

    conn = await asyncpg.connect(store.dsn)
    try:
        count = await conn.fetchval("SELECT count(*) FROM session_messages")
    finally:
        await conn.close()
    assert count == 0


async def test_save_session_run_persists_delta_and_metadata(store: PostgresStore) -> None:
    await store.save_session_run(
        Session(key=KEY, roster={"U01": "jane"}, last_event_id="Ev1"),
        run_id="Ev1",
        messages=HISTORY,
    )
    session = await store.get_session(KEY)
    assert session is not None
    assert session.last_event_id == "Ev1"
    assert await store.get_session_messages(KEY) == HISTORY

    # Crash-replay of the save step: same run_id converges, metadata re-upserts.
    await store.save_session_run(
        Session(key=KEY, roster={"U01": "jane", "U02": "bob"}, last_event_id="Ev1"),
        run_id="Ev1",
        messages=HISTORY,
    )
    replayed = await store.get_session(KEY)
    assert replayed is not None
    assert replayed.roster == {"U01": "jane", "U02": "bob"}
    assert await store.get_session_messages(KEY) == HISTORY


async def test_save_session_run_clears_consumed_context_entries_atomically(
    store: PostgresStore,
) -> None:
    await store.ensure_session(KEY)
    await store.merge_context_key(KEY, "pending", {"WF1": {"event_id": "Ev1"}})
    await store.merge_context_key(KEY, "todo", {"items": ["ship"]})

    await store.save_session_run(
        Session(key=KEY, roster={"U01": "jane"}, last_event_id="Ev1"),
        run_id="Ev1",
        messages=HISTORY,
        clear_context_entries=("pending", ["WF1"]),
    )

    session = await store.get_session(KEY)
    assert session is not None
    assert session.roster == {"U01": "jane"}
    assert session.last_event_id == "Ev1"
    # The key itself goes once its last entry is cleared; other keys survive.
    assert session.context == {"todo": {"items": ["ship"]}}
    assert await store.get_session_messages(KEY) == HISTORY


async def test_clear_spares_entries_merged_while_the_run_was_live(
    store: PostgresStore,
) -> None:
    # A sweep can announce another dead workflow between a run's load and its
    # save; clearing only the consumed entries must leave that record intact.
    await store.ensure_session(KEY)
    await store.merge_context_key(KEY, "pending", {"WF1": {"event_id": "Ev1"}})
    await store.merge_context_key(KEY, "pending", {"WF2": {"event_id": "Ev2"}})

    await store.save_session_run(
        Session(key=KEY, last_event_id="Ev3"),
        run_id="Ev3",
        messages=HISTORY,
        clear_context_entries=("pending", ["WF1"]),
    )

    session = await store.get_session(KEY)
    assert session is not None
    assert session.context == {"pending": {"WF2": {"event_id": "Ev2"}}}


async def test_session_saves_do_not_clobber_context(store: PostgresStore) -> None:
    await store.ensure_session(KEY)
    await store.merge_context_key(KEY, "pending", {"WF1": {"event_id": "Ev1"}})

    await store.save_session(Session(key=KEY, state=SessionState.RUNNING))
    saved = await store.get_session(KEY)
    assert saved is not None
    assert saved.context == {"pending": {"WF1": {"event_id": "Ev1"}}}

    await store.save_session_run(
        Session(key=KEY, state=SessionState.IDLE, last_event_id="Ev2"),
        run_id="Ev2",
        messages=HISTORY,
    )
    saved_run = await store.get_session(KEY)
    assert saved_run is not None
    assert saved_run.context == {"pending": {"WF1": {"event_id": "Ev1"}}}


async def test_run_snapshot_payload_roundtrip(store: PostgresStore) -> None:
    await store.save_run_snapshot("WF1", KEY, SNAPSHOT)

    assert await store.get_run_snapshots(["WF1"]) == {"WF1": SNAPSHOT}
    listed = await store.list_run_snapshots(["missing", "WF1"])
    assert len(listed) == 1
    assert listed[0]["workflow_id"] == "WF1"
    assert isinstance(listed[0]["created_at"], datetime)


async def test_large_run_snapshot_payload_roundtrips_through_jsonb(
    store: PostgresStore,
) -> None:
    large_payload = {
        "messages": [
            {
                "kind": "request",
                "parts": [
                    {
                        "part_kind": "tool-return",
                        "tool_name": "large",
                        "tool_call_id": "large-call",
                        "content": {
                            "items": [
                                {"id": index, "payload": "x" * 100} for index in range(1_000)
                            ],
                            "huge_string": "y" * 60_000,
                        },
                    }
                ],
            }
        ],
        "covers": ["WF-large"],
        "delivered_steer_ids": [],
    }

    await store.save_run_snapshot("WF-large", KEY, large_payload)

    assert await store.get_run_snapshots(["WF-large"]) == {"WF-large": large_payload}


async def test_save_run_snapshot_upserts_by_workflow_id(store: PostgresStore) -> None:
    await store.save_run_snapshot("WF1", KEY, SNAPSHOT)
    replacement = {**SNAPSHOT, "covers": ["WF1", "WF0"]}

    await store.save_run_snapshot("WF1", KEY, replacement)

    assert await store.get_run_snapshots(["WF1"]) == {"WF1": replacement}


async def test_run_snapshots_from_another_schema_version_are_not_returned(
    store: PostgresStore,
) -> None:
    await store.save_run_snapshot("current", KEY, SNAPSHOT)
    conn = await asyncpg.connect(store.dsn)
    try:
        await conn.execute(
            """
            INSERT INTO run_snapshots
                (workflow_id, team, channel, thread_root, history_version, payload)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            "other-version",
            KEY.team,
            KEY.channel,
            KEY.thread_root,
            HISTORY_SCHEMA_VERSION + 1,
            json.dumps(SNAPSHOT),
        )
    finally:
        await conn.close()

    assert await store.get_run_snapshots(["current", "other-version"]) == {"current": SNAPSHOT}
    assert {
        item["workflow_id"] for item in await store.list_run_snapshots(["current", "other-version"])
    } == {"current"}


async def test_get_run_snapshots_ignores_absent_ids(store: PostgresStore) -> None:
    await store.save_run_snapshot("present", KEY, SNAPSHOT)

    assert await store.get_run_snapshots(["missing", "present"]) == {"present": SNAPSHOT}


async def test_save_session_run_deletes_consumed_snapshots(store: PostgresStore) -> None:
    for workflow_id in ("WF1", "WF2", "WF3"):
        await store.save_run_snapshot(workflow_id, KEY, {**SNAPSHOT, "covers": [workflow_id]})
    await store.ensure_session(KEY)
    await store.merge_context_key(
        KEY,
        "pending",
        {
            workflow_id: {"event_id": f"Ev{workflow_id[-1]}"}
            for workflow_id in ("WF1", "WF2", "WF3")
        },
    )

    await store.save_session_run(
        Session(key=KEY, last_event_id="Ev4"),
        run_id="Ev4",
        messages=HISTORY,
        clear_context_entries=("pending", ["WF1", "WF2"]),
    )

    assert await store.get_run_snapshots(["WF1", "WF2", "WF3"]) == {
        "WF3": {**SNAPSHOT, "covers": ["WF3"]}
    }


async def test_delete_run_snapshots_older_than_deletes_only_expired_rows(
    store: PostgresStore,
) -> None:
    await store.save_run_snapshot("old", KEY, SNAPSHOT)
    await store.save_run_snapshot("recent", KEY, SNAPSHOT)
    conn = await asyncpg.connect(store.dsn)
    try:
        await conn.execute(
            "UPDATE run_snapshots SET created_at = now() - interval '2 hours' "
            "WHERE workflow_id = 'old'"
        )
    finally:
        await conn.close()

    assert await store.delete_run_snapshots_older_than(3600) == 1
    assert await store.get_run_snapshots(["old", "recent"]) == {"recent": SNAPSHOT}


async def test_mail_claimed_by_matches_complete_workflow_id_in_claim_order(
    store: PostgresStore,
) -> None:
    first = await store.insert_mail(KEY, EVENT, SteerPriority.ASAP, target_workflow_id="WF1")
    assert first is not None
    await store.claim_mail(KEY, token="WF1:step")

    second_event = EVENT.model_copy(update={"event_id": "Ev124", "ts": "1700000000.000200"})
    second = await store.insert_mail(
        KEY, second_event, SteerPriority.ASAP, target_workflow_id="WF12"
    )
    assert second is not None
    await store.claim_mail(KEY, token="WF12:step")

    third_event = EVENT.model_copy(update={"event_id": "Ev125", "ts": "1700000000.000300"})
    third = await store.insert_mail(KEY, third_event, SteerPriority.ASAP, target_workflow_id="WF1")
    assert third is not None
    await store.claim_mail(KEY, token="WF1:step")

    assert [mail.event_id for mail in await store.mail_claimed_by("WF1")] == ["Ev123", "Ev125"]
    assert [mail.event_id for mail in await store.mail_claimed_by("WF12")] == ["Ev124"]
    assert await store.mail_claimed_by("unknown") == []


async def test_migrate_is_idempotent(store: PostgresStore) -> None:
    await store.migrate()
    await store.migrate()


# --- thread summaries ----------------------------------------------------------------

PUBLIC_VIEW = SummaryView(team="T01", channel="C09", include_public=True)


async def save(
    store: PostgresStore,
    text: str,
    *,
    team: str = "T01",
    channel: str = "C01",
    thread: str = "1.1",
    private: bool = False,
    covered: str = "1700000001.000100",
) -> int:
    key = SessionKey(team=team, channel=channel, thread_root=thread)
    assert await store.save_summary(key, private=private, text=text, covered_ts=covered)
    found = await store.thread_summary(key)
    assert found is not None
    return found.id


async def test_summary_save_and_read(store: PostgresStore) -> None:
    summary = await save(store, "Payments timed out.", thread="1700000000.000100")

    found = await store.get_summary(PUBLIC_VIEW, summary)
    assert found is not None
    assert (found.id, found.channel, found.thread_ts, found.text, found.covered_ts) == (
        summary,
        "C01",
        "1700000000.000100",
        "Payments timed out.",
        "1700000001.000100",
    )
    assert found.created_at == found.updated_at
    assert await store.get_summary(PUBLIC_VIEW, summary + 1) is None
    assert await store.thread_summary(SessionKey(team="T01", channel="C01", thread_root="2.2")) is (
        None
    )


async def test_summary_save_only_moves_forward(store: PostgresStore) -> None:
    key = SessionKey(team="T01", channel="C01", thread_root="1700000000.000100")
    await store._connected_pool.execute(
        """INSERT INTO thread_summaries (team, channel, thread_ts, private, text, covered_ts)
        VALUES ('T01', 'C01', '1700000000.000100', false, 'Old entry.', '')"""
    )

    assert await store.save_summary(
        key, private=False, text="Up to 2.", covered_ts="1700000002.000100"
    )
    assert await store.save_summary(
        key, private=True, text="Up to 10.", covered_ts="1700000010.000100"
    )
    assert not await store.save_summary(
        key, private=False, text="Late save.", covered_ts="1700000002.000100"
    )
    assert not await store.save_summary(
        key, private=False, text="Same message.", covered_ts="1700000010.000100"
    )

    found = await store.thread_summary(key)
    assert found is not None
    assert (found.text, found.covered_ts) == ("Up to 10.", "1700000010.000100")
    assert found.updated_at > found.created_at
    # The channel became private after the first summary.
    assert await store.get_summary(PUBLIC_VIEW, found.id) is None


async def test_a_newer_public_save_keeps_a_private_summary_private(store: PostgresStore) -> None:
    key = SessionKey(team="T01", channel="C01", thread_root="1.1")
    assert await store.save_summary(key, private=True, text="Private.", covered_ts="2.1")
    # A job that read the channel before it became private, but newer messages.
    assert await store.save_summary(key, private=False, text="Newer.", covered_ts="2.2")

    found = await store.thread_summary(key)
    assert found is not None
    assert found.text == "Newer."
    assert await store.get_summary(PUBLIC_VIEW, found.id) is None


async def test_summary_view_sets_which_summaries_a_conversation_sees(
    store: PostgresStore,
) -> None:
    public = await save(store, "outage one")
    private = await save(store, "outage two", channel="C02", private=True)
    other_team = await save(store, "outage", team="T02")
    summaries = {public, private, other_team}

    async def seen(view: SummaryView) -> set[int]:
        by_read = {summary for summary in summaries if await store.get_summary(view, summary)}
        found = await store.search_summaries(view, ["outage"], 10)
        assert {match.id for match in found.matches} == by_read
        return by_read

    assert await seen(SummaryView("T01", "C09", include_public=True)) == {public}
    assert await seen(SummaryView("T01", "C02", include_public=True)) == {public, private}
    assert await seen(SummaryView("T01", "C02", include_public=False)) == {private}
    assert await seen(SummaryView("T01", "C09", include_public=False)) == set()
    assert await seen(SummaryView("T02", "C09", include_public=True)) == {other_team}
    assert await seen(SummaryView("T01", None, include_public=True)) == {public}


async def test_summary_search_matches_any_word_and_ranks_by_matched_words(
    store: PostgresStore,
) -> None:
    three = await save(store, "Payments API timeouts after the deploy.", thread="1.1")
    # Many hits on two words do not beat one hit on each of three words.
    two = await save(store, " ".join(["timeout"] * 8 + ["payment"] * 8), thread="2.2")
    one = await save(store, "A deploy.", thread="3.3")
    await save(store, "Database migration.", thread="4.4")

    words = ["timeout", "payment", "deploy"]
    found = await store.search_summaries(PUBLIC_VIEW, words, 10)
    assert [match.id for match in found.matches] == [three, two, one]
    assert found.total == 3

    first = await store.search_summaries(PUBLIC_VIEW, words, 1)
    assert [match.id for match in first.matches] == [three]
    assert first.total == 3


async def test_summary_search_phrases_keep_word_order(store: PostgresStore) -> None:
    summary = await save(store, "The billing worker ran out of memory.")

    found = await store.search_summaries(PUBLIC_VIEW, ["billing worker"], 10)
    assert [match.id for match in found.matches] == [summary]
    assert (await store.search_summaries(PUBLIC_VIEW, ["worker billing"], 10)).matches == []


async def test_summary_search_for_stop_words_finds_nothing(store: PostgresStore) -> None:
    await save(store, "The deploy was slow.")

    found = await store.search_summaries(PUBLIC_VIEW, ["the", "was"], 10)
    assert (found.matches, found.total) == ([], 0)


async def test_summary_search_follows_the_newest_text(store: PostgresStore) -> None:
    summary = await save(store, "Cause: disk full.", covered="1700000001.000100")
    await save(store, "Cause: the pool.", covered="1700000002.000100")

    assert [m.id for m in (await store.search_summaries(PUBLIC_VIEW, ["pool"], 10)).matches] == [
        summary
    ]
    assert (await store.search_summaries(PUBLIC_VIEW, ["disk"], 10)).matches == []


async def test_summary_search_shows_the_text_around_the_match(store: PostgresStore) -> None:
    before = " ".join(f"before{n}" for n in range(100))
    after = " ".join(f"after{n}" for n in range(100))
    await save(store, f"{before} the pool ran dry. {after}")

    [match] = (await store.search_summaries(PUBLIC_VIEW, ["pool"], 10)).matches
    assert "the pool ran dry" in match.snippet
    assert "before0 " not in match.snippet
    assert "after99" not in match.snippet


def usage_row(**overrides: object) -> dict:
    defaults: dict[str, object] = {
        "team": "T01",
        "kind": "model_request",
        "provider": "anthropic",
        "resource": "claude-sonnet-5",
        "quantity_in": 100,
        "quantity_out": 10,
        "metadata": {"requests": 1},
        "channel": "C01",
        "thread_root": "1700000000.000100",
        "workflow_id": "WF1",
    }
    return {**defaults, **overrides}


async def test_record_usage_upserts_on_the_workflow_key(store: PostgresStore) -> None:
    """A replayed write with superset numbers converges — no duplicate row."""
    await store.record_usage([usage_row()])
    await store.record_usage(
        [usage_row(provider="vercel", quantity_in=300, quantity_out=30, metadata={"requests": 3})]
    )

    conn = await asyncpg.connect(store.dsn)
    try:
        rows = await conn.fetch("SELECT * FROM usage_events")
    finally:
        await conn.close()
    assert len(rows) == 1
    assert rows[0]["quantity_in"] == 300
    assert rows[0]["quantity_out"] == 30
    assert json.loads(rows[0]["metadata"]) == {"requests": 3}
    # Provider is a row attribute, not part of the key: the replayed write sets it.
    assert rows[0]["provider"] == "vercel"
    assert rows[0]["team"] == "T01"
    assert rows[0]["workflow_id"] == "WF1"
    assert rows[0]["created_at"] is not None


async def test_record_usage_keeps_distinct_resources_apart(store: PostgresStore) -> None:
    await store.record_usage(
        [
            usage_row(),
            usage_row(resource="claude-haiku-4-5", quantity_in=5, quantity_out=1),
            usage_row(workflow_id="WF2"),
        ]
    )

    conn = await asyncpg.connect(store.dsn)
    try:
        count = await conn.fetchval("SELECT count(*) FROM usage_events")
    finally:
        await conn.close()
    assert count == 3
