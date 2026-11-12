from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
from dbos import DBOS
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.messages import ModelRequest
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

import hondo.deployment as deployment
from hondo.agent.skills import SKILL_RULES, SkillsCapability
from hondo.agent_settings import AgentModel
from hondo.api.store import ControlStore
from hondo.chat import ChannelInfo
from hondo.config import Config
from hondo.context import RunScope, reset_scope, set_scope
from hondo.models import Event, EventKind, SessionKey
from hondo.runtime.model_selection import RunModel, RuntimeModels
from hondo.skills import SkillError, SkillHeader, build_skill, parse_skill
from hondo.store.base import SkillSummary
from hondo.store.postgres import PostgresStore
from tests.fakes import FakeSlack, FakeStore
from tests.test_auth import APP_URL, VERIFIED_JANE, FakeOIDCProvider, _app, _client
from tests.test_workspaces import _sign_in_as_admin

ROLLBACK = """\
---
name: deploy-rollback
description: Roll back a bad deploy of the payments service.
license: Apache-2.0
metadata:
  owner: platform
---

# Roll back

1. Run `deploy rollback payments`.
2. Check the error rate.
"""

TRIAGE = """\
---
name: triage
description: Sort new bug reports by severity.
---

Ask for the steps to reproduce.
"""

# Put in refused content to show that no error repeats the content.
MARKER = "do-not-echo-7d1f"
NOT_IN_THREAD = (
    "Not saved: you can write a draft skill only in a channel thread, when someone asks for it."
)


# --- SKILL.md ----------------------------------------------------------------------


def test_parse_reads_name_and_description_and_keeps_other_keys_out() -> None:
    assert parse_skill(ROLLBACK) == SkillHeader(
        name="deploy-rollback", description="Roll back a bad deploy of the payments service."
    )


def test_parse_accepts_windows_line_endings_and_strips_the_description() -> None:
    content = "---\r\nname: triage\r\ndescription: '  Sort new bugs.  '\r\n---\r\nSteps.\r\n"
    assert parse_skill(content) == SkillHeader(name="triage", description="Sort new bugs.")


def test_parse_reads_a_block_description() -> None:
    content = "---\nname: triage\ndescription: |\n  Sort new bugs.\n  Ask for steps.\n---\n"
    assert parse_skill(content).description == "Sort new bugs.\nAsk for steps."


@pytest.mark.parametrize(
    "content",
    [
        "name: triage\ndescription: Sort new bugs.\n",
        "\n---\nname: triage\ndescription: Sort new bugs.\n---\n",
        "# Triage\n---\nname: triage\ndescription: Sort new bugs.\n---\n",
        "---\nname: triage\ndescription: Sort new bugs.\n",
        "---\nname: [triage\ndescription: Sort new bugs.\n---\n",
        "---\n- name: triage\n---\n",
        "---\nSort new bugs.\n---\n",
        "---\n---\nSort new bugs.\n",
    ],
)
def test_parse_refuses_missing_or_broken_frontmatter(content: str) -> None:
    with pytest.raises(SkillError) as refused:
        parse_skill(content)
    assert refused.value.code == "invalid_skill_frontmatter"


@pytest.mark.parametrize(
    "line",
    [
        "",
        "name:",
        "name: [triage]",
        "name: Triage",
        "name: -triage",
        "name: triage-",
        "name: tri--age",
        "name: tri_age",
        "name: tri age",
        "name: " + "a" * 65,
    ],
)
def test_parse_refuses_a_bad_name(line: str) -> None:
    with pytest.raises(SkillError) as refused:
        parse_skill(f"---\n{line}\ndescription: Sort new bugs.\n---\n")
    assert refused.value.code == "invalid_skill_name"


@pytest.mark.parametrize(
    "name", ["a", "404-handler", "v2-triage-3", "a" * 64, "yes", "on", "42", "2024"]
)
def test_parse_accepts_names_within_the_rules(name: str) -> None:
    assert parse_skill(f"---\nname: {name}\ndescription: x\n---\n").name == name


@pytest.mark.parametrize(
    "line",
    [
        "",
        "description:",
        "description: ''",
        "description: '   '",
        "description: [Sort, bugs]",
        "description: " + "x" * 1025,
    ],
)
def test_parse_refuses_a_bad_description(line: str) -> None:
    with pytest.raises(SkillError) as refused:
        parse_skill(f"---\nname: triage\n{line}\n---\n")
    assert refused.value.code == "invalid_skill_description"


