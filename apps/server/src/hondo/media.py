"""Images and other media for each model request.

Slack images stay URLs in the history. They load here, for one request at a
time, from the chat client's memory cache or from Slack.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

import structlog
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ModelHTTPError, RetryModelRequest
from pydantic_ai.messages import (
    BinaryContent,
    FileUrl,
    ImageUrl,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolReturnPart,
    UploadedFile,
    UserContent,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.profiles import ModelProfile

from hondo.runtime.model_selection import RunModel

logger = structlog.get_logger("hondo.media")

type ImageLoader = Callable[[str], Awaitable[tuple[bytes, str] | None]]

# The newest images go to the model. Anthropic accepts at most 100 images and
# 32 MB in one request.
IMAGE_LIMIT = 20
IMAGE_BYTES_LIMIT = 20_000_000

_IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")


def compatible_media(
    value: BinaryContent, profile: ModelProfile, *, reads_images: bool = True
) -> BinaryContent | str:
    mime_type = value.media_type
    if mime_type.startswith("text/") or mime_type == "application/json":
        try:
            return value.data.decode("utf-8")
        except UnicodeDecodeError:
            return "The media could not be read."
    if mime_type in _IMAGE_TYPES and not reads_images:
        return "This model cannot read images."
    if (
        mime_type in _IMAGE_TYPES
        or (
            mime_type == "application/pdf"
            and profile.get("openai_chat_supports_document_input", True)
        )
        or (mime_type in ("audio/wav", "audio/mpeg") and profile.get("supports_audio_input", False))
    ):
        return value
    return "This media format is not available to the current model."


def compatible_content(
    value: Any,
    profile: ModelProfile,
    *,
    allow_references: bool = False,
    reads_images: bool = True,
) -> Any:
    media: list[BinaryContent] = []

    def convert(item: Any, *, nested: bool) -> Any:
        if isinstance(item, (list, tuple)):
            return [convert(child, nested=True) for child in item]
        if isinstance(item, dict):
            return {key: convert(child, nested=True) for key, child in item.items()}
        # Tool data cannot grant access to URLs or files in the provider account.
        if isinstance(item, FileUrl) and not allow_references:
            return f"Media link: {item.url}"
        if isinstance(item, UploadedFile) and not allow_references:
            return f"Media file reference: {item.file_id}"
        if not isinstance(item, BinaryContent):
            return item
        adapted = compatible_media(item, profile, reads_images=reads_images)
        if nested and isinstance(adapted, BinaryContent):
            media.append(adapted)
            return f"See file {adapted.identifier}."
        return adapted

    result = (
        [convert(item, nested=False) for item in value]
        if isinstance(value, (list, tuple))
        else convert(value, nested=False)
    )
    if not media:
        return result
    return [*result, *media] if isinstance(result, list) else [result, *media]


def compatible_messages(
    messages: list[ModelMessage], profile: ModelProfile, *, reads_images: bool = True
) -> list[ModelMessage]:
    return [
        replace(
            message,
            parts=[
                replace(
                    part,
                    content=compatible_content(
                        part.content,
                        profile,
                        allow_references=isinstance(part, UserPromptPart),
                        reads_images=reads_images,
                    ),
                )
                if isinstance(part, (ToolReturnPart, UserPromptPart))
                else part
                for part in message.parts
            ],
        )
        if isinstance(message, ModelRequest)
        else message
        for message in messages
    ]


async def load_images(
    messages: list[ModelMessage], load: ImageLoader, *, reads_images: bool
) -> list[ModelMessage]:
    """Each image URL in a user prompt as the image, newest first, within the
    limits. Everything else is a text note."""
    loaded: dict[int, UserContent] = {}
    count = size = 0
    for item in reversed(
        [
            item
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, UserPromptPart) and not isinstance(part.content, str)
            for item in part.content
            if isinstance(item, ImageUrl)
        ]
    ):
        name = item.identifier
        if not reads_images:
            loaded[id(item)] = f"[image: {name}, this model cannot read images]"
            continue
        if count >= IMAGE_LIMIT:
            loaded[id(item)] = f"[image: {name}, older image not shown]"
            continue
        image = await load(item.url)
        if image is None:
            loaded[id(item)] = f"[image: {name}, could not be loaded]"
        elif size + len(image[0]) > IMAGE_BYTES_LIMIT:
            loaded[id(item)] = f"[image: {name}, older image not shown]"
        else:
            count += 1
            size += len(image[0])
            loaded[id(item)] = BinaryContent(data=image[0], media_type=image[1], identifier=name)
    if not loaded:
        return messages
    return [
        replace(
            message,
            parts=[
                replace(part, content=[loaded.get(id(item), item) for item in part.content])
                if isinstance(part, UserPromptPart) and not isinstance(part.content, str)
                else part
                for part in message.parts
            ],
        )
        if isinstance(message, ModelRequest)
        else message
        for message in messages
    ]


class MediaContent(AbstractCapability[RunModel]):
    def __init__(self, load: ImageLoader) -> None:
        self.load = load
        self.refused = False

    async def for_run(self, ctx: RunContext[RunModel]) -> MediaContent:
        return MediaContent(self.load)

    async def prepare_model_request(
        self, ctx: RunContext[RunModel], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        reads_images = not self.refused and (ctx.deps is None or ctx.deps.reads_images)
        messages = await load_images(request_context.messages, self.load, reads_images=reads_images)
        return replace(
            request_context,
            messages=compatible_messages(
                messages, request_context.model.profile, reads_images=reads_images
            ),
        )

    async def on_model_request_error(
        self,
        ctx: RunContext[RunModel],
        *,
        request_context: ModelRequestContext,
        error: Exception,
    ) -> ModelResponse:
        if (
            self.refused
            or not isinstance(error, ModelHTTPError)
            or error.status_code != 400
            or not _has_images(request_context)
        ):
            raise error
        # Model lists do not always say which models read images, so a refused
        # request is sent again with text notes, and so is the rest of the run.
        logger.info("model refused images; sending text notes")
        self.refused = True
        raise RetryModelRequest()


def _has_images(request_context: ModelRequestContext) -> bool:
    def images(content: Any) -> bool:
        if isinstance(content, (list, tuple)):
            return any(images(item) for item in content)
        if isinstance(content, dict):
            return any(images(item) for item in content.values())
        return isinstance(content, ImageUrl) or (
            isinstance(content, BinaryContent) and content.media_type in _IMAGE_TYPES
        )

    return any(
        images(part.content)
        for message in request_context.messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, (ToolReturnPart, UserPromptPart))
    )
