from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from hondo.mcp.catalog import BuiltinInput, load_catalog
from hondo.mcp.models import MCPError

RECORDS: dict[str, Any] = {
    "title": "Records",
    "description": "Find records of the team.",
    "url": "https://mcp.example/mcp",
    "headers": {"Authorization": "Bearer {token}", "X-Team": "{team}", "X-Mode": "full"},
    "inputs": {
        "token": {"label": "Access token", "secret": True, "help": "Make it in Settings."},
        "team": {"label": "Team"},
    },
    "setup": ["Make a token in Settings.", "Paste it below."],
}
RECORDS_SKILL = "---\nname: records-lookup\ndescription: Find one record.\n---\n\nCall lookup.\n"


def write(
    root: Path,
    builtin: str = "records",
    connector: dict[str, Any] | str = RECORDS,
    skill: str | None = RECORDS_SKILL,
) -> Path:
    folder = root / builtin
    folder.mkdir()
    text = connector if isinstance(connector, str) else yaml.safe_dump(connector, sort_keys=False)
    (folder / "connector.yaml").write_text(text)
    if skill is not None:
        (folder / "SKILL.md").write_text(skill)
    return root


def test_every_shipped_connector_loads() -> None:
    catalog = load_catalog()

    assert [(item.id, item.title) for item in catalog.connectors] == [
        ("atlassian", "Atlassian"),
        ("github", "GitHub"),
    ]
    atlassian, github = catalog.connectors
    assert github.url == "https://api.githubcopilot.com/mcp/"
    assert github.fill({"token": "github_pat_1"}) == {
        "Authorization": "Bearer github_pat_1",
        "X-MCP-Toolsets": "context,repos,issues,pull_requests,users,actions",
    }
    assert [(item.name, item.label, item.secret) for item in github.inputs] == [
        ("token", "Access token", True)
    ]
    assert [skill.name for skill in github.skills] == ["github-what-changed"]
    assert atlassian.url == "https://mcp.atlassian.com/v2/mcp"
    assert atlassian.fill({"token": "atl-1"}) == {"Authorization": "Bearer atl-1"}
    assert [(item.name, item.label, item.secret) for item in atlassian.inputs] == [
        ("token", "API token", True)
    ]
    assert [skill.name for skill in atlassian.skills] == ["atlassian-what-changed"]
    for connector in catalog.connectors:
        assert 1 <= len(connector.setup) <= 10
        assert all(skill.content.startswith("---\n") for skill in connector.skills)


def test_a_file_loads_into_the_catalog(tmp_path: Path) -> None:
    write(tmp_path)
    write(
        tmp_path,
        "alpha",
        RECORDS | {"title": "Zeta records"},
        RECORDS_SKILL.replace("records-lookup", "alpha-lookup"),
    )

    catalog = load_catalog(tmp_path)

    assert [item.id for item in catalog.connectors] == ["records", "alpha"]
    records = catalog.connectors[0]
    assert (records.title, records.description, records.url) == (
        "Records",
        "Find records of the team.",
        "https://mcp.example/mcp",
    )
    assert records.inputs == (
        BuiltinInput(name="token", label="Access token", secret=True, help="Make it in Settings."),
        BuiltinInput(name="team", label="Team", secret=False, help=None),
    )
    assert records.setup == ("Make a token in Settings.", "Paste it below.")
    assert records.fill({"token": "rk_1", "team": "ops"}) == {
        "Authorization": "Bearer rk_1",
        "X-Team": "ops",
        "X-Mode": "full",
    }
    skill = catalog.skill("records-lookup")
    assert skill is not None
    assert (skill.name, skill.description, skill.content, skill.builtin) == (
        "records-lookup",
        "Find one record.",
        RECORDS_SKILL,
        "records",
    )
    assert catalog.find("missing") is None and catalog.skill("missing") is None


@pytest.mark.parametrize(
    "inputs",
    [{}, {"token": "rk_1"}, {"token": "rk_1", "team": "ops", "region": "eu"}],
)
def test_filling_needs_exactly_the_declared_inputs(tmp_path: Path, inputs: dict[str, str]) -> None:
    [records] = load_catalog(write(tmp_path)).connectors

    with pytest.raises(MCPError) as refused:
        records.fill(inputs)

    assert (refused.value.code, refused.value.status) == ("invalid_request", 422)


def with_headers(headers: dict[str, str]) -> dict[str, Any]:
    return RECORDS | {"headers": headers}


@pytest.mark.parametrize(
    "connector,error",
    [
        (with_headers({"Authorization": "Bearer {tokn}", "X-Team": "{team}"}), "unknown inputs"),
        (with_headers({"Authorization": "Bearer {token}"}), "not used in a header"),
        (with_headers({"Authorization": "Bearer {token", "X-Team": "{team}"}), "brace"),
        (with_headers({"Authorization": "Bearer {Token}", "X-Team": "{team}"}), "brace"),
        (with_headers({"Host": "{token}", "X-Team": "{team}"}), "reserved"),
        (with_headers({"Mcp-Session-Id": "{token}", "X-Team": "{team}"}), "reserved"),
        (with_headers({"Authorization": "{token}", "authorization": "{team}"}), "unique"),
        (with_headers({"X Key": "{token}", "X-Team": "{team}"}), "headers"),
        (with_headers({"Authorization": "Bearer {token}\n", "X-Team": "{team}"}), "printable"),
        (RECORDS | {"url": "https://10.0.0.1/mcp"}, "public https"),
        (RECORDS | {"url": "https://localhost/mcp"}, "public https"),
        (RECORDS | {"url": "http://mcp.example/mcp"}, "public https"),
        (RECORDS | {"url": "https://{team}.mcp.example/mcp"}, "templates"),
        (RECORDS | {"setup": []}, "setup"),
        (RECORDS | {"description": "Two\nlines"}, "description"),
        (RECORDS | {"readonly": True}, "readonly"),
        ("title: [unclosed", "built-in connector records"),
    ],
)
def test_a_broken_connector_file_stops_the_load(
    tmp_path: Path, connector: dict[str, Any] | str, error: str
) -> None:
    write(tmp_path, connector=connector)

    with pytest.raises(ValueError, match="built-in connector records") as refused:
        load_catalog(tmp_path)

    assert error in str(refused.value)


@pytest.mark.parametrize(
    "builtin,skill,error",
    [
        ("records", RECORDS_SKILL.replace("records-lookup", "lookup"), 'start with "records-"'),
        ("records", RECORDS_SKILL.replace("records-lookup", "recordslookup"), "records-"),
        ("records", "name: records-lookup\n", "invalid_skill_frontmatter"),
        ("records", None, "SKILL.md"),
        ("Records", RECORDS_SKILL, "folder name"),
    ],
)
def test_a_broken_skill_or_folder_stops_the_load(
    tmp_path: Path, builtin: str, skill: str | None, error: str
) -> None:
    write(tmp_path, builtin, skill=skill)

    with pytest.raises(ValueError, match=f"built-in connector {builtin}") as refused:
        load_catalog(tmp_path)

    assert error in str(refused.value)