@pytest.mark.parametrize("value", ["2024", "yes", "null"])
def test_parse_reads_every_description_as_text(value: str) -> None:
    assert parse_skill(f"---\nname: triage\ndescription: {value}\n---\n").description == value


def test_parse_ignores_values_of_other_keys() -> None:
    content = "---\nname: triage\ndescription: Sort new bugs.\nupdated: 2026-02-30\n---\n"
    assert parse_skill(content) == SkillHeader(name="triage", description="Sort new bugs.")


def test_parse_refuses_an_escape_that_is_not_a_character() -> None:
    with pytest.raises(SkillError) as refused:
        parse_skill('---\nname: triage\ndescription: "\\U00110000"\n---\n')
    assert refused.value.code == "invalid_skill_frontmatter"


@pytest.mark.parametrize("escape", ["a\\0b", "\\ud800"])
def test_parse_refuses_a_description_that_cannot_be_saved(escape: str) -> None:
    with pytest.raises(SkillError) as refused:
        parse_skill(f'---\nname: triage\ndescription: "{escape}"\n---\n')
    assert refused.value.code == "invalid_skill_description"


def test_parse_refuses_frontmatter_nested_too_deep() -> None:
    content = "---\nname: triage\ndescription: x\nmetadata: " + "[" * 5000 + "]" * 5000
    with pytest.raises(SkillError) as refused:
        parse_skill(content + "\n---\n")
    assert refused.value.code == "invalid_skill_frontmatter"


def test_parse_accepts_a_description_of_1024_characters() -> None:
    content = "---\nname: triage\ndescription: " + "x" * 1024 + "\n---\n"
    assert parse_skill(content).description == "x" * 1024


def test_build_writes_frontmatter_that_parses_back() -> None:
    content = build_skill("deploy-rollback", "Use when: a deploy fails", "\n1. Roll back.\n\n")

    assert content == (
        "---\n"
        "name: deploy-rollback\n"
        "description: 'Use when: a deploy fails'\n"
        "---\n"
        "\n"
        "1. Roll back.\n"
    )
    assert parse_skill(content) == SkillHeader(
        name="deploy-rollback", description="Use when: a deploy fails"
    )


def test_build_refuses_what_parse_refuses() -> None:
    with pytest.raises(SkillError) as refused:
        build_skill("Deploy Rollback", "Roll back.", "Steps.")
    assert refused.value.code == "invalid_skill_name"
    with pytest.raises(SkillError) as refused:
        build_skill("deploy-rollback", "", "Steps.")
    assert refused.value.code == "invalid_skill_description"


# --- HTTP API --------------------------------------------------------------------


def _update(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "revision": 1,
        "content": ROLLBACK,
        "published": False,
        "all_channels": False,
        "channels": [],
    }
    return body | changes


async def test_admin_creates_lists_reads_updates_and_deletes_skills(
    control_store: ControlStore, pg_dsn: str
) -> None:
    async with _client(_app(control_store, pg_dsn, FakeOIDCProvider(VERIFIED_JANE))) as client:
        _, workspace = await _sign_in_as_admin(client, control_store)
        base = f"/api/workspaces/{workspace}/skills"

        created = await client.post(base, json={"content": ROLLBACK})
        assert created.status_code == 201
        skill = created.json()
        skill_id = skill.pop("id")
        assert str(UUID(skill_id)) == skill_id
        datetime.fromisoformat(skill.pop("updated_at"))
        assert skill == {
            "name": "deploy-rollback",
            "description": "Roll back a bad deploy of the payments service.",
            "published": False,
            "all_channels": False,
            "channels": [],
            "source_url": None,
            "revision": 1,
            "content": ROLLBACK,
        }
        assert (await client.post(base, json={"content": TRIAGE})).status_code == 201

        listed = await client.get(base)
        assert listed.status_code == 200
        first, second = listed.json()["skills"]
        assert first["id"] == skill_id
        assert {key: value for key, value in first.items() if key != "updated_at"} == {
            "id": skill_id,
            "name": "deploy-rollback",
            "description": "Roll back a bad deploy of the payments service.",
            "published": False,
            "all_channels": False,
            "channels": [],
            "source_url": None,
            "revision": 1,
        }
        assert second["name"] == "triage"

        path = f"{base}/{skill_id}"
        renamed = ROLLBACK.replace("name: deploy-rollback", "name: payments-rollback")
        updated = await client.put(
            path,
            json=_update(content=renamed, published=True, channels=["C02", "C01", "C02"]),
        )
        assert updated.status_code == 200
        body = updated.json()
        assert {key: value for key, value in body.items() if key != "updated_at"} == {
            "id": skill_id,
            "name": "payments-rollback",
            "description": "Roll back a bad deploy of the payments service.",
            "published": True,
            "all_channels": False,
            "channels": ["C01", "C02"],
            "source_url": None,
            "revision": 2,
            "content": renamed,
        }
        fetched = await client.get(path)
        assert fetched.status_code == 200
        assert fetched.json() == body
        assert [item["name"] for item in (await client.get(base)).json()["skills"]] == [
            "payments-rollback",
            "triage",
        ]

        deleted = await client.delete(path)
        assert deleted.status_code == 204
        assert deleted.content == b""
        for response in [
            await client.get(path),
            await client.put(path, json=_update(revision=2)),
            await client.delete(path),
        ]:
            assert response.status_code == 404
            assert response.json() == {"detail": "skill_not_found"}
        assert [item["name"] for item in (await client.get(base)).json()["skills"]] == ["triage"]


