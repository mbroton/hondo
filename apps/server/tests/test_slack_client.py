from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from slack_sdk.errors import SlackApiError

from hondo.chat import ChannelInfo, ChatApiError, ChatFile, ChatMessage
from hondo.models import Event, EventKind
from hondo.slack import SlackChatClient, SlackStyle, message_files


def test_slack_style_formats_platform_prose_and_references() -> None:
    style = SlackStyle()
    event = Event(
        event_id="Ev01",
        kind=EventKind.MESSAGE,
        team="T01",
        channel="C01",
        ts="1700000000.000100",
    )

    assert style.mention("U01") == "<@U01>"
    assert style.channel_mention("C01") == "<#C01>"
    assert style.message_link("C01", event.ts) == "https://slack.com/archives/C01/p1700000000000100"
    assert style.format_time(event.ts) == "2023-11-14 22:13 UTC"
    assert "call no_reply" in style.writing_rules


PUBLIC = ChannelInfo(is_channel=True, is_private=False, is_ext_shared=False)
GROUP_DM = ChannelInfo(is_channel=False, is_private=True, is_ext_shared=False)


class StubWebClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.post_response: dict[str, Any] = {"ok": True, "ts": "1700000001.000100"}
        self.thread_response: dict[str, Any] = {"ok": True, "messages": []}
        self.user_response: dict[str, Any] = {"ok": True, "user": {}}
        self.channel_response: dict[str, Any] = {
            "ok": True,
            "channel": {
                "is_channel": True,
                "is_im": False,
                "is_mpim": False,
                "is_private": False,
                "is_ext_shared": False,
            },
        }
        self.channel_error: SlackApiError | None = None
        self.update_error: SlackApiError | None = None
        self.reaction_error: SlackApiError | None = None
        self.history_error: SlackApiError | None = None
        self.user_error: SlackApiError | None = None

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("chat_postMessage", kwargs))
        return self.post_response

    async def chat_update(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("chat_update", kwargs))
        if self.update_error is not None:
            raise self.update_error
        return {"ok": True}

    async def reactions_add(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("reactions_add", kwargs))
        if self.reaction_error is not None:
            raise self.reaction_error
        return {"ok": True}

    async def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("conversations_replies", kwargs))
        return self.thread_response

    async def conversations_history(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("conversations_history", kwargs))
        if self.history_error is not None:
            raise self.history_error
        return self.thread_response

    async def conversations_info(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("conversations_info", kwargs))
        await asyncio.sleep(0)
        if self.channel_error is not None:
            raise self.channel_error
        return self.channel_response

    async def users_info(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("users_info", kwargs))
        if self.user_error is not None:
            raise self.user_error
        return self.user_response


async def test_post_message_maps_arguments_and_returns_ref() -> None:
    web = StubWebClient()

    ref = await SlackChatClient(web).post_message(
        channel="C01", text="Hondo.", thread_ts="1700000000.000100"
    )

    assert ref == "1700000001.000100"
    assert web.calls == [
        (
            "chat_postMessage",
            {"channel": "C01", "text": "Hondo.", "thread_ts": "1700000000.000100"},
        )
    ]


@pytest.mark.parametrize(
    ("text", "posted"),
    [
        (
            "Same as the [earlier thread](<https://slack.com/archives/C01/p1700000000000100>).",
            "Same as the <https://slack.com/archives/C01/p1700000000000100|earlier thread>.",
        ),
        (
            "See [the docs](https://example.com/deploy).",
            "See <https://example.com/deploy|the docs>.",
        ),
        (
            "[Rollback](https://en.wikipedia.org/wiki/Rollback_(data_management))",
            "<https://en.wikipedia.org/wiki/Rollback_(data_management)|Rollback>",
        ),
        ("[https://example.com](https://example.com)", "<https://example.com>"),
        ("![chart](https://example.com/chart.png)", "<https://example.com/chart.png|chart>"),
        ("[a > b](https://example.com)", "<https://example.com|a &gt; b>"),
        ("Mail [ops](mailto:ops@example.com).", "Mail <mailto:ops@example.com|ops>."),
        (
            "Two: [a](https://a.example) and [b](https://b.example)",
            "Two: <https://a.example|a> and <https://b.example|b>",
        ),
        ("**Short version:** no runbook.", "*Short version:* no runbook."),
        ("A **bold** and **two words** here", "A *bold* and *two words* here"),
        ("**[the runbook](https://example.com/rb)**", "*<https://example.com/rb|the runbook>*"),
    ],
)
async def test_post_message_turns_markdown_into_slack_mrkdwn(text: str, posted: str) -> None:
    web = StubWebClient()

    await SlackChatClient(web).post_message(channel="C01", text=text)

    assert web.calls[0][1]["text"] == posted


