"""SKILL.md files in the Agent Skills format (https://agentskills.io/specification).

A skill is one file: YAML frontmatter, then Markdown instructions. Hondo reads
only `name` and `description` from the frontmatter and keeps the other keys in
the file. The API and the bot parse with the same rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

import yaml

SKILL_CONTENT_MAX_CHARS = 20_000
SKILL_NAME_MAX_CHARS = 64
SKILL_DESCRIPTION_MAX_CHARS = 1024
SKILLS_PER_WORKSPACE = 100

_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")

type SkillErrorCode = Literal[
    "skill_not_found",
    "skill_changed",
    "skill_name_taken",
    "skill_not_ready",
    "skill_limit",
    "invalid_skill_frontmatter",
    "invalid_skill_name",
    "invalid_skill_description",
]


class SkillError(Exception):
    def __init__(self, code: SkillErrorCode) -> None:
        self.code: SkillErrorCode = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SkillHeader:
    name: str
    description: str


def parse_skill(content: str) -> SkillHeader:
    lines = content.split("\n")
    markers = [line.rstrip() for line in lines]
    if markers[0] != "---" or "---" not in markers[1:]:
        raise SkillError("invalid_skill_frontmatter")
    end = markers.index("---", 1)
    try:
        # Plain strings only: typed YAML reads "name: yes" as a boolean and
        # fails on values that Hondo does not read, such as a bad date.
        frontmatter = yaml.load("\n".join(lines[1:end]), Loader=yaml.BaseLoader)
    except (yaml.YAMLError, ValueError, RecursionError):
        raise SkillError("invalid_skill_frontmatter") from None
    if not isinstance(frontmatter, dict):
        raise SkillError("invalid_skill_frontmatter")
    name = frontmatter.get("name")
    if not isinstance(name, str) or len(name) > SKILL_NAME_MAX_CHARS or not _NAME.fullmatch(name):
        raise SkillError("invalid_skill_name")
    description = frontmatter.get("description")
    if not isinstance(description, str):
        raise SkillError("invalid_skill_description")
    description = description.strip()
    if (
        not description
        or len(description) > SKILL_DESCRIPTION_MAX_CHARS
        or not storable(description)
    ):
        raise SkillError("invalid_skill_description")
    return SkillHeader(name=name, description=description)


def storable(text: str) -> bool:
    # Postgres text cannot hold NUL, and a lone surrogate is not UTF-8. JSON
    # and YAML escapes can make both.
    try:
        text.encode()
    except UnicodeEncodeError:
        return False
    return "\x00" not in text


def build_skill(name: str, description: str, instructions: str) -> str:
    """A SKILL.md with this frontmatter and body. Raises SkillError like parse_skill."""

    frontmatter = yaml.safe_dump(
        {"name": name, "description": description},
        sort_keys=False,
        allow_unicode=True,
        width=float("inf"),
    )
    content = f"---\n{frontmatter}---\n\n{instructions.strip()}\n"
    parse_skill(content)
    return content