async def test_changes_that_conflict_are_refused(control_store: ControlStore, pg_dsn: str) -> None:
    async with _client(_app(control_store, pg_dsn, FakeOIDCProvider(VERIFIED_JANE))) as client:
        _, workspace = await _sign_in_as_admin(client, control_store)
        base = f"/api/workspaces/{workspace}/skills"
        path = f"{base}/{(await client.post(base, json={'content': ROLLBACK})).json()['id']}"
        await client.post(base, json={"content": TRIAGE})

        not_ready = await client.put(path, json=_update(published=True))
        assert not_ready.status_code == 409
        assert not_ready.json() == {"detail": "skill_not_ready"}
        everywhere = await client.put(path, json=_update(published=True, all_channels=True))
        assert everywhere.status_code == 200
        assert everywhere.json()["revision"] == 2

        stale = await client.put(path, json=_update(revision=1))
        assert stale.status_code == 409
        assert stale.json() == {"detail": "skill_changed"}

        taken = ROLLBACK.replace("name: deploy-rollback", "name: triage")
        for response in [
            await client.put(path, json=_update(revision=2, content=taken)),
            await client.post(base, json={"content": TRIAGE}),
        ]:
            assert response.status_code == 409
            assert response.json() == {"detail": "skill_name_taken"}

        bad_channel = await client.put(path, json=_update(revision=2, channels=["general"]))
        assert bad_channel.status_code == 422
        assert bad_channel.json() == {"detail": "invalid_request"}

        saved = (await client.get(path)).json()
        assert (saved["name"], saved["revision"], saved["published"]) == (
            "deploy-rollback",
            2,
            True,
        )


async def test_create_refuses_a_skill_over_the_limit(
    control_store: ControlStore, pg_dsn: str
) -> None:
    async with _client(_app(control_store, pg_dsn, FakeOIDCProvider(VERIFIED_JANE))) as client:
        _, workspace = await _sign_in_as_admin(client, control_store)
        await control_store._connected_pool.execute(
            """INSERT INTO skills (workspace_id, name, description, content)
            SELECT $1, 'skill-' || n, 'Old.', 'SKILL.md' FROM generate_series(1, 99) AS n""",
            workspace,
        )
        base = f"/api/workspaces/{workspace}/skills"

        assert (await client.post(base, json={"content": TRIAGE})).status_code == 201
        refused = await client.post(base, json={"content": ROLLBACK})

        assert refused.status_code == 409
        assert refused.json() == {"detail": "skill_limit"}
        assert len((await client.get(base)).json()["skills"]) == 100


