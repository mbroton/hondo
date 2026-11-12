from __future__ import annotations

import asyncio
import re
import time
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from hondo.chat import (
    ChannelInfo,
    ChatApiError,
    ChatFile,
    ChatHistoryPage,
    ChatMessage,
    ChatUser,
)
from hondo.models import NO_REPLY

logger = structlog.get_logger("hondo.slack")

_DIRECT_MESSAGE = ChannelInfo(is_channel=False, is_private=True, is_ext_shared=False)

_FILES_URL = "https://files.slack.com/"
_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
# Anthropic refuses an image over 5 MB of base64, which is 3.75 MB of data.
_IMAGE_BYTES = 3_750_000
_IMAGE_CACHE_BYTES = 64_000_000
_IMAGE_TTL = 3600.0
_IMAGE_FAILURE_TTL = 60.0

SLACK_WRITING_RULES = f"""\
<slack_conventions>
You work in Slack. You are chatting, not writing documents.

- Messages you receive look like "[2026-07-10 11:45 UTC] John Doe: text": time, \
speaker, message. An @Name inside the text is a Slack mention. Your own @name and \
user id are stated above.
- How your turn works: a response with no tool calls is your FINAL answer and ends \
your turn. You can't say something now and keep working after it. Text you write \
alongside tool calls is posted to the thread as its own message while the work \
continues.
- RESPOND IMMEDIATELY, ALWAYS: before you start working, acknowledge. When the \
request needs tool calls or research, put one short line saying you're on it \
("on it, checking the staging configs") in the SAME response as your first tool \
calls. Never send that line as a text-only response, because that ends your turn \
with the work not done. If you can answer directly with no tool work, the answer \
itself is the immediate response.
- After that opening line, no play-by-play: your task list card already shows \
progress. Speak up mid-run only when you find something worth sharing, hit a \
blocker, or need input. Never announce what you're about to do next.
- Write like a coworker in chat, not a bot: lead with the answer, use \
contractions, keep the register casual. No headers, no sections, no essay structure.
- Keep it short. Most replies are 1-3 sentences or a few short bullets, under about \
80 words. Give the result, not the process: leave out what you tried unless someone \
asks or it changes the answer. Leave out side notes nobody asked for. Don't end with \
an offer ("want me to...?") unless one next step clearly matters. A long \
investigation still opens with the verdict in one line. If a reply needs scrolling, \
cut it down.
- Punctuation like a person typing: commas and periods do the work. Never use em \
dashes (—) or en dashes (–) between words or clauses. Use a comma, a colon, \
parentheses, or a new sentence instead. No semicolons either, and never open with \
"Certainly!" or "Great question!".
- Slack mrkdwn is NOT Markdown. Bold is *single asterisks*. Double asterisks like \
**this** do not render and show up as literal asterisks. Italic is _underscores_. \
`code` and ```code blocks``` work. Links are <URL|link text>, never [text](url). \
Markdown headers (#) and tables do not render, so never use them.
- To mention someone in YOUR replies, write <@USER_ID>. A plain @Name doesn't \
notify anyone. Ids for people here are in context, and lookup_user resolves an id \
you can't place. Channels are <#CHANNEL_ID>.
- You reply inside a thread, so don't repeat the question or greet on every message.
- When a new reply changes the task mid-run, acknowledge the change the same way as the \
opening ack: one line alongside your next tool calls ("got it, checking the database \
first").
- A message that mentions you (your @name appears in it) or arrives in a DM is \
always for you: answer it, even if it's just a greeting. Never call {NO_REPLY} \
for it.
- A message that does NOT mention you needs judgment. People drop the @ once a \
conversation is going, so many of these are still for you. Judge from content. A \
follow-up to something you said, a question about your work, or anything aimed at \
"you" gets a normal reply. Stay out only when teammates are clearly talking among \
themselves about something that isn't you or your work in this thread. These \
messages are exempt from the respond-immediately rule: decide in silence first. \
Mid-run, an irrelevant one changes nothing, so don't acknowledge it and keep working. \
When such a message is the one you're asked to respond to and it isn't for you, \
call {NO_REPLY}. Never write that you stay out: all text you write is posted.
- Use read_thread when you need context that might be missing from the conversation so \
far. Reactions (add_reaction) are for when someone asks for one, not for acknowledging \
requests.
</slack_conventions>"""


