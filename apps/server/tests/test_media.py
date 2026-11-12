from __future__ import annotations

from typing import Any

from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import (
    BinaryContent,
    ImageUrl,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from hondo.media import MediaContent
from hondo.runtime.model_selection import RunModel

FILES_URL = "https://files.slack.com/files-pri/T01-F01/"


def image(name: str) -> ImageUrl:
    return ImageUrl(FILES_URL + name, media_type="image/png", identifier=name)


def prompt_items(messages: list[ModelMessage]) -> list[Any]:
    return [
        item
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart) and not isinstance(part.content, str)
        for item in part.content
    ]


class Loader:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def __call__(self, url: str) -> tuple[bytes, str] | None:
        self.urls.append(url)
        if url.endswith("gone.png"):
            return None
        return url.encode(), "image/png"


async def test_images_load_for_the_request_and_stay_urls_in_the_history() -> None:
    seen: list[list[Any]] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(prompt_items(messages))
        return ModelResponse(parts=[TextPart("a 503 alert")])

    loader = Loader()
    agent = Agent(FunctionModel(model), deps_type=RunModel, capabilities=[MediaContent(loader)])
    result = await agent.run(
        ["jane: what is this?", image("alert.png"), image("gone.png")],
        deps=RunModel("function:model"),
    )

    assert seen == [
        [
            "jane: what is this?",
            BinaryContent(
                data=(FILES_URL + "alert.png").encode(),
                media_type="image/png",
                identifier="alert.png",
            ),
            "[image: gone.png, could not be loaded]",
        ]
    ]
    assert prompt_items(result.all_messages()) == [
        "jane: what is this?",
        image("alert.png"),
        image("gone.png"),
    ]


async def test_a_model_that_cannot_read_images_gets_a_note() -> None:
    seen: list[list[Any]] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(prompt_items(messages))
        return ModelResponse(parts=[TextPart("I cannot read images.")])

    loader = Loader()
    agent = Agent(FunctionModel(model), deps_type=RunModel, capabilities=[MediaContent(loader)])
    await agent.run(
        ["jane: what is this?", image("alert.png")],
        deps=RunModel("function:model", reads_images=False),
    )

    assert seen == [["jane: what is this?", "[image: alert.png, this model cannot read images]"]]
    assert loader.urls == []


async def test_only_the_newest_twenty_images_go_to_the_model() -> None:
    seen: list[list[Any]] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(prompt_items(messages))
        return ModelResponse(parts=[TextPart("ok")])

    loader = Loader()
    agent = Agent(FunctionModel(model), deps_type=RunModel, capabilities=[MediaContent(loader)])
    names = [f"{index:02}.png" for index in range(22)]
    await agent.run(["jane: look", *map(image, names)], deps=RunModel("function:model"))

    items = seen[0]
    assert items[1:3] == [
        "[image: 00.png, older image not shown]",
        "[image: 01.png, older image not shown]",
    ]
    assert [item.identifier for item in items[3:]] == names[2:]
    assert all(isinstance(item, BinaryContent) for item in items[3:])
    assert sorted(loader.urls) == [FILES_URL + name for name in names[2:]]


async def test_a_refused_image_is_sent_again_as_a_note_for_the_rest_of_the_run() -> None:
    requests: list[list[Any]] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        items = prompt_items(messages)
        requests.append(items)
        if any(isinstance(item, BinaryContent) for item in items):
            raise ModelHTTPError(400, "model", {"error": "image input is not supported"})
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("check", {})])
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(FunctionModel(model), deps_type=RunModel, capabilities=[MediaContent(Loader())])

    @agent.tool_plain
    def check() -> str:
        return "checked"

    result = await agent.run(["jane: look", image("alert.png")], deps=RunModel("function:model"))

    note = "[image: alert.png, this model cannot read images]"
    assert result.output == "done"
    assert [any(isinstance(item, BinaryContent) for item in items) for items in requests] == [
        True,
        False,
        False,
    ]
    assert requests[1:] == [["jane: look", note], ["jane: look", note]]