@pytest.mark.parametrize(
    "content,detail",
    [
        (f"# {MARKER}\n", "invalid_skill_frontmatter"),
        (f"---\nname: [{MARKER}\n---\n", "invalid_skill_frontmatter"),
        (f"---\nname: Bad-{MARKER}\ndescription: Sort bugs.\n---\n", "invalid_skill_name"),
        (f"---\nname: triage\ndescription: '  '\n---\n{MARKER}\n", "invalid_skill_description"),
        ("---\nname: triage\ndescription: Sort bugs.\n---\n" + MARKER * 1300, "invalid_request"),
        ("", "invalid_request"),
        (f"---\nname: triage\ndescription: Sort bugs.\n---\n{MARKER}\x00\n", "invalid_request"),
        (f"---\nname: triage\ndescription: Sort bugs.\n---\n{MARKER}\ud800\n", "invalid_request"),
    ],
)
async def test_invalid_skill_files_are_refused_without_their_content(
    control_store: ControlStore, pg_dsn: str, content: str, detail: str
) -> None:
    async with _client(_app(control_store, pg_dsn, FakeOIDCProvider(VERIFIED_JANE))) as client:
        _, workspace = await _sign_in_as_admin(client, control_store)
        base = f"/api/workspaces/{workspace}/skills"
        path = f"{base}/{(await client.post(base, json={'content': TRIAGE})).json()['id']}"

        # json.dumps writes a lone surrogate as an escape, as a browser does.
        # httpx's own encoder cannot send one.
        headers = {"Content-Type": "application/json"}
        for response in [
            await client.post(base, content=json.dumps({"content": content}), headers=headers),
            await client.put(path, content=json.dumps(_update(content=content)), headers=headers),
        ]:
            assert response.status_code == 422
            assert response.json() == {"detail": detail}
            assert MARKER not in response.text

        assert [item["name"] for item in (await client.get(base)).json()["skills"]] == ["triage"]


async def test_only_admins_of_the_workspace_reach_its_skills(
    control_store: ControlStore, pg_dsn: str
) -> None:
    async with _client(_app(control_store, pg_dsn, FakeOIDCProvider(VERIFIED_JANE))) as client:
        signed_out = await client.get(f"/api/workspaces/{uuid4()}/skills")
        assert signed_out.status_code == 401
        _, workspace = await _sign_in_as_admin(client, control_store)
        base = f"/api/workspaces/{workspace}/skills"
        own = (await client.post(base, json={"content": TRIAGE})).json()["id"]
        other_workspace = await control_store._connected_pool.fetchval(
            """INSERT INTO workspaces (slack_team_id, name, status, bot_token_encrypted)
            VALUES ('T2', 'Other', 'active', 'ciphertext') RETURNING id"""
        )
        foreign = await control_store._connected_pool.fetchval(
            """INSERT INTO skills (workspace_id, name, description, content)
            VALUES ($1, 'triage', 'Sort bugs.', 'SKILL.md') RETURNING id""",
            other_workspace,
        )

        for response in [
            await client.get(f"{base}/{foreign}"),
            await client.put(f"{base}/{foreign}", json=_update()),
            await client.delete(f"{base}/{foreign}"),
        ]:
            assert response.status_code == 404
            assert response.json() == {"detail": "skill_not_found"}
        assert (
            await control_store._connected_pool.fetchval(
                "SELECT content FROM skills WHERE id = $1", foreign
            )
            == "SKILL.md"
        )
        other = await client.get(f"/api/workspaces/{other_workspace}/skills")
        assert other.status_code == 404
        assert other.json() == {"detail": "not_found"}

        await control_store._connected_pool.execute(
            "DELETE FROM workspace_admins WHERE workspace_id = $1", workspace
        )
        for response in [
            await client.get(base),
            await client.post(base, json={"content": ROLLBACK}),
            await client.get(f"{base}/{own}"),
            await client.put(f"{base}/{own}", json=_update()),
            await client.delete(f"{base}/{own}"),
        ]:
            assert response.status_code == 404
            assert response.json() == {"detail": "not_found"}


# --- runs --------------------------------------------------------------------------

TEAM = "T1"
LINK_BASE = "https://hondo.example"


def scope(
    channel: str = "C01",
    ts: str = "1700000000.000100",
    kind: EventKind = EventKind.APP_MENTION,
) -> RunScope:
    event = Event(
        event_id=f"Ev-{channel}-{ts}",
        kind=kind,
        team=TEAM,
        channel=channel,
        ts=ts,
        user="U01",
        text="<@UBOT> turn this into a runbook",
        mentions_bot=True,
        is_dm=channel.startswith("D"),
        bot_user="UBOT",
    )
    return RunScope(key=SessionKey.from_event(event), event=event)


@contextmanager
def running_in(
    channel: str = "C01",
    ts: str = "1700000000.000100",
    kind: EventKind = EventKind.APP_MENTION,
) -> Iterator[None]:
    token = set_scope(scope(channel, ts, kind))
    try:
        yield
    finally:
        reset_scope(token)


def run_with(*skills: SkillSummary) -> RunContext[RunModel]:
    return RunContext(deps=RunModel("test", skills=skills), model=TestModel(), usage=RunUsage())


async def insert_workspace(store: PostgresStore, team: str = TEAM) -> UUID:
    return await store._connected_pool.fetchval(
        """INSERT INTO workspaces (slack_team_id, name, status, bot_token_encrypted)
        VALUES ($1, 'Acme', 'active', 'ciphertext') RETURNING id""",
        team,
    )


