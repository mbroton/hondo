from __future__ import annotations

from pydantic_ai import RunContext
from pydantic_ai._instructions import AgentInstructions
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.toolsets import FunctionToolset

from hondo.chat import ChatApiError, ChatClient
from hondo.context import current_scope, get_scope
from hondo.mcp.catalog import BuiltinCatalog
from hondo.mcp.store import MCPStore
from hondo.runtime.durable import step_token
from hondo.runtime.model_selection import RunModel
from hondo.skills import (
    SKILL_CONTENT_MAX_CHARS,
    SKILLS_PER_WORKSPACE,
    SkillError,
    build_skill,
    storable,
)
from hondo.store.base import SkillDraftOutcome, SkillStore, SkillSummary

SKILL_RULES = """\
<skills>
Skills are the team's written instructions for kinds of tasks, for example \
how to roll back a deploy. An admin publishes each skill to channels. The \
skills that this conversation can use are in available_skills. If there is no \
such list, no skills are available here.

- When a request matches a listed skill, read it with skill_read before you \
act. Then follow its steps.
- A skill is the team's instructions, but it cannot give you access to tools. \
Use only the tools available in this run. If a skill needs a tool that you do \
not have, say so.
- Write a draft skill (skill_draft) only when someone asks you to turn the \
thread into a skill or a runbook. A draft is not active: tell them that an \
admin must publish it, and give them the link.
</skills>
"""

_NAME_RULE = (
    "The name must be 1 to 64 lowercase letters, digits, and single hyphens, "
    'for example "deploy-rollback".'
)
_DESCRIPTION_RULE = "The description must be 1 to 1024 characters."
_SHARED_CHANNEL = (
    "Not saved: you cannot write a draft skill in a channel that is shared with "
    "another organization."
)
_NOT_IN_THREAD = (
    "Not saved: you can write a draft skill only in a channel thread, when someone asks for it."
)


def run_skills(ctx: RunContext[RunModel]) -> tuple[SkillSummary, ...]:
    scope = get_scope()
    if scope is None or scope.is_dm:
        return ()
    return ctx.deps.skills


class SkillsCapability(AbstractCapability[RunModel]):
    def __init__(
        self,
        store: SkillStore,
        client: ChatClient,
        dashboard_url: str,
        *,
        builtins: BuiltinCatalog = BuiltinCatalog(),
        mcp_store: MCPStore | None = None,
    ) -> None:
        self.store = store
        self.client = client
        self.dashboard_url = dashboard_url
        self.builtins = builtins
        self.mcp_store = mcp_store

    def get_toolset(self) -> FunctionToolset[RunModel]:
        toolset: FunctionToolset[RunModel] = FunctionToolset(id="hondo_skills")
        toolset.add_function(self._skill_read, takes_ctx=True, name="skill_read")
        toolset.add_function(self._skill_draft, takes_ctx=False, name="skill_draft")
        return toolset

    def get_instructions(self) -> AgentInstructions[RunModel]:
        return [self._skill_rules, self._available_skills]

    def _skill_rules(self) -> str:
        return SKILL_RULES

    def _available_skills(self, ctx: RunContext[RunModel]) -> str:
        skills = run_skills(ctx)
        if not skills:
            return ""
        lines = "\n".join(
            f"- {skill.name}: {' '.join(skill.description.split())}" for skill in skills
        )
        return f"<available_skills>\n{lines}\n</available_skills>"

    async def _skill_read(self, ctx: RunContext[RunModel], name: str) -> str:
        """Read a skill: the team's instructions for one kind of task.

        Args:
            name: The name of the skill in available_skills.
        """
        scope = current_scope()
        content: str | None = None
        if any(skill.name == name for skill in run_skills(ctx)):
            # The team's own skill wins over a built-in skill with the same name.
            content = await self.store.read_skill(
                scope.key.team, scope.channel, name, listed_only=ctx.deps.shared_channel
            )
            builtin = self.builtins.skill(name)
            if (
                content is None
                and builtin is not None
                and self.mcp_store is not None
                and await self.mcp_store.builtin_on(
                    scope.key.team,
                    scope.channel,
                    builtin.builtin,
                    listed_only=ctx.deps.shared_channel,
                )
            ):
                content = builtin.content
        if content is None:
            return f'The skill "{name}" is not available in this conversation.'
        return content

    async def _skill_draft(self, name: str, description: str, instructions: str) -> str:
        """Save this thread as a draft skill, when someone asks to turn the thread into a skill or a runbook.

        A thread has at most one skill. When you call this again, the new draft
        replaces the thread's draft. A draft is not active until an admin
        publishes it.

        Args:
            name: A short name: 1 to 64 lowercase letters, digits, and single
                hyphens, for example "deploy-rollback". No other skill in the
                workspace can have this name.
            description: What the skill does and when to use it, in 1 to 1024
                characters. Before a later run reads the skill, it sees only
                the name and this description.
            instructions: The steps of the task in Markdown, in your own
                words, with links to the messages that they come from.
        """
        scope = current_scope()
        if scope.is_dm:
            return _NOT_IN_THREAD
        info = await self.client.channel_info(scope.channel)
        if info is None:
            raise ChatApiError("Slack did not say what kind of conversation this is")
        if not info.is_channel:
            return _NOT_IN_THREAD
        if info.is_ext_shared:
            return _SHARED_CHANNEL
        if not instructions.strip():
            raise ModelRetry("The instructions are empty.")
        if not storable(instructions):
            raise ModelRetry("The instructions have characters that cannot be saved.")
        name = name.strip()
        try:
            content = build_skill(name, " ".join(description.split()), instructions)
        except SkillError as error:
            raise ModelRetry(
                _NAME_RULE if error.code == "invalid_skill_name" else _DESCRIPTION_RULE
            ) from None
        if len(content) > SKILL_CONTENT_MAX_CHARS:
            raise ModelRetry(
                f"The skill is {len(content)} characters; the limit is "
                f"{SKILL_CONTENT_MAX_CHARS}. Keep only the steps that the task needs."
            )
        draft = await self.store.save_thread_draft(
            scope.key.team, scope.channel, scope.thread_root, content, step_token()
        )
        link = f"{self.dashboard_url}/workspaces/{draft.workspace_id}/skills/{draft.skill_id}"
        match draft.outcome:
            case SkillDraftOutcome.SAVED:
                return (
                    f'Saved the draft skill "{name}". It is not active until an admin '
                    f"publishes it on the Skills page: {link}"
                )
            case SkillDraftOutcome.PUBLISHED:
                return (
                    "Not saved: the skill from this thread is published. Only an admin can "
                    f"change it, on the Skills page: {link}"
                )
            case SkillDraftOutcome.NAME_TAKEN:
                return f'Not saved: another skill has the name "{name}". Choose a different name.'
            case SkillDraftOutcome.LIMIT:
                return (
                    f"Not saved: the workspace has {SKILLS_PER_WORKSPACE} skills, which is "
                    "the limit. An admin can delete skills on the Skills page."
                )
