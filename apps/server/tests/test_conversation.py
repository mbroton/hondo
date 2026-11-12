from types import SimpleNamespace
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest
from pydantic_ai.models.test import TestModel

from hondo.agent.conversation import ConversationCapability, SearchPage
from hondo.chat import ChannelInfo, ChatApiError, ChatHistoryPage, ChatMessage, ChatUser
from hondo.context import RunScope, reset_scope, set_scope
from hondo.models import Event, EventKind, SessionKey
from hondo.users import UserDirectory
from tests.fakes import FakeSlack

EVENT = Event(
    event_id="Ev001",
    kind=EventKind.APP_MENTION,
    team="T01",
    channel="C01",
    ts="1700000000.000100",
    user="U01",
    text="<@UBOT> hello",
    mentions_bot=True,
    bot_user="UBOT",
)

SCOPE = RunScope(
    key=SessionKey.from_event(EVENT),
    event=EVENT,
    roster={"UBOT": "Hondo", "U01": "jane"},
)


@pytest.fixture
def slack_client() -> FakeSlack:
    return FakeSlack()


def make_conversation(slack_client: FakeSlack) -> ConversationCapability:
    return ConversationCapability(
        client=slack_client,
        directory=UserDirectory(slack_client),
    )


async def test_toolset_tools_hit_slack(slack_client: FakeSlack) -> None:
    slack_client.thread_messages = [
        ChatMessage(author="U01", ref="1700000000.000100", text="earlier message")
    ]
    conversation = make_conversation(slack_client)
    agent = Agent(TestModel(), capabilities=[conversation])  # TestModel calls every tool once

    scope = RunScope(key=SCOPE.key, event=EVENT, roster=dict(SCOPE.roster))
    token = set_scope(scope)
    try:
        await agent.run("hello")
    finally:
        reset_scope(token)

    assert slack_client.reactions == [
        {"channel": "C01", "name": "a", "message_ref": "1700000000.000100"}
    ]
    assert slack_client.posted[0]["channel"] == "C01"
    assert slack_client.posted[0]["thread_ts"] == "1700000000.000100"


async def test_instructions_include_etiquette_and_session_context(slack_client: FakeSlack) -> None:
    conversation = make_conversation(slack_client)
    agent = Agent(TestModel(call_tools=[]), capabilities=[conversation])

    token = set_scope(SCOPE)
    try:
        result = await agent.run("hello")
    finally:
        reset_scope(token)

    first_request = result.all_messages()[0]
    assert isinstance(first_request, ModelRequest)
    instructions = first_request.instructions
    assert instructions is not None
    assert "mrkdwn" in instructions
    assert "<#C01>" in instructions
    # Identity and the roster: the model knows who it is and who's here.
    assert "you go by @Hondo (<@UBOT>)" in instructions
    assert "People here: jane (<@U01>)" in instructions
    # The ambient-message protocol (untagged participation) is taught here.
    assert "call no_reply" in instructions
    assert "does NOT mention you" in instructions
    # Order: identity first, writing rules next, per-run situation last (the
    # dynamic tail keeps the cacheable prefix stable across sessions).
    assert (
        instructions.index("<identity>")
        < instructions.index("mrkdwn")
        < instructions.index("People here:")
    )
    # Every fragment is a tagged section, so the composed prompt separates
    # concerns instead of stacking untagged blobs.
    for tag_name in ("identity", "slack_conventions", "current_session"):
        assert f"<{tag_name}>" in instructions
        assert f"</{tag_name}>" in instructions


async def test_identity_and_session_context_are_empty_outside_a_run(
    slack_client: FakeSlack,
) -> None:
    conversation = make_conversation(slack_client)
    assert conversation._identity() == ""
    assert await conversation._session_context() == ""


@pytest.mark.parametrize(
    "info,said",
    [
        (ChannelInfo(is_channel=True, is_private=False, is_ext_shared=False), "<#C01> (public)."),
        (ChannelInfo(is_channel=True, is_private=True, is_ext_shared=False), "<#C01> (private)."),
        (
            ChannelInfo(is_channel=True, is_private=False, is_ext_shared=True),
            "<#C01> (public, shared with another organization).",
        ),
        (None, "<#C01>."),
    ],
)
async def test_session_context_says_who_can_see_the_channel(
    slack_client: FakeSlack, info: ChannelInfo | None, said: str
) -> None:
    slack_client.channels["C01"] = info
    conversation = make_conversation(slack_client)
    token = set_scope(SCOPE)
    try:
        context = await conversation._session_context()
    finally:
        reset_scope(token)
    assert f"Right now you are replying in a thread in channel {said}" in context