async def add_skill(
    store: PostgresStore,
    workspace: UUID,
    name: str,
    *,
    published: bool,
    all_channels: bool = False,
    channels: tuple[str, ...] = (),
) -> UUID:
    return await store._connected_pool.fetchval(
        """INSERT INTO skills (workspace_id, name, description, content, published,
            all_channels, channels)
        VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id""",
        workspace,
        name,
        f"Use for {name}.",
        f"---\nname: {name}\ndescription: Use for {name}.\n---\n\nSteps for {name}.\n",
        published,
        all_channels,
        list(channels),
    )


@pytest.fixture
def slack() -> FakeSlack:
    return FakeSlack()


@pytest.fixture
def skills(store: PostgresStore, slack: FakeSlack) -> SkillsCapability:
    return SkillsCapability(store, slack, LINK_BASE)


async def test_a_run_gets_the_published_skills_of_its_channel(
    store: PostgresStore, pg_dsn: str
) -> None:
    workspace = await insert_workspace(store)
    await add_skill(store, workspace, "everywhere", published=True, all_channels=True)
    await add_skill(store, workspace, "here", published=True, channels=("C01", "C03"))
    await add_skill(
        store, workspace, "shared-too", published=True, all_channels=True, channels=("C03",)
    )
    await add_skill(store, workspace, "elsewhere", published=True, channels=("C02",))
    await add_skill(store, workspace, "draft", published=False, all_channels=True)
    other = await insert_workspace(store, "T2")
    await add_skill(store, other, "other-team", published=True, all_channels=True)
    agents = FakeStore()
    agents.agent_models[TEAM] = AgentModel(uuid4(), workspace, "vercel", "openai/gpt-4.1")

    async def channel_info(channel: str) -> ChannelInfo:
        return ChannelInfo(
            is_channel=not channel.startswith("D"), is_private=False, is_ext_shared=channel == "C03"
        )

    models = RuntimeModels(
        agents,
        Config.model_validate(
            {"database": {"url": pg_dsn}, "workspace": {"slack": {"app_token": "xapp-test"}}}
        ),
        skill_store=store,
        channel_info=channel_info,
    )

    in_channel = await models.select(TEAM, "C01")
    in_shared_channel = await models.select(TEAM, "C03")
    in_dm = await models.select(TEAM, "D01")
    no_channel = await models.select(TEAM)

    assert in_channel is not None
    assert in_channel.skills == (
        SkillSummary(name="everywhere", description="Use for everywhere."),
        SkillSummary(name="here", description="Use for here."),
        SkillSummary(name="shared-too", description="Use for shared-too."),
    )
    # "All Slack channels" leaves out a channel shared with another organization.
    assert in_shared_channel is not None
    assert in_shared_channel.skills == (
        SkillSummary(name="here", description="Use for here."),
        SkillSummary(name="shared-too", description="Use for shared-too."),
    )
    assert (in_channel.shared_channel, in_shared_channel.shared_channel) == (False, True)
    assert in_dm is not None and in_dm.skills == ()
    assert no_channel is not None and no_channel.skills == ()


async def test_prompt_lists_the_run_skills_only_in_channel_threads(
    skills: SkillsCapability,
) -> None:
    agent = Agent(TestModel(call_tools=[]), deps_type=RunModel, capabilities=[skills])
    deps = RunModel(
        "test",
        skills=(
            SkillSummary("deploy-rollback", "Roll back a bad deploy.\n  Use it for payments."),
            SkillSummary("triage", "Sort new bugs."),
        ),
    )

    async def instructions(channel: str, kind: EventKind = EventKind.APP_MENTION) -> str:
        with running_in(channel, kind=kind):
            result = await agent.run("hello", deps=deps)
        request = result.all_messages()[0]
        assert isinstance(request, ModelRequest) and request.instructions is not None
        return request.instructions

    in_thread = await instructions("C01")
    assert in_thread.startswith(SKILL_RULES.strip())
    assert in_thread.endswith(
        "<available_skills>\n"
        "- deploy-rollback: Roll back a bad deploy. Use it for payments.\n"
        "- triage: Sort new bugs.\n"
        "</available_skills>"
    )
    assert await instructions("D01") == SKILL_RULES.strip()