_CODE = re.compile(r"(```.*?```|`[^`\n]+`)", re.DOTALL)
_MARKDOWN_LINK = re.compile(
    r"!?\[([^\[\]\n]+)\]\(\s*<?((?:https?://|mailto:)[^\s<>()]+(?:\([^\s<>()]*\)[^\s<>()]*)*)>?\s*\)"
)

_MARKDOWN_BOLD = re.compile(r"\*\*(?=\S)([^\n]*?\S)\*\*")


def _slack_link(match: re.Match[str]) -> str:
    label, url = match.group(1).strip(), match.group(2)
    if label == url:
        return f"<{url}>"
    return f"<{url}|{label.replace('<', '&lt;').replace('>', '&gt;')}>"


def slack_mrkdwn(text: str) -> str:
    """Rewrite Markdown links ([text](url)) as Slack links (<url|text>), and
    Markdown bold (**text**) as Slack bold (*text*).

    Models write Markdown even when the rules say not to, and Slack shows it
    as raw text. Code keeps its text as written.
    """
    return "".join(
        part if index % 2 else _MARKDOWN_BOLD.sub(r"*\1*", _MARKDOWN_LINK.sub(_slack_link, part))
        for index, part in enumerate(_CODE.split(text))
    )


class SlackStyle:
    @property
    def writing_rules(self) -> str:
        return SLACK_WRITING_RULES

    def mention(self, user_id: str) -> str:
        return f"<@{user_id}>"

    def channel_mention(self, channel_id: str) -> str:
        return f"<#{channel_id}>"

    def message_link(self, channel: str, ts: str) -> str:
        # Domain-less archive link; Slack resolves it to the right workspace on open.
        return f"https://slack.com/archives/{channel}/p{ts.replace('.', '')}"

    def format_time(self, ts: str) -> str:
        """Render a Slack epoch timestamp ("1700000000.000300") as a UTC wall time.

        Malformed input yields "" and the caller skips the stamp — it is cosmetic
        and must never be the reason a run dies.
        """
        try:
            moment = datetime.fromtimestamp(float(ts), tz=UTC)
        except (ValueError, OverflowError, OSError):
            return ""
        return moment.strftime("%Y-%m-%d %H:%M UTC")