async def test_lookup_user_resolves_and_extends_the_roster(slack_client: FakeSlack) -> None:
    conversation = make_conversation(slack_client)
    scope = RunScope(key=SCOPE.key, event=EVENT, roster={})
    token = set_scope(scope)
    try:
        # Raw <@…> form and unknown ids are both handled gracefully.
        assert (
            await conversation._lookup_user("<@U01>")
            == "<@U01> is @jane (person, real name Jane Doe)."
        )
        assert scope.roster == {"U01": "jane"}
        assert "No Slack user found" in await conversation._lookup_user("U99")
    finally:
        reset_scope(token)


async def test_tools_refuse_to_run_without_scope(slack_client: FakeSlack) -> None:
    conversation = make_conversation(slack_client)
    with pytest.raises(RuntimeError, match="No Hondo run scope"):
        await conversation._add_reaction("eyes")


async def test_add_reaction_tolerates_already_reacted(slack_client: FakeSlack) -> None:
    """Repeat reactions (progress :eyes:, redelivered runs) must not fail the run."""

    class ReactedSlack(FakeSlack):
        error = "already_reacted"

        async def add_reaction(self, **kwargs: Any) -> None:
            raise ChatApiError(self.error)

    reacted = ReactedSlack()
    conversation = make_conversation(reacted)
    token = set_scope(SCOPE)
    try:
        assert await conversation._add_reaction("eyes") == "Reacted with :eyes:"
        reacted.error = "invalid_name"  # anything else must still surface
        with pytest.raises(ChatApiError, match="invalid_name"):
            await conversation._add_reaction("bogus")
    finally:
        reset_scope(token)


async def test_read_thread_in_dm_uses_channel_history(slack_client: FakeSlack) -> None:
    dm_event = EVENT.model_copy(update={"channel": "D01", "is_dm": True, "kind": EventKind.MESSAGE})
    slack_client.channel_messages = [
        ChatMessage(author="U01", ref="2.0", text="newest"),
        ChatMessage(author="U01", ref="1.0", text="oldest"),
    ]
    conversation = make_conversation(slack_client)

    token = set_scope(RunScope(key=SessionKey.from_event(dm_event), event=dm_event))
    try:
        page = await conversation._read_thread()
    finally:
        reset_scope(token)

    assert [m["text"] for m in page["messages"]] == ["oldest", "newest"]
    assert "more" not in page


def _channel_msg(ref: str, text: str, replies: int = 0) -> ChatMessage:
    return ChatMessage(author="U01", ref=ref, text=text, reply_count=replies)


async def test_read_channel_returns_newest_messages_oldest_first(slack_client: FakeSlack) -> None:
    slack_client.channel_messages = [_channel_msg(f"{i}.0", f"m{i}") for i in range(1, 6)]
    conversation = make_conversation(slack_client)

    token = set_scope(RunScope(key=SCOPE.key, event=EVENT))
    try:
        page = await conversation._read_channel(limit=3)
    finally:
        reset_scope(token)

    assert [m["text"] for m in page["messages"]] == ["m3", "m4", "m5"]
    assert page.get("more") == {"before": "3.0"}


async def test_read_channel_complete_range_has_no_more(slack_client: FakeSlack) -> None:
    slack_client.channel_messages = [_channel_msg("1.0", "only")]
    conversation = make_conversation(slack_client)

    token = set_scope(RunScope(key=SCOPE.key, event=EVENT))
    try:
        page = await conversation._read_channel(limit=3)
    finally:
        reset_scope(token)

    assert [m["text"] for m in page["messages"]] == ["only"]
    assert "more" not in page


async def test_read_channel_forward_read_starts_right_after_anchor(slack_client: FakeSlack) -> None:
    # The messages right after the anchor matter, not the newest of the range.
    slack_client.channel_messages = [_channel_msg(f"{i}.0", f"m{i}") for i in range(1, 10)]
    conversation = make_conversation(slack_client)

    token = set_scope(RunScope(key=SCOPE.key, event=EVENT))
    try:
        page = await conversation._read_channel(after="2.0", limit=3)
    finally:
        reset_scope(token)

    assert [m["text"] for m in page["messages"]] == ["m3", "m4", "m5"]
    assert page.get("more") == {"after": "5.0"}