async def test_skill_rules_come_after_summaries_and_before_the_session() -> None:
    config = Config.model_validate(
        {
            "database": {"url": "postgresql://test:test@localhost/test"},
            "workspace": {"slack": {"app_token": "xapp-test"}},
        }
    )
    store = PostgresStore(config.database.url.get_secret_value())
    try:
        app = deployment.build(config, bot_token="xoxb-test", store=store)
        with app.agent.override(model=TestModel(call_tools=[])), running_in():
            result = await app.agent.run(
                "hello", deps=RunModel("test", skills=(SkillSummary("triage", "Sort bugs."),))
            )
    finally:
        DBOS.destroy(destroy_registry=True)

    request = result.all_messages()[0]
    assert isinstance(request, ModelRequest) and request.instructions is not None
    order = [
        request.instructions.index(tag)
        for tag in ("<thread_summaries>", "<skills>", "<available_skills>", "<current_session>")
    ]
    assert order == sorted(order)


async def test_skill_read_gives_a_listed_skill_while_it_is_published_here(
    skills: SkillsCapability, store: PostgresStore
) -> None:
    workspace = await insert_workspace(store)
    await add_skill(store, workspace, "here", published=True, channels=("C01",))
    await add_skill(store, workspace, "draft", published=False, all_channels=True)
    await add_skill(store, workspace, "elsewhere", published=True, channels=("C02",))
    await add_skill(store, workspace, "unlisted", published=True, all_channels=True)
    # The run's list as it was at the start of the run.
    run = run_with(
        SkillSummary("here", "Use for here."),
        SkillSummary("draft", "Use for draft."),
        SkillSummary("elsewhere", "Use for elsewhere."),
    )

    with running_in("C01"):
        assert await skills._skill_read(run, "here") == (
            "---\nname: here\ndescription: Use for here.\n---\n\nSteps for here.\n"
        )
        for name in ("draft", "elsewhere", "unlisted", "missing"):
            assert await skills._skill_read(run, name) == (
                f'The skill "{name}" is not available in this conversation.'
            )
    with running_in("D01", kind=EventKind.MESSAGE):
        assert await skills._skill_read(run, "here") == (
            'The skill "here" is not available in this conversation.'
        )

    await store._connected_pool.execute("UPDATE skills SET published = false WHERE name = 'here'")
    with running_in("C01"):
        assert await skills._skill_read(run, "here") == (
            'The skill "here" is not available in this conversation.'
        )


async def test_skill_read_in_a_shared_channel_needs_the_channel_by_name(
    skills: SkillsCapability, store: PostgresStore
) -> None:
    workspace = await insert_workspace(store)
    await add_skill(store, workspace, "named", published=True, channels=("C04",))
    await add_skill(store, workspace, "moved", published=True, channels=("C04",))
    run = RunContext(
        deps=RunModel(
            "test",
            skills=(
                SkillSummary("named", "Use for named."),
                SkillSummary("moved", "Use for moved."),
            ),
            shared_channel=True,
        ),
        model=TestModel(),
        usage=RunUsage(),
    )
    # During the run, an admin moves one skill to "All Slack channels".
    await store._connected_pool.execute(
        "UPDATE skills SET all_channels = true, channels = '{}' WHERE name = 'moved'"
    )

    with running_in("C04"):
        assert await skills._skill_read(run, "named") == (
            "---\nname: named\ndescription: Use for named.\n---\n\nSteps for named.\n"
        )
        assert await skills._skill_read(run, "moved") == (
            'The skill "moved" is not available in this conversation.'
        )


async def _drafts(store: PostgresStore) -> list[dict[str, Any]]:
    rows = await store._connected_pool.fetch(
        """SELECT id, name, description, content, published, all_channels, channels,
            source_channel, source_ts, revision
        FROM skills ORDER BY created_at, name"""
    )
    return [dict(row) for row in rows]