@pytest.mark.parametrize(
    "text",
    [
        "Already a Slack link: <https://example.com|docs>",
        "Run `curl [x](https://example.com)` to test.",
        "```\n[x](https://example.com)\n```",
        "A [note] (not a link) and [x](relative/path)",
        "<@U01> check <#C01>",
        "Already *bold* in Slack",
        "Run `**kwargs` through it.",
        "2 ** 10 is 1024, and a ** b",
    ],
)
async def test_post_message_keeps_text_that_is_not_a_markdown_link(text: str) -> None:
    web = StubWebClient()

    await SlackChatClient(web).post_message(channel="C01", text=text)

    assert web.calls[0][1]["text"] == text


async def test_channel_lookup_is_shared_and_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1000.0
    monkeypatch.setattr("hondo.slack.time", SimpleNamespace(monotonic=lambda: now))
    web = StubWebClient()
    client = SlackChatClient(web)
    assert await asyncio.gather(*(client.channel_info("C01") for _ in range(8))) == [PUBLIC] * 8
    assert web.calls == [("conversations_info", {"channel": "C01"})]

    now += 301
    web.channel_response = {
        "ok": True,
        "channel": {"is_im": False, "is_mpim": True, "is_private": True, "is_ext_shared": False},
    }
    assert await client.channel_info("C01") == GROUP_DM
    assert web.calls == [("conversations_info", {"channel": "C01"})] * 2


async def test_channel_info_reads_privacy_and_sharing() -> None:
    web = StubWebClient()
    client = SlackChatClient(web)
    web.channel_response = {
        "ok": True,
        "channel": {
            "is_group": True,
            "is_im": False,
            "is_mpim": False,
            "is_private": True,
            "is_ext_shared": True,
            "name": "incidents",
        },
    }
    assert await client.channel_info("C01") == ChannelInfo(
        is_channel=True, is_private=True, is_ext_shared=True, name="incidents"
    )

    # Missing flags narrow who sees what.
    web.channel_response = {"ok": True, "channel": {"is_channel": True}}
    assert await client.channel_info("C02") == ChannelInfo(
        is_channel=False, is_private=True, is_ext_shared=True
    )


async def test_direct_message_needs_no_channel_lookup() -> None:
    web = StubWebClient()
    client = SlackChatClient(web)
    assert await client.channel_info("D01") == ChannelInfo(
        is_channel=False, is_private=True, is_ext_shared=False
    )
    assert web.calls == []


async def test_failed_channel_lookup_gives_no_info() -> None:
    web = StubWebClient()
    web.channel_error = SlackApiError("failed", {"ok": False, "error": "missing_scope"})
    assert await SlackChatClient(web).channel_info("C01") is None


async def test_failed_channel_lookup_does_not_delay_permission_recovery() -> None:
    web = StubWebClient()
    client = SlackChatClient(web)
    web.channel_error = SlackApiError("failed", {"ok": False, "error": "missing_scope"})
    assert await client.channel_info("C01") is None
    web.channel_error = None
    assert await client.channel_info("C01") == PUBLIC
    assert web.calls == [("conversations_info", {"channel": "C01"})] * 2


async def test_slow_channel_lookup_does_not_block_other_channels() -> None:
    started, release = asyncio.Event(), asyncio.Event()

    class SlowWeb(StubWebClient):
        async def conversations_info(self, **kwargs: Any) -> dict[str, Any]:
            if kwargs["channel"] == "C02":
                started.set()
                await release.wait()
            return await super().conversations_info(**kwargs)

    client = SlackChatClient(SlowWeb())
    assert await client.channel_info("C01") == PUBLIC
    slow = asyncio.create_task(client.channel_info("C02"))
    await started.wait()
    try:
        assert await asyncio.wait_for(client.channel_info("C01"), 1) == PUBLIC
        assert await asyncio.wait_for(client.channel_info("C03"), 1) == PUBLIC
    finally:
        release.set()
        assert await slow == PUBLIC


