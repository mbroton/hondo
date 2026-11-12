"""UserDirectory: cached id → name resolution and pure mention expansion."""

from hondo.chat import ChatUser
from hondo.users import UserDirectory, expand_mentions, mention_ids


class CountingSlack:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def user_info(self, *, user_id: str) -> ChatUser:
        self.calls.append(user_id)
        if user_id == "U01":
            return ChatUser(id="U01", name="jane", real_name="Jane Doe", is_bot=False)
        if user_id == "U02":
            return ChatUser(id="U02", name="Kate Nowak", real_name="Kate Nowak", is_bot=False)
        raise RuntimeError(f"no such user {user_id}")


async def test_directory_resolves_and_caches() -> None:
    slack = CountingSlack()
    directory = UserDirectory(slack)  # type: ignore[arg-type]
    assert await directory.name("U01") == "jane"
    assert await directory.name("U01") == "jane"
    assert slack.calls == ["U01"]  # second hit served from cache
    assert await directory.name("U02") == "Kate Nowak"


async def test_directory_failure_is_cached_and_yields_none() -> None:
    slack = CountingSlack()
    directory = UserDirectory(slack)  # type: ignore[arg-type]
    assert await directory.name("U99") is None
    assert await directory.name("U99") is None
    assert slack.calls == ["U99"]  # failures don't hammer the API either
    assert await directory.resolve_many(["U01", "U99"]) == {"U01": "jane"}


def test_mention_ids_and_expansion() -> None:
    texts = ["ask <@U01> about it", "cc <@U02|kate> and <@U01>", "bot msg <@B01>"]
    # B01 is a bot_id, not a user id — never treated as a mention.
    assert mention_ids(texts) == {"U01", "U02"}
    names = {"U01": "jane"}
    assert expand_mentions("ask <@U01>, <@U02> stays raw", names) == "ask @jane, <@U02> stays raw"