async def test_skill_draft_saves_and_replaces_the_thread_draft(
    skills: SkillsCapability, store: PostgresStore
) -> None:
    workspace = await insert_workspace(store)

    with running_in("C01", "1700000000.000100"):
        saved = await skills._skill_draft(
            " deploy-rollback ", "Roll back a bad deploy.", "1. Run the rollback.\n"
        )
    [draft] = await _drafts(store)
    link = f"https://hondo.example/workspaces/{workspace}/skills/{draft['id']}"
    assert saved == (
        'Saved the draft skill "deploy-rollback". It is not active until an admin '
        f"publishes it on the Skills page: {link}"
    )
    assert {key: value for key, value in draft.items() if key != "id"} == {
        "name": "deploy-rollback",
        "description": "Roll back a bad deploy.",
        "content": (
            "---\nname: deploy-rollback\ndescription: Roll back a bad deploy.\n---\n\n"
            "1. Run the rollback.\n"
        ),
        "published": False,
        "all_channels": False,
        "channels": ["C01"],
        "source_channel": "C01",
        "source_ts": "1700000000.000100",
        "revision": 1,
    }

    with running_in("C01", "1700000000.000100"):
        replaced = await skills._skill_draft(
            "payments-rollback", "Roll back payments.", "1. Run `rollback payments`."
        )
    [again] = await _drafts(store)
    assert replaced == (
        'Saved the draft skill "payments-rollback". It is not active until an admin '
        f"publishes it on the Skills page: {link}"
    )
    assert (again["id"], again["name"], again["content"], again["revision"]) == (
        draft["id"],
        "payments-rollback",
        "---\nname: payments-rollback\ndescription: Roll back payments.\n---\n\n"
        "1. Run `rollback payments`.\n",
        2,
    )

    with running_in("C01", "1700000500.000100"):
        other = await skills._skill_draft("triage", "Sort new bugs.", "Ask for steps.")
        taken = await skills._skill_draft("payments-rollback", "Roll back.", "Steps.")
    rows = await _drafts(store)
    assert other == (
        'Saved the draft skill "triage". It is not active until an admin publishes it on '
        f"the Skills page: https://hondo.example/workspaces/{workspace}/skills/{rows[1]['id']}"
    )
    assert taken == (
        'Not saved: another skill has the name "payments-rollback". Choose a different name.'
    )
    assert [(row["name"], row["revision"]) for row in rows] == [
        ("payments-rollback", 2),
        ("triage", 1),
    ]


async def test_skill_draft_does_not_change_a_published_skill(
    skills: SkillsCapability, store: PostgresStore
) -> None:
    workspace = await insert_workspace(store)
    with running_in("C01"):
        await skills._skill_draft("triage", "Sort new bugs.", "Ask for steps.")
    [draft] = await _drafts(store)
    await store._connected_pool.execute("UPDATE skills SET published = true")

    with running_in("C01"):
        refused = await skills._skill_draft("triage", "Sort all bugs.", "Close them.")

    assert refused == (
        "Not saved: the skill from this thread is published. Only an admin can change it, "
        f"on the Skills page: https://hondo.example/workspaces/{workspace}/skills/{draft['id']}"
    )
    assert await _drafts(store) == [draft | {"published": True}]


async def test_a_draft_that_recovery_writes_again_keeps_admin_edits(
    store: PostgresStore,
) -> None:
    workspace = await insert_workspace(store)
    first = await store.save_thread_draft(TEAM, "C01", "1700000000.000100", TRIAGE, "run-1:4")
    edited = TRIAGE + "\n3. Close duplicates.\n"
    await store._connected_pool.execute(
        "UPDATE skills SET content = $1, revision = revision + 1", edited
    )

    replayed = await store.save_thread_draft(TEAM, "C01", "1700000000.000100", TRIAGE, "run-1:4")
    [kept] = await _drafts(store)
    later = await store.save_thread_draft(TEAM, "C01", "1700000000.000100", ROLLBACK, "run-2:4")
    [replaced] = await _drafts(store)

    assert first.workspace_id == workspace
    assert replayed == first
    assert (kept["content"], kept["revision"]) == (edited, 2)
    assert later == first
    assert (replaced["content"], replaced["revision"]) == (ROLLBACK, 3)


async def test_skill_draft_waits_for_an_admin_who_publishes_the_draft(
    skills: SkillsCapability, store: PostgresStore
) -> None:
    await insert_workspace(store)
    with running_in("C01"):
        await skills._skill_draft("triage", "Sort new bugs.", "Ask for steps.")
    [draft] = await _drafts(store)
    admin = await asyncpg.connect(store.dsn)
    try:
        publish = admin.transaction()
        await publish.start()
        await admin.execute("SELECT id FROM skills WHERE id = $1 FOR UPDATE", draft["id"])
        await admin.execute("UPDATE skills SET published = true WHERE id = $1", draft["id"])
        with running_in("C01"):
            redraft = asyncio.create_task(
                skills._skill_draft("triage", "Sort all bugs.", "Close them.")
            )
            await _wait_for_a_blocked_query(admin)
            await publish.commit()
            refused = await redraft
    finally:
        await admin.close()

    assert refused.startswith("Not saved: the skill from this thread is published.")
    assert await _drafts(store) == [draft | {"published": True}]