async def test_cancelled_channel_waiter_does_not_cancel_shared_lookup() -> None:
    started, release = asyncio.Event(), asyncio.Event()

    class SlowWeb(StubWebClient):
        async def conversations_info(self, **kwargs: Any) -> dict[str, Any]:
            started.set()
            await release.wait()
            return await super().conversations_info(**kwargs)

    web = SlowWeb()
    client = SlackChatClient(web)
    first = asyncio.create_task(client.channel_info("C01"))
    await started.wait()
    second = asyncio.create_task(client.channel_info("C01"))
    first.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await first
    finally:
        release.set()
    assert await second == PUBLIC
    assert web.calls == [("conversations_info", {"channel": "C01"})]


async def test_post_notice_maps_context_block_and_fallback_text() -> None:
    web = StubWebClient()
    text = "⚠️ Hondo hit an error and this run died."

    ref = await SlackChatClient(web).post_notice(channel="C01", text=text, thread_ts=None)

    assert ref == "1700000001.000100"
    assert web.calls == [
        (
            "chat_postMessage",
            {
                "channel": "C01",
                "text": text,
                "thread_ts": None,
                "blocks": [{"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}],
            },
        )
    ]


async def test_update_message_maps_ref_and_wraps_api_errors() -> None:
    web = StubWebClient()
    client = SlackChatClient(web)

    await client.update_message(channel="C01", message_ref="1700000000.000100", text="Updated plan")

    assert web.calls == [
        (
            "chat_update",
            {"channel": "C01", "ts": "1700000000.000100", "text": "Updated plan"},
        )
    ]

    web.update_error = SlackApiError("failed", {"ok": False, "error": "message_not_found"})
    with pytest.raises(ChatApiError, match="message_not_found"):
        await client.update_message(
            channel="C01", message_ref="1700000000.000100", text="Updated plan"
        )


async def test_add_reaction_maps_ref_and_wraps_api_errors() -> None:
    web = StubWebClient()
    client = SlackChatClient(web)

    await client.add_reaction(channel="C01", message_ref="1700000000.000100", name="eyes")

    assert web.calls == [
        (
            "reactions_add",
            {"channel": "C01", "name": "eyes", "timestamp": "1700000000.000100"},
        )
    ]

    web.reaction_error = SlackApiError("failed", {"ok": False, "error": "already_reacted"})
    with pytest.raises(ChatApiError, match="already_reacted"):
        await client.add_reaction(channel="C01", message_ref="1700000000.000100", name="eyes")


async def test_fetch_thread_normalizes_users_bots_and_missing_authors() -> None:
    web = StubWebClient()
    web.thread_response = {
        "ok": True,
        "messages": [
            {"user": "U01", "text": "one", "ts": "1.0"},
            {"bot_id": "B01", "text": "two", "ts": "2.0"},
            {"text": "three"},
        ],
    }

    messages = await SlackChatClient(web).fetch_thread(channel="C01", root="1.0", limit=20)

    assert messages == [
        ChatMessage(author="U01", text="one", ref="1.0"),
        ChatMessage(author="B01", text="two", ref="2.0"),
        ChatMessage(author="", text="three", ref=""),
    ]
    assert web.calls == [
        ("conversations_replies", {"channel": "C01", "ts": "1.0", "limit": 20, "oldest": None})
    ]


async def test_fetch_thread_reads_every_page_once() -> None:
    class PagedWebClient(StubWebClient):
        async def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(("conversations_replies", kwargs))
            if "cursor" not in kwargs:
                return {
                    "ok": True,
                    "messages": [{"user": "U01", "text": "root", "ts": "1.0"}],
                    "response_metadata": {"next_cursor": "page2"},
                }
            # Slack can repeat the root at the top of a later page.
            return {
                "ok": True,
                "messages": [
                    {"user": "U01", "text": "root", "ts": "1.0"},
                    {"user": "U02", "text": "reply", "ts": "2.0"},
                ],
                "response_metadata": {"next_cursor": ""},
            }

    web = PagedWebClient()
    messages = await SlackChatClient(web).fetch_thread(channel="C01", root="1.0")

    assert messages == [
        ChatMessage(author="U01", text="root", ref="1.0"),
        ChatMessage(author="U02", text="reply", ref="2.0"),
    ]
    assert [call[1].get("cursor") for call in web.calls] == [None, "page2"]

    web.calls.clear()
    limited = await SlackChatClient(web).fetch_thread(channel="C01", root="1.0", limit=1)
    assert limited == [ChatMessage(author="U01", text="root", ref="1.0")]
    assert len(web.calls) == 1


async def test_fetch_thread_follows_a_cursor_past_an_empty_page() -> None:
    pages: dict[str | None, dict[str, Any]] = {
        None: {"messages": [{"user": "U01", "text": "root", "ts": "1.0"}], "next": "c2"},
        "c2": {"messages": [], "next": "c3"},
        "c3": {"messages": [{"user": "U02", "text": "reply", "ts": "2.0"}], "next": ""},
    }

    class EmptyPageWebClient(StubWebClient):
        async def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
            page = pages[kwargs.get("cursor")]
            return {
                "ok": True,
                "messages": page["messages"],
                "response_metadata": {"next_cursor": page["next"]},
            }

    messages = await SlackChatClient(EmptyPageWebClient()).fetch_thread(channel="C01", root="1.0")

    assert [message.text for message in messages] == ["root", "reply"]


async def test_fetch_thread_stops_when_slack_repeats_a_cursor() -> None:
    class StuckWebClient(StubWebClient):
        async def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(("conversations_replies", kwargs))
            return {
                "ok": True,
                "messages": [{"user": "U01", "text": "root", "ts": "1.0"}],
                "response_metadata": {"next_cursor": "same"},
            }

    web = StuckWebClient()
    messages = await SlackChatClient(web).fetch_thread(channel="C01", root="1.0", limit=5)

    assert messages == [ChatMessage(author="U01", text="root", ref="1.0")]
    assert len(web.calls) == 2


async def test_fetch_history_sorts_oldest_first_and_reports_has_more() -> None:
    web = StubWebClient()
    web.thread_response = {
        "ok": True,
        "has_more": True,
        "messages": [
            {"user": "U01", "text": "newer", "ts": "2.0", "reply_count": 3},
            {"user": "U01", "text": "older", "ts": "1.0"},
        ],
    }

    page = await SlackChatClient(web).fetch_history(
        channel="C01", limit=2, oldest="0.5", latest="9.0"
    )

    assert [message.text for message in page.messages] == ["older", "newer"]
    assert page.messages[1].reply_count == 3
    assert page.has_more
    assert web.calls == [
        ("conversations_history", {"channel": "C01", "limit": 2, "oldest": "0.5", "latest": "9.0"})
    ]


async def test_attachment_text_joins_the_message_text() -> None:
    web = StubWebClient()
    web.thread_response = {
        "ok": True,
        "messages": [
            {
                "bot_id": "B01",
                "text": "",
                "ts": "1.0",
                "attachments": [
                    {
                        "fallback": "Triggered: API latency",
                        "title": "Triggered: API latency",
                        "text": "p99 above 2s for 5m",
                        "fields": [{"title": "Service", "value": "api"}, {"value": "prod"}],
                    },
                    {"fallback": "Runbook: restart the api pods"},
                ],
            },
            {
                "user": "U01",
                "text": "see <https://example.com/pr/1>",
                "ts": "2.0",
                "attachments": [{"from_url": "https://example.com/pr/1", "title": "PR 1"}],
            },
        ],
    }

    messages = await SlackChatClient(web).fetch_thread(channel="C01", root="1.0")

    assert [message.text for message in messages] == [
        "Triggered: API latency\np99 above 2s for 5m\nService: api\nprod\n"
        "Runbook: restart the api pods",
        "see <https://example.com/pr/1>",
    ]


async def test_fetch_history_wraps_api_errors() -> None:
    web = StubWebClient()
    web.history_error = SlackApiError("failed", {"ok": False, "error": "ratelimited"})

    with pytest.raises(ChatApiError, match="ratelimited"):
        await SlackChatClient(web).fetch_history(channel="C01")


@pytest.mark.parametrize(
    ("user", "expected_name"),
    [
        (
            {"profile": {"display_name": " display "}, "real_name": "Real", "name": "handle"},
            "display",
        ),
        ({"profile": {}, "real_name": " Real ", "name": "handle"}, "Real"),
        ({"profile": {}, "real_name": "", "name": " handle "}, "handle"),
        ({"profile": {}, "real_name": "", "name": ""}, "U01"),
    ],
)
async def test_user_info_name_fallback_chain(user: dict[str, Any], expected_name: str) -> None:
    web = StubWebClient()
    web.user_response = {"ok": True, "user": user}

    info = await SlackChatClient(web).user_info(user_id="U01")

    assert info.id == "U01"
    assert info.name == expected_name
    assert info.real_name == str(user.get("real_name") or "").strip()
    assert info.is_bot is False
    assert web.calls == [("users_info", {"user": "U01"})]


async def test_user_info_wraps_api_errors() -> None:
    web = StubWebClient()
    web.user_error = SlackApiError("failed", {"ok": False, "error": "user_not_found"})

    with pytest.raises(ChatApiError, match="user_not_found"):
        await SlackChatClient(web).user_info(user_id="U99")


FILES_URL = "https://files.slack.com/files-pri/T01-F01/"


def test_message_files_pick_an_image_url_the_model_can_read() -> None:
    message = {
        "files": [
            {
                "name": "alert.png",
                "mimetype": "image/png",
                "size": 14957,
                "url_private": FILES_URL + "alert.png",
                "thumb_360": FILES_URL + "alert_360.png",
            },
            {
                "name": "IMG_0001.HEIC",
                "mimetype": "image/heic",
                "size": 900_000,
                "url_private": FILES_URL + "IMG_0001.HEIC",
                "thumb_360": FILES_URL + "IMG_0001_360.jpg",
                "thumb_1024": FILES_URL + "IMG_0001_1024.jpg",
                "thumb_1024_w": 1024,
            },
            {
                "name": "huge.png",
                "mimetype": "image/png",
                "size": 9_000_000,
                "url_private": FILES_URL + "huge.png",
                "thumb_800": FILES_URL + "huge_800.png",
            },
            {"name": "report.pdf", "mimetype": "application/pdf", "url_private": FILES_URL},
            {"name": "elsewhere.png", "mimetype": "image/png", "url_private": "https://x.test/a"},
            {"mode": "tombstone"},
        ]
    }

    assert message_files(message) == (
        ChatFile("alert.png", FILES_URL + "alert.png", "image/png"),
        ChatFile("IMG_0001.HEIC", FILES_URL + "IMG_0001_1024.jpg", "image/jpeg"),
        ChatFile("huge.png", FILES_URL + "huge_800.png", "image/png"),
        ChatFile("report.pdf"),
        ChatFile("elsewhere.png"),
        ChatFile("file"),
    )


async def test_image_downloads_with_the_bot_token_once_and_only_from_slack() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("page.png"):
            return httpx.Response(
                200, content=b"<html>sign in</html>", headers={"content-type": "text/html"}
            )
        return httpx.Response(200, content=b"png-bytes", headers={"content-type": "image/png"})

    web = SimpleNamespace(token="xoxb-test")
    http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    client = SlackChatClient(web, http=http)

    assert await client.image(FILES_URL + "alert.png") == (b"png-bytes", "image/png")
    assert await client.image(FILES_URL + "alert.png") == (b"png-bytes", "image/png")
    assert await client.image(FILES_URL + "page.png") is None
    assert await client.image("https://x.test/alert.png") is None

    assert [str(request.url) for request in requests] == [
        FILES_URL + "alert.png",
        FILES_URL + "page.png",
    ]
    assert {request.headers["authorization"] for request in requests} == {"Bearer xoxb-test"}


async def test_thread_messages_carry_their_files() -> None:
    web = StubWebClient()
    web.thread_response = {
        "ok": True,
        "messages": [
            {
                "user": "U01",
                "ts": "1700000000.000100",
                "text": "",
                "files": [
                    {
                        "name": "alert.png",
                        "mimetype": "image/png",
                        "size": 10,
                        "url_private": FILES_URL + "alert.png",
                    }
                ],
            }
        ],
    }

    [message] = await SlackChatClient(web).fetch_thread(channel="C01", root="1700000000.000100")

    assert message.files == (ChatFile("alert.png", FILES_URL + "alert.png", "image/png"),)