async def test_read_channel_forward_read_carries_before_bound_into_more(
    slack_client: FakeSlack,
) -> None:
    slack_client.channel_messages = [_channel_msg(f"{i}.0", f"m{i}") for i in range(1, 10)]
    conversation = make_conversation(slack_client)

    token = set_scope(RunScope(key=SCOPE.key, event=EVENT))
    try:
        page = await conversation._read_channel(after="2.0", before="8.0", limit=3)
    finally:
        reset_scope(token)

    assert [m["text"] for m in page["messages"]] == ["m3", "m4", "m5"]
    assert page.get("more") == {"after": "5.0", "before": "8.0"}


async def test_read_channel_rejects_oversized_forward_range(
    slack_client: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pydantic_ai import ModelRetry

    monkeypatch.setattr("hondo.agent.conversation._HISTORY_FETCH_MAX", 3)
    slack_client.channel_messages = [_channel_msg(f"{i}.0", f"m{i}") for i in range(1, 7)]
    conversation = make_conversation(slack_client)

    token = set_scope(RunScope(key=SCOPE.key, event=EVENT))
    try:
        with pytest.raises(ModelRetry, match="narrow"):
            await conversation._read_channel(after="0.5")
    finally:
        reset_scope(token)


async def test_read_channel_marks_thread_starters(slack_client: FakeSlack) -> None:
    slack_client.channel_messages = [
        _channel_msg("1.0", "plain"),
        _channel_msg("2.0", "started a thread", replies=4),
    ]
    conversation = make_conversation(slack_client)

    token = set_scope(RunScope(key=SCOPE.key, event=EVENT))
    try:
        page = await conversation._read_channel()
    finally:
        reset_scope(token)

    plain, starter = page["messages"]
    assert "replies" not in plain
    assert starter.get("replies") == 4


async def test_read_thread_opens_another_thread_by_root(slack_client: FakeSlack) -> None:
    slack_client.thread_messages = [_channel_msg("42.0", "root message")]
    conversation = make_conversation(slack_client)

    token = set_scope(RunScope(key=SCOPE.key, event=EVENT))
    try:
        page = await conversation._read_thread(root="42.0")
    finally:
        reset_scope(token)

    assert slack_client.thread_fetches[-1]["root"] == "42.0"
    assert [m["text"] for m in page["messages"]] == ["root message"]


async def test_read_thread_truncation_offers_continuation(slack_client: FakeSlack) -> None:
    slack_client.thread_messages = [_channel_msg(f"{i}.0", f"m{i}") for i in range(1, 6)]
    conversation = make_conversation(slack_client)

    token = set_scope(RunScope(key=SCOPE.key, event=EVENT))
    try:
        page = await conversation._read_thread(limit=3)
    finally:
        reset_scope(token)

    assert [m["text"] for m in page["messages"]] == ["m1", "m2", "m3"]
    more = page.get("more")
    assert more == {"root": EVENT.ts, "after": "3.0"}
    assert more is not None

    token = set_scope(RunScope(key=SCOPE.key, event=EVENT))
    try:
        rest = await conversation._read_thread(**more, limit=3)
    finally:
        reset_scope(token)

    assert [m["text"] for m in rest["messages"]] == ["m4", "m5"]
    assert "more" not in rest


def _ts(n: int) -> str:
    return f"17000000{n:02d}.000100"


async def _search(conversation: ConversationCapability, **kwargs: Any) -> SearchPage:
    token = set_scope(RunScope(key=SCOPE.key, event=EVENT))
    try:
        return await conversation._search_channel(**kwargs)
    finally:
        reset_scope(token)


async def test_search_ranks_by_matched_words_then_replies_then_newest(
    slack_client: FakeSlack,
) -> None:
    slack_client.channel_messages = [
        _channel_msg(_ts(1), "postgres timeout on the api"),
        _channel_msg(_ts(2), "postgres upgrade done", replies=5),
        _channel_msg(_ts(3), "Timeouts again"),
        _channel_msg(_ts(4), "is POSTGRES ok?"),
        _channel_msg(_ts(5), "notimeout here"),
        _channel_msg(_ts(6), "deploy finished"),
    ]
    conversation = make_conversation(slack_client)

    page = await _search(conversation, words=["postgres", "timeout"])
    top = await _search(conversation, words=["postgres", "timeout"], limit=2)

    assert [hit["text"] for hit in page["hits"]] == [
        "postgres timeout on the api",
        "postgres upgrade done",
        "is POSTGRES ok?",
        "Timeouts again",
    ]
    assert "more" not in page
    assert [hit["text"] for hit in top["hits"]] == [
        "postgres timeout on the api",
        "postgres upgrade done",
    ]
    assert (page["matched"], top["matched"]) == (4, 4)


async def test_search_hit_is_a_linked_start_of_the_message(slack_client: FakeSlack) -> None:
    text = "deploy failed: " + "x" * 300
    slack_client.channel_messages = [_channel_msg("1700000000.000100", text, replies=2)]

    page = await _search(make_conversation(slack_client), words=["deploy"])

    assert page["hits"] == [
        {
            "user": "U01",
            "name": "jane",
            "ts": "1700000000.000100",
            "text": text[:200] + "…",
            "replies": 2,
            "date": "2023-11-14 22:13 UTC",
            "link": "https://slack.com/archives/C01/p1700000000000100",
        }
    ]


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        (
            "[FIRING] api " + "x" * 300 + " connection timeout on /v1/orders " + "y" * 300,
            "connection timeout on /v1/orders",
        ),
        # Slack's escapes make the raw text longer than the text people read.
        ("&amp;" * 100 + " timeout here " + "z" * 300, "timeout here"),
        # Casefold makes the text longer: "ß" becomes "ss".
        ("Straße " * 200 + "timeout here " + "z" * 300, "timeout here"),
    ],
    ids=["far", "escapes", "casefold"],
)
async def test_search_hit_shows_the_text_around_the_match(
    slack_client: FakeSlack, text: str, shown: str
) -> None:
    slack_client.channel_messages = [_channel_msg(_ts(1), text)]

    page = await _search(make_conversation(slack_client), words=["timeout"])

    hit = page["hits"][0]["text"]
    assert shown in hit
    assert hit.startswith("…")
    assert hit.endswith("…")
    assert len(hit) <= 202


