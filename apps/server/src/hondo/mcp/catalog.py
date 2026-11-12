"""Built-in connectors: the custom connector form filled in from a data file, plus a skill.

Each connector is a folder in hondo/mcp/builtins: connector.yaml and SKILL.md.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from typing import Annotated, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from hondo.mcp.auth import HEADER_NAME, RESERVED_HEADERS, printable_ascii
from hondo.mcp.models import MCPError
from hondo.mcp.network import public_url
from hondo.skills import SkillError, parse_skill

BUILTIN_ID = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
INPUT_NAME = r"^[a-z][a-z0-9_]*$"
_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")
_ONE_LINE = r"^[^\r\n]+$"


@dataclass(frozen=True, slots=True)
class BuiltinInput:
    name: str
    label: str
    secret: bool
    help: str | None


@dataclass(frozen=True, slots=True)
class BuiltinSkill:
    name: str
    description: str
    content: str
    builtin: str


@dataclass(frozen=True, slots=True)
class BuiltinConnector:
    id: str
    title: str
    description: str
    url: str
    headers: dict[str, str]
    inputs: tuple[BuiltinInput, ...]
    setup: tuple[str, ...]
    skills: tuple[BuiltinSkill, ...]

    def fill(self, inputs: Mapping[str, str]) -> dict[str, str]:
        """The request headers with the admin's input values in the templates."""
        if set(inputs) != {item.name for item in self.inputs}:
            raise MCPError("invalid_request", 422)
        return {
            name: _PLACEHOLDER.sub(lambda match: inputs[match[1]], template)
            for name, template in self.headers.items()
        }


@dataclass(frozen=True, slots=True)
class BuiltinCatalog:
    connectors: tuple[BuiltinConnector, ...] = ()

    def find(self, builtin: str | None) -> BuiltinConnector | None:
        return next((item for item in self.connectors if item.id == builtin), None)

    def skill(self, name: str) -> BuiltinSkill | None:
        return next(
            (skill for item in self.connectors for skill in item.skills if skill.name == name),
            None,
        )


class _InputFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=60, pattern=_ONE_LINE)
    secret: bool = False
    help: str | None = Field(default=None, min_length=1, max_length=200, pattern=_ONE_LINE)


class _ConnectorFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=100, pattern=_ONE_LINE)
    description: str = Field(min_length=1, max_length=200, pattern=_ONE_LINE)
    url: str = Field(min_length=1, max_length=2048)
    headers: dict[Annotated[str, Field(min_length=1, max_length=100, pattern=HEADER_NAME)], str] = (
        Field(min_length=1, max_length=10)
    )
    inputs: dict[Annotated[str, Field(max_length=40, pattern=INPUT_NAME)], _InputFile] = Field(
        default_factory=dict, max_length=10
    )
    setup: list[Annotated[str, Field(min_length=1, max_length=300, pattern=_ONE_LINE)]] = Field(
        min_length=1, max_length=10
    )

    @field_validator("url")
    @classmethod
    def public(cls, value: str) -> str:
        if "{" in value or "}" in value:
            raise ValueError("the url cannot have templates")
        try:
            return public_url(value)
        except MCPError:
            raise ValueError("the url must be a public https URL") from None

    @model_validator(mode="after")
    def headers_use_the_inputs(self) -> Self:
        if len({name.lower() for name in self.headers}) != len(self.headers):
            raise ValueError("header names must be unique without case")
        used: set[str] = set()
        for name, template in self.headers.items():
            if name.lower() in RESERVED_HEADERS:
                raise ValueError(f"header {name} is reserved")
            if not template.strip() or not printable_ascii(template):
                raise ValueError(f"header {name} must be printable ASCII")
            if {"{", "}"} & set(_PLACEHOLDER.sub("", template)):
                raise ValueError(f"header {name} has a brace that is not an {{input}}")
            names = set(_PLACEHOLDER.findall(template))
            if unknown := names - self.inputs.keys():
                raise ValueError(f"header {name} uses unknown inputs {sorted(unknown)}")
            used |= names
        if unused := self.inputs.keys() - used:
            raise ValueError(f"inputs {sorted(unused)} are not used in a header")
        return self


def _load(folder: Traversable) -> BuiltinConnector:
    builtin = folder.name
    try:
        if not re.fullmatch(BUILTIN_ID, builtin):
            raise ValueError("the folder name must be lowercase letters, digits and hyphens")
        data = _ConnectorFile.model_validate(
            yaml.safe_load(folder.joinpath("connector.yaml").read_text(encoding="utf-8"))
        )
        content = folder.joinpath("SKILL.md").read_text(encoding="utf-8")
        header = parse_skill(content)
        if not header.name.startswith(f"{builtin}-"):
            raise ValueError(f'the skill name must start with "{builtin}-"')
    except SkillError as exc:
        raise ValueError(f"built-in connector {builtin}: SKILL.md: {exc.code}") from exc
    except (OSError, yaml.YAMLError, ValueError) as exc:
        raise ValueError(f"built-in connector {builtin}: {exc}") from exc
    return BuiltinConnector(
        id=builtin,
        title=data.title,
        description=data.description,
        url=data.url,
        headers=dict(data.headers),
        inputs=tuple(
            BuiltinInput(name=name, label=item.label, secret=item.secret, help=item.help)
            for name, item in data.inputs.items()
        ),
        setup=tuple(data.setup),
        skills=(BuiltinSkill(header.name, header.description, content, builtin),),
    )


def load_catalog(root: Traversable | None = None) -> BuiltinCatalog:
    """Read and check every built-in connector. A broken file stops the process at start."""
    folder = root if root is not None else files("hondo") / "mcp" / "builtins"
    connectors = sorted(
        (_load(item) for item in folder.iterdir() if item.is_dir()),
        key=lambda item: (item.title.lower(), item.id),
    )
    names = [skill.name for item in connectors for skill in item.skills]
    if len(set(names)) != len(names):
        raise ValueError("built-in skill names must be unique")
    return BuiltinCatalog(tuple(connectors))