class SlackChatClient:
    def __init__(self, web_client: Any, *, http: httpx.AsyncClient | None = None) -> None:
        self._web = web_client
        self._http = http if http is not None else httpx.AsyncClient(timeout=10)
        self._channel_infos: OrderedDict[str, tuple[float, ChannelInfo]] = OrderedDict()
        self._channel_lookups: dict[str, asyncio.Task[ChannelInfo | None]] = {}
        self._images: OrderedDict[str, tuple[float, tuple[bytes, str] | None]] = OrderedDict()
        self._image_bytes = 0

    @classmethod
    def from_token(cls, token: str) -> SlackChatClient:
        return cls(AsyncWebClient(token=token))

    async def channel_info(self, channel: str) -> ChannelInfo | None:
        if channel.startswith("D"):
            return _DIRECT_MESSAGE
        cached = self._channel_infos.get(channel)
        if cached is not None and cached[0] > time.monotonic():
            self._channel_infos.move_to_end(channel)
            return cached[1]
        task = self._channel_lookups.get(channel)
        if task is None:
            task = asyncio.create_task(self._load_channel_info(channel))
            self._channel_lookups[channel] = task
            task.add_done_callback(lambda _: self._channel_lookups.pop(channel, None))
        return await asyncio.shield(task)

    async def _load_channel_info(self, channel: str) -> ChannelInfo | None:
        try:
            response = await self._web.conversations_info(channel=channel)
        except Exception as exc:
            logger.warning(
                "conversation type lookup failed",
                error_type=type(exc).__name__,
                error_code=exc.response.get("error") if isinstance(exc, SlackApiError) else None,
            )
            return None
        conversation = response.get("channel")
        if not isinstance(conversation, dict):
            conversation = {}
        info = ChannelInfo(
            is_channel=(
                conversation.get("is_im") is False
                and conversation.get("is_mpim") is False
                and (conversation.get("is_channel") is True or conversation.get("is_group") is True)
            ),
            # Unknown counts as private and shared: these flags only ever
            # narrow who sees what.
            is_private=conversation.get("is_private") is not False,
            is_ext_shared=conversation.get("is_ext_shared") is not False,
            name=name if isinstance(name := conversation.get("name"), str) and name else None,
        )
        self._channel_infos[channel] = (time.monotonic() + 300, info)
        self._channel_infos.move_to_end(channel)
        if len(self._channel_infos) > 1024:
            self._channel_infos.popitem(last=False)
        return info

    async def image(self, url: str) -> tuple[bytes, str] | None:
        """A Slack file from memory or Slack. It is never written anywhere."""
        cached = self._images.get(url)
        if cached is not None and cached[0] > time.monotonic():
            self._images.move_to_end(url)
            return cached[1]
        image = await self._download_image(url)
        self._forget_image(url)
        size = len(image[0]) if image is not None else 0
        ttl = _IMAGE_TTL if image is not None else _IMAGE_FAILURE_TTL
        self._images[url] = (time.monotonic() + ttl, image)
        self._image_bytes += size
        while self._image_bytes > _IMAGE_CACHE_BYTES:
            self._forget_image(next(iter(self._images)))
        return image

    def _forget_image(self, url: str) -> None:
        entry = self._images.pop(url, None)
        if entry is not None and entry[1] is not None:
            self._image_bytes -= len(entry[1][0])

    async def _download_image(self, url: str) -> tuple[bytes, str] | None:
        if not url.startswith(_FILES_URL):
            return None
        try:
            response = await self._http.get(
                url,
                headers={"Authorization": f"Bearer {self._web.token}"},
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            logger.warning("slack image download failed", error_type=type(exc).__name__)
            return None
        media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if (
            response.status_code != 200
            or media_type not in _IMAGE_TYPES
            or len(response.content) > _IMAGE_BYTES
        ):
            logger.warning(
                "slack image unavailable",
                status=response.status_code,
                media_type=media_type,
                size=len(response.content),
            )
            return None
        return response.content, media_type

    async def post_message(
        self, *, channel: str, text: str, thread_ts: str | None = None
    ) -> str | None:
        response = await self._web.chat_postMessage(
            channel=channel,
            text=slack_mrkdwn(text),
            thread_ts=thread_ts,
        )
        return response.get("ts")

    async def post_notice(
        self, *, channel: str, text: str, thread_ts: str | None = None
    ) -> str | None:
        response = await self._web.chat_postMessage(
            channel=channel,
            text=text,
            thread_ts=thread_ts,
            blocks=[{"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}],
        )
        return response.get("ts")

    async def update_message(self, *, channel: str, message_ref: str, text: str) -> None:
        try:
            await self._web.chat_update(channel=channel, ts=message_ref, text=text)
        except SlackApiError as exc:
            raise ChatApiError(exc.response.get("error", str(exc))) from exc

    async def post_approval(
        self, *, channel: str, thread_ts: str, approval_id: str, text: str
    ) -> str | None:
        response = await self._web.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=text,
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": text}},
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "action_id": "approval_approve",
                            "text": {"type": "plain_text", "text": "Approve"},
                            "style": "primary",
                            "value": approval_id,
                        },
                        {
                            "type": "button",
                            "action_id": "approval_reject",
                            "text": {"type": "plain_text", "text": "Reject"},
                            "style": "danger",
                            "value": approval_id,
                        },
                    ],
                },
            ],
        )
        return response.get("ts")

    async def close_approval(
        self, *, channel: str, message_ref: str, text: str, status: str
    ) -> None:
        await self._web.chat_update(
            channel=channel,
            ts=message_ref,
            text=f"{text}\n{status}",
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": text}},
                {"type": "context", "elements": [{"type": "mrkdwn", "text": status}]},
            ],
        )

    async def is_member(self, *, channel: str, user_id: str) -> bool:
        cursor: str | None = None
        try:
            while True:
                response = await self._web.conversations_members(
                    channel=channel, limit=1000, **({"cursor": cursor} if cursor else {})
                )
                if user_id in (response.get("members") or []):
                    return True
                cursor = (response.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    return False
        except SlackApiError as exc:
            logger.warning("channel member lookup failed", error=exc.response.get("error"))
            return False

    async def add_reaction(self, *, channel: str, message_ref: str, name: str) -> None:
        try:
            await self._web.reactions_add(
                channel=channel,
                name=name,
                timestamp=message_ref,
            )
        except SlackApiError as exc:
            raise ChatApiError(exc.response.get("error", str(exc))) from exc

    async def set_status(self, *, channel: str, thread_ts: str, text: str) -> None:
        try:
            await self._web.assistant_threads_setStatus(
                channel_id=channel, thread_ts=thread_ts, status=text
            )
        except SlackApiError as exc:
            raise ChatApiError(exc.response.get("error", str(exc))) from exc

    async def fetch_thread(
        self, *, channel: str, root: str, limit: int | None = None, oldest: str | None = None
    ) -> list[ChatMessage]:
        messages: dict[str, dict[str, Any]] = {}
        cursor: str | None = None
        cursors: set[str] = set()
        while True:
            response = await self._web.conversations_replies(
                channel=channel,
                ts=root,
                limit=None if limit is None else limit - len(messages),
                oldest=oldest,
                **({"cursor": cursor} if cursor else {}),
            )
            # Each page can start with the root again.
            for message in response["messages"]:
                messages.setdefault(message.get("ts", ""), message)
            cursor = (response.get("response_metadata") or {}).get("next_cursor")
            # A cursor seen before would repeat the same pages forever.
            if not cursor or cursor in cursors or (limit is not None and len(messages) >= limit):
                return self._messages(list(messages.values())[:limit])
            cursors.add(cursor)

    async def fetch_history(
        self,
        *,
        channel: str,
        limit: int | None = None,
        oldest: str | None = None,
        latest: str | None = None,
    ) -> ChatHistoryPage:
        try:
            response = await self._web.conversations_history(
                channel=channel, limit=limit, oldest=oldest, latest=latest
            )
        except SlackApiError as exc:
            raise ChatApiError(exc.response.get("error", str(exc))) from exc
        messages = self._messages(response["messages"])
        # Slack pages newest-first; the contract is oldest-first. Slack ts are
        # fixed-width ("1752893100.123456"), so the string sort is chronological.
        messages.sort(key=lambda message: message.ref)
        return ChatHistoryPage(messages=messages, has_more=bool(response.get("has_more")))

    async def user_info(self, *, user_id: str) -> ChatUser:
        try:
            response = await self._web.users_info(user=user_id)
        except SlackApiError as exc:
            raise ChatApiError(exc.response.get("error", str(exc))) from exc
        user = response["user"]
        profile = user.get("profile") or {}
        name = str(
            profile.get("display_name") or user.get("real_name") or user.get("name") or ""
        ).strip()
        return ChatUser(
            id=user_id,
            name=name or user_id,
            real_name=str(user.get("real_name") or "").strip(),
            is_bot=bool(user.get("is_bot")),
        )

    @staticmethod
    def _messages(messages: list[dict[str, Any]]) -> list[ChatMessage]:
        return [
            ChatMessage(
                author=message.get("user") or message.get("bot_id") or "",
                text=_message_text(message),
                ref=message.get("ts", ""),
                reply_count=int(message.get("reply_count") or 0),
                files=message_files(message),
            )
            for message in messages
        ]


def message_files(message: dict[str, Any]) -> tuple[ChatFile, ...]:
    """The files on a Slack message. An image gets the URL of its original or,
    when the original is too large or of a type models do not read, of its
    largest thumbnail."""
    files: list[ChatFile] = []
    for file in message.get("files") or []:
        if isinstance(file, dict):
            name = str(file.get("name") or file.get("title") or "file")
            url, image_type = _image(file)
            files.append(ChatFile(name=name, image_url=url, image_type=image_type))
    return tuple(files)


def _image(file: dict[str, Any]) -> tuple[str | None, str | None]:
    mime_type = str(file.get("mimetype") or "")
    if not mime_type.startswith("image/"):
        return None, None
    if mime_type in _IMAGE_TYPES and int(file.get("size") or 0) <= _IMAGE_BYTES:
        url = file.get("url_private")
    else:
        sizes = [int(key[6:]) for key in file if re.fullmatch(r"thumb_\d+", key)]
        url = file.get(f"thumb_{max(sizes)}") if sizes else None
        mime_type = "image/png" if isinstance(url, str) and url.endswith(".png") else "image/jpeg"
    if not isinstance(url, str) or not url.startswith(_FILES_URL):
        return None, None
    return url, mime_type


def _message_text(message: dict[str, Any]) -> str:
    """The message text and its attachments, where alert bots put their content."""
    lines = [message.get("text") or ""]
    for attachment in message.get("attachments") or []:
        # A link preview repeats the linked page, not the message.
        if attachment.get("from_url"):
            continue
        parts = [attachment.get(name) for name in ("pretext", "title", "text")]
        parts += [
            ": ".join(str(value) for value in (field.get("title"), field.get("value")) if value)
            for field in attachment.get("fields") or []
        ]
        if not any(parts):
            parts = [attachment.get("fallback")]
        lines += [str(part) for part in parts if part]
    return "\n".join(line for line in lines if line)
