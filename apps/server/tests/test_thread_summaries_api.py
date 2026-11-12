from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from hondo.api.store import ControlStore
from hondo.chat import ChannelInfo
from tests.test_auth import VERIFIED_JANE, FakeOIDCProvider, FakeSlackDirectory, _app, _client
from tests.test_workspaces import _sign_in_as_admin

PUBLIC = ChannelInfo(is_channel=True, is_private=False, is_ext_shared=False, name="incidents")
PRIVATE = ChannelInfo(is_channel=True, is_private=True, is_ext_shared=False, name="hr-cases")
SHARED = ChannelInfo(is_channel=True, is_private=False, is_ext_shared=True, name="acme-vendor")
SECRET = "salary review for Dana"
START = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
async def _empty_summaries(control_store: ControlStore) -> AsyncIterator[None]:
    await control_store._connected_pool.execute("TRUNCATE thread_summaries RESTART IDENTITY")
    yield


async def _summary(
    control_store: ControlStore,
    text: str,
    *,
    channel: str = "C01",
    thread_ts: str = "1700000000.000100",
    private: bool = False,
    team: str = "T1",
    minutes: int = 0,
) -> int:
    return await control_store._connected_pool.fetchval(
        """
        INSERT INTO thread_summaries (team, channel, thread_ts, private, text, covered_ts,
                                      updated_at)
        VALUES ($1, $2, $3, $4, $5, $3, $6)
        RETURNING id
        """,
        team,
        channel,
        thread_ts,
        private,
        text,
        START + timedelta(minutes=minutes),
    )


def _directory() -> FakeSlackDirectory:
    directory = FakeSlackDirectory()
    directory.channels = {"C01": PUBLIC, "C02": PRIVATE, "C03": PUBLIC, "C04": SHARED}
    return directory


async def test_admin_lists_summaries_newest_first_and_hides_private_ones(
    control_store: ControlStore, pg_dsn: str
) -> None:
    async with _client(
        _app(control_store, pg_dsn, FakeOIDCProvider(VERIFIED_JANE), directory=_directory())
    ) as client:
        _, workspace = await _sign_in_as_admin(client, control_store)
        older = await _summary(control_store, "Payments API returned 502 errors.", minutes=1)
        private = await _summary(
            control_store,
            SECRET,
            channel="C02",
            thread_ts="1700000100.000200",
            private=True,
            minutes=2,
        )
        became_private = await _summary(
            control_store,
            f"{SECRET} again",
            channel="C02",
            thread_ts="1700000200.000300",
            minutes=3,
        )
        shared = await _summary(
            control_store, f"{SECRET} with the vendor", channel="C04", minutes=4
        )
        unknown = await _summary(
            control_store, f"{SECRET} in a lost channel", channel="C99", minutes=5
        )
        newest = await _summary(
            control_store,
            "Deploy rollback of billing.",
            channel="C03",
            thread_ts="1700000300.000400",
            minutes=6,
        )
        await _summary(control_store, f"{SECRET} elsewhere", team="T2", minutes=7)

        response = await client.get(f"/api/workspaces/{workspace}/thread-summaries")

    assert response.status_code == 200
    assert SECRET not in response.text
    hidden = {"private": True, "channel": None, "url": None, "text": None}
    assert response.json() == {
        "summaries": [
            {
                "id": newest,
                "private": False,
                "channel": "incidents",
                "url": "https://slack.com/archives/C03/p1700000300000400",
                "text": "Deploy rollback of billing.",
                "updated_at": "2026-11-01T09:06:00Z",
            },
            {"id": unknown, **hidden, "updated_at": "2026-11-01T09:05:00Z"},
            {"id": shared, **hidden, "updated_at": "2026-11-01T09:04:00Z"},
            {"id": became_private, **hidden, "updated_at": "2026-11-01T09:03:00Z"},
            {"id": private, **hidden, "updated_at": "2026-11-01T09:02:00Z"},
            {
                "id": older,
                "private": False,
                "channel": "incidents",
                "url": "https://slack.com/archives/C01/p1700000000000100",
                "text": "Payments API returned 502 errors.",
                "updated_at": "2026-11-01T09:01:00Z",
            },
        ],
        "next_offset": None,
    }


async def test_summaries_come_in_pages_of_25(control_store: ControlStore, pg_dsn: str) -> None:
    async with _client(
        _app(control_store, pg_dsn, FakeOIDCProvider(VERIFIED_JANE), directory=_directory())
    ) as client:
        _, workspace = await _sign_in_as_admin(client, control_store)
        for minute in range(27):
            await _summary(
                control_store,
                f"Thread number {minute}.",
                thread_ts=f"17000000{minute:02}.0001",
                minutes=minute,
            )
        base = f"/api/workspaces/{workspace}/thread-summaries"

        first = await client.get(base)
        second = await client.get(base, params={"offset": 25})

    assert first.status_code == 200
    assert [item["text"] for item in first.json()["summaries"]] == [
        f"Thread number {minute}." for minute in range(26, 1, -1)
    ]
    assert first.json()["next_offset"] == 25
    assert second.status_code == 200
    assert [item["text"] for item in second.json()["summaries"]] == [
        "Thread number 1.",
        "Thread number 0.",
    ]
    assert second.json()["next_offset"] is None