async def test_search_hit_drops_a_link_that_the_window_splits(slack_client: FakeSlack) -> None:
    link = "<https://example.com/" + "p" * 80 + "|runbook>"
    text = "x" * 300 + f" {link} api timeout " + "y" * 300
    slack_client.channel_messages = [_channel_msg(_ts(1), text)]

    page = await _search(make_conversation(slack_client), words=["timeout"])

    assert page["hits"][0]["text"].startswith("…api timeout y")


async def test_search_hit_keeps_a_split_link_that_holds_the_match(
    slack_client: FakeSlack,
) -> None:
    link = "<https://grafana.example.com/d/" + "p" * 80 + "|api timeout dashboard>"
    slack_client.channel_messages = [_channel_msg(_ts(1), "x" * 300 + f" {link} " + "y" * 300)]

    page = await _search(make_conversation(slack_client), words=["timeout"])

    assert link in page["hits"][0]["text"]


async def test_search_looks_up_only_the_names_it_shows(slack_client: FakeSlack) -> None:
    slack_client.users["U02"] = ChatUser(id="U02", name="sam", real_name="Sam Roe", is_bot=False)
    slack_client.users["U03"] = ChatUser(id="U03", name="kim", real_name="Kim Poe", is_bot=False)
    shown = "deploy failed, <@U02> is on it"
    cut = "deploy failed again " + "x" * 176 + " <@U03> please look"
    slack_client.channel_messages = [_channel_msg(_ts(1), shown), _channel_msg(_ts(2), cut)]
    scope = RunScope(key=SCOPE.key, event=EVENT)
    token = set_scope(scope)
    try:
        page = await make_conversation(slack_client)._search_channel(words=["deploy"])
    finally:
        reset_scope(token)

    assert [hit["text"] for hit in page["hits"]] == [
        "deploy failed again " + "x" * 176 + "…",
        "deploy failed, @sam is on it",
    ]
    assert scope.roster == {"U01": "jane", "U02": "sam"}


async def test_search_matches_a_phrase_as_written(slack_client: FakeSlack) -> None:
    slack_client.channel_messages = [
        _channel_msg(_ts(1), "Connection refused by db-1"),
        _channel_msg(_ts(2), "the connection was refused"),
    ]

    page = await _search(make_conversation(slack_client), words=["connection refused"])

    assert [hit["text"] for hit in page["hits"]] == ["Connection refused by db-1"]


async def test_search_reads_text_as_people_wrote_it(slack_client: FakeSlack) -> None:
    # Slack escapes &, < and > in the raw text.
    slack_client.channel_messages = [
        _channel_msg(_ts(1), "P&amp;L report is late"),
        _channel_msg(_ts(2), "p99 back to &lt;500ms"),
        _channel_msg(_ts(3), "Straße closed"),
        _channel_msg(_ts(4), "<@U01> please check the db"),
        _channel_msg(_ts(5), "_outage_ in prod, db_timeout"),
    ]
    conversation = make_conversation(slack_client)

    found = [
        [hit["ts"] for hit in (await _search(conversation, words=[word]))["hits"]]
        for word in ("p&l", "<500ms", "STRASSE", "straße", "U01", "outage", "timeout")
    ]

    assert found == [[_ts(1)], [_ts(2)], [_ts(3)], [_ts(3)], [_ts(4)], [_ts(5)], [_ts(5)]]