async def _wait_for_a_blocked_query(db: asyncpg.Connection) -> None:
    # Not through the store: its test pool has one connection, and the
    # blocked draft holds it.
    for _ in range(500):
        if await db.fetchval(
            """SELECT count(*) FROM pg_stat_activity
            WHERE datname = current_database() AND wait_event_type = 'Lock'"""
        ):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("no query waited for the admin's lock")


async def test_skill_draft_only_in_channel_threads(
    skills: SkillsCapability, store: PostgresStore, slack: FakeSlack
) -> None:
    await insert_workspace(store)
    slack.channels["G01"] = ChannelInfo(is_channel=False, is_private=True, is_ext_shared=False)
    slack.channels["C07"] = ChannelInfo(is_channel=True, is_private=False, is_ext_shared=True)

    with running_in("C07"):
        assert await skills._skill_draft("triage", "Sort bugs.", "Steps.") == (
            "Not saved: you cannot write a draft skill in a channel that is shared with "
            "another organization."
        )
    for channel, kind in [("D01", EventKind.MESSAGE), ("G01", EventKind.APP_MENTION)]:
        with running_in(channel, kind=kind):
            assert await skills._skill_draft("triage", "Sort bugs.", "Steps.") == NOT_IN_THREAD

    assert await _drafts(store) == []


async def test_skill_draft_asks_again_for_bad_arguments(
    skills: SkillsCapability, store: PostgresStore
) -> None:
    await insert_workspace(store)

    with running_in():
        with pytest.raises(ModelRetry, match="lowercase letters, digits, and single hyphens"):
            await skills._skill_draft("Deploy Rollback", "Roll back.", "Steps.")
        with pytest.raises(ModelRetry, match="1 to 1024 characters"):
            await skills._skill_draft("triage", " \n ", "Steps.")
        with pytest.raises(ModelRetry, match="1 to 1024 characters"):
            await skills._skill_draft("triage", "x" * 1025, "Steps.")
        with pytest.raises(ModelRetry, match="The instructions are empty."):
            await skills._skill_draft("triage", "Sort bugs.", " \n")
        with pytest.raises(ModelRetry, match="characters that cannot be saved"):
            await skills._skill_draft("triage", "Sort bugs.", "Step\x00 one.")
        with pytest.raises(ModelRetry, match="1 to 1024 characters"):
            await skills._skill_draft("triage", "Sort\x00 bugs.", "Steps.")
        with pytest.raises(ModelRetry, match="the limit is 20000"):
            await skills._skill_draft("triage", "Sort bugs.", "x" * 20_000)

    assert await _drafts(store) == []


async def test_skill_draft_keeps_the_workspace_limit(
    skills: SkillsCapability, store: PostgresStore
) -> None:
    workspace = await insert_workspace(store)
    await store._connected_pool.execute(
        """INSERT INTO skills (workspace_id, name, description, content)
        SELECT $1, 'skill-' || n, 'Old.', 'SKILL.md' FROM generate_series(1, 99) AS n""",
        workspace,
    )

    with running_in("C01", "1700000000.000100"):
        first = await skills._skill_draft("triage", "Sort bugs.", "Steps.")
        replaced = await skills._skill_draft("triage", "Sort new bugs.", "Steps.")
    with running_in("C01", "1700000500.000100"):
        refused = await skills._skill_draft("deploy-rollback", "Roll back.", "Steps.")

    assert first.startswith('Saved the draft skill "triage".')
    assert replaced.startswith('Saved the draft skill "triage".')
    assert refused == (
        "Not saved: the workspace has 100 skills, which is the limit. "
        "An admin can delete skills on the Skills page."
    )
    assert len(await _drafts(store)) == 100


async def test_dashboard_links_a_draft_to_its_thread(
    control_store: ControlStore, store: PostgresStore, pg_dsn: str
) -> None:
    async with _client(_app(control_store, pg_dsn, FakeOIDCProvider(VERIFIED_JANE))) as client:
        _, workspace = await _sign_in_as_admin(client, control_store)
        with running_in("C01", "1700000000.000100"):
            await SkillsCapability(store, FakeSlack(), APP_URL)._skill_draft(
                "triage", "Sort new bugs.", "Ask for steps."
            )

        listed = await client.get(f"/api/workspaces/{workspace}/skills")

    assert listed.status_code == 200
    [summary] = listed.json()["skills"]
    assert (summary["source_url"], summary["channels"], summary["published"]) == (
        "https://slack.com/archives/C01/p1700000000000100",
        ["C01"],
        False,
    )