async def test_search_finds_what_the_agent_would_find(
    control_store: ControlStore, pg_dsn: str
) -> None:
    async with _client(
        _app(control_store, pg_dsn, FakeOIDCProvider(VERIFIED_JANE), directory=_directory())
    ) as client:
        _, workspace = await _sign_in_as_admin(client, control_store)
        timeouts = await _summary(control_store, "The checkout service had a timeout at noon.")
        both = await _summary(
            control_store,
            "Checkout timeouts again; the fix was a rollback.",
            channel="C03",
            minutes=1,
        )
        await _summary(
            control_store, "Lunch plans for Friday.", thread_ts="1700000500.000100", minutes=2
        )
        await _summary(
            control_store, f"{SECRET}: a timeout in payroll.", channel="C02", private=True
        )
        await _summary(control_store, f"{SECRET}: a timeout at the vendor.", channel="C04")
        base = f"/api/workspaces/{workspace}/thread-summaries"

        found = await client.get(base, params={"q": "  timeouts rollback ", "offset": 25})
        nothing = await client.get(base, params={"q": "kubernetes"})

    assert found.status_code == 200
    assert SECRET not in found.text
    assert [item["id"] for item in found.json()["summaries"]] == [both, timeouts]
    assert found.json()["summaries"][1] == {
        "id": timeouts,
        "private": False,
        "channel": "incidents",
        "url": "https://slack.com/archives/C01/p1700000000000100",
        "text": "The checkout service had a timeout at noon.",
        "updated_at": "2026-11-01T09:00:00Z",
    }
    assert found.json()["next_offset"] is None
    assert nothing.status_code == 200
    assert nothing.json() == {"summaries": [], "next_offset": None}


async def test_delete_removes_a_summary_from_the_list_and_from_search(
    control_store: ControlStore, pg_dsn: str
) -> None:
    async with _client(
        _app(control_store, pg_dsn, FakeOIDCProvider(VERIFIED_JANE), directory=_directory())
    ) as client:
        _, workspace = await _sign_in_as_admin(client, control_store)
        gone = await _summary(control_store, "The rollback of billing worked.")
        kept = await _summary(
            control_store, "A rollback of search is planned.", channel="C03", minutes=1
        )
        private = await _summary(control_store, SECRET, channel="C02", private=True, minutes=2)
        foreign = await _summary(control_store, "Rollback in another team.", team="T2")
        base = f"/api/workspaces/{workspace}/thread-summaries"

        deleted = await client.delete(f"{base}/{gone}")
        deleted_private = await client.delete(f"{base}/{private}")
        again = await client.delete(f"{base}/{gone}")
        other_team = await client.delete(f"{base}/{foreign}")
        searched = await client.get(base, params={"q": "rollback"})
        listed = await client.get(base)
        foreign_text = await control_store._connected_pool.fetchval(
            "SELECT text FROM thread_summaries WHERE id = $1", foreign
        )

    assert deleted.status_code == 204
    assert deleted.content == b""
    assert deleted_private.status_code == 204
    for response in (again, other_team):
        assert response.status_code == 404
        assert response.json() == {"detail": "summary_not_found"}
    assert [item["id"] for item in searched.json()["summaries"]] == [kept]
    assert [item["id"] for item in listed.json()["summaries"]] == [kept]
    assert foreign_text == "Rollback in another team."


async def test_only_admins_of_the_workspace_reach_its_summaries(
    control_store: ControlStore, pg_dsn: str
) -> None:
    async with _client(
        _app(control_store, pg_dsn, FakeOIDCProvider(VERIFIED_JANE), directory=_directory())
    ) as client:
        signed_out = await client.get(f"/api/workspaces/{uuid4()}/thread-summaries")
        _, workspace = await _sign_in_as_admin(client, control_store)
        summary = await _summary(control_store, "Payments API returned 502 errors.")
        base = f"/api/workspaces/{workspace}/thread-summaries"
        other_workspace = await control_store._connected_pool.fetchval(
            """INSERT INTO workspaces (slack_team_id, name, status, bot_token_encrypted)
            VALUES ('T2', 'Other', 'active', 'ciphertext') RETURNING id"""
        )
        other = await client.get(f"/api/workspaces/{other_workspace}/thread-summaries")
        await control_store._connected_pool.execute(
            "DELETE FROM workspace_admins WHERE workspace_id = $1", workspace
        )
        listed = await client.get(base)
        searched = await client.get(base, params={"q": "payments"})
        deleted = await client.delete(f"{base}/{summary}")
        still_there = await control_store._connected_pool.fetchval(
            "SELECT count(*) FROM thread_summaries WHERE id = $1", summary
        )

    assert signed_out.status_code == 401
    for response in (other, listed, searched, deleted):
        assert response.status_code == 404
        assert response.json() == {"detail": "not_found"}
    assert still_there == 1


@pytest.mark.parametrize("path", ["0", "-1", "abc", str(2**63)])
async def test_delete_refuses_a_bad_summary_id(
    control_store: ControlStore, pg_dsn: str, path: str
) -> None:
    async with _client(
        _app(control_store, pg_dsn, FakeOIDCProvider(VERIFIED_JANE), directory=_directory())
    ) as client:
        _, workspace = await _sign_in_as_admin(client, control_store)
        response = await client.delete(f"/api/workspaces/{workspace}/thread-summaries/{path}")

    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_request"}