async def test_search_stops_at_its_read_limit_and_continues_below(
    slack_client: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("hondo.agent.conversation._HISTORY_FETCH_MAX", 3)
    monkeypatch.setattr("hondo.agent.conversation._SEARCH_MAX_FETCHES", 2)
    slack_client.channel_messages = [_channel_msg(_ts(n), f"alert {n}") for n in range(1, 10)]
    conversation = make_conversation(slack_client)

    first = await _search(conversation, words=["alert"])
    more = first.get("more")
    assert more is not None
    rest = await _search(conversation, words=["alert"], **more)

    # Two reads of three messages: the six newest.
    assert sorted(hit["ts"] for hit in first["hits"]) == [_ts(n) for n in range(4, 10)]
    assert more == {"before": _ts(4)}
    assert first.get("searched_since") == "2023-11-14 22:13 UTC"
    assert sorted(hit["ts"] for hit in rest["hits"]) == [_ts(n) for n in range(1, 4)]
    assert "more" not in rest
    assert "searched_since" not in rest


async def test_search_needs_a_word(slack_client: FakeSlack) -> None:
    from pydantic_ai import ModelRetry

    with pytest.raises(ModelRetry, match="at least one word"):
        await _search(make_conversation(slack_client), words=[" ", ""])


@pytest.mark.parametrize(("limit", "hits"), [(100, 50), (-1, 1)])
async def test_search_returns_1_to_50_hits(slack_client: FakeSlack, limit: int, hits: int) -> None:
    slack_client.channel_messages = [_channel_msg(_ts(n), f"alert {n}") for n in range(60)]

    page = await _search(make_conversation(slack_client), words=["alert"], limit=limit)

    assert len(page["hits"]) == hits
    assert page["matched"] == 60


class SlowSlack(FakeSlack):
    """Each history read takes 20 s on `now`."""

    def __init__(self) -> None:
        super().__init__()
        self.now = 0.0

    async def fetch_history(self, **kwargs: Any) -> ChatHistoryPage:
        self.now += 20
        return await super().fetch_history(**kwargs)


async def test_search_starts_no_read_after_30_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hondo.agent.conversation._HISTORY_FETCH_MAX", 3)
    slack = SlowSlack()
    monkeypatch.setattr(
        "hondo.agent.conversation.time", SimpleNamespace(monotonic=lambda: slack.now)
    )
    slack.channel_messages = [_channel_msg(_ts(n), f"alert {n}") for n in range(1, 10)]

    page = await _search(make_conversation(slack), words=["alert"])

    # Reads end at 20 s and 40 s; no third read starts.
    assert sorted(hit["ts"] for hit in page["hits"]) == [_ts(n) for n in range(4, 10)]
    assert page.get("more") == {"before": _ts(4)}


class FailingSlack(FakeSlack):
    """Answers `answered` history reads, then fails with `error`."""

    def __init__(self, answered: int, error: Exception) -> None:
        super().__init__()
        self.answered = answered
        self.error = error

    async def fetch_history(self, **kwargs: Any) -> ChatHistoryPage:
        if self.answered == 0:
            raise self.error
        self.answered -= 1
        return await super().fetch_history(**kwargs)


# A Slack error, and a request that times out on the client side.
@pytest.mark.parametrize("error", [ChatApiError("ratelimited"), TimeoutError()])
async def test_search_cut_short_by_slack_returns_what_it_found(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    monkeypatch.setattr("hondo.agent.conversation._HISTORY_FETCH_MAX", 3)
    slack = FailingSlack(answered=1, error=error)
    slack.channel_messages = [_channel_msg(_ts(n), f"alert {n}") for n in range(1, 7)]

    page = await _search(make_conversation(slack), words=["alert"])

    assert sorted(hit["ts"] for hit in page["hits"]) == [_ts(4), _ts(5), _ts(6)]
    assert page.get("more") == {"before": _ts(4)}


async def test_search_failing_before_any_read_raises() -> None:
    with pytest.raises(ChatApiError, match="ratelimited"):
        slack = FailingSlack(answered=0, error=ChatApiError("ratelimited"))
        await _search(make_conversation(slack), words=["alert"])
