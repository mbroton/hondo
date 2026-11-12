import pytest

from hondo.config import EngagePolicy
from hondo.models import Event, EventKind, Intent
from hondo.runtime.router import route


def make_event(**overrides: object) -> Event:
    defaults: dict[str, object] = {
        "event_id": "Ev001",
        "kind": EventKind.MESSAGE,
        "team": "T01",
        "channel": "C01",
        "ts": "1700000000.000100",
        "user": "U01",
        "text": "hello",
    }
    return Event(**{**defaults, **overrides})  # type: ignore[arg-type]


def mention(**overrides: object) -> Event:
    return make_event(kind=EventKind.APP_MENTION, mentions_bot=True, **overrides)


def dm(**overrides: object) -> Event:
    return make_event(channel="D01", is_dm=True, **overrides)


def thread_reply(**overrides: object) -> Event:
    return make_event(thread_ts="1700000000.000001", **overrides)


def test_mention_engages_when_idle() -> None:
    assert route(mention(), EngagePolicy()) is Intent.ENGAGE


def test_mention_steers_when_live() -> None:
    assert route(mention(), EngagePolicy(), run_live=True) is Intent.STEER


def test_dm_engages_when_idle() -> None:
    assert route(dm(), EngagePolicy()) is Intent.ENGAGE


def test_dm_steers_when_live() -> None:
    assert route(dm(), EngagePolicy(), run_live=True) is Intent.STEER


def test_unmentioned_channel_chatter_is_dropped() -> None:
    assert route(make_event(), EngagePolicy()) is None


def test_mentions_can_be_disabled() -> None:
    assert route(mention(), EngagePolicy(respond_to_mentions=False)) is None
    assert route(mention(), EngagePolicy(respond_to_mentions=False), run_live=True) is None


def test_dms_can_be_disabled() -> None:
    assert route(dm(), EngagePolicy(respond_to_dms=False)) is None
    assert route(dm(), EngagePolicy(respond_to_dms=False), run_live=True) is None


def test_allowlist_blocks_other_channels() -> None:
    policy = EngagePolicy(channel_allowlist=frozenset({"C99"}))
    assert route(mention(), policy) is None
    assert route(mention(channel="C99"), policy) is Intent.ENGAGE


def test_thread_reply_steers_live_session() -> None:
    assert route(thread_reply(), EngagePolicy(), session_exists=True, run_live=True) is Intent.STEER


def test_thread_reply_wakes_idle_engaged_session() -> None:
    # Engaged-thread following (default policy): once a session exists, an
    # un-addressed reply starts a run; the model decides whether to answer.
    assert route(thread_reply(), EngagePolicy(), session_exists=True, run_live=False) is (
        Intent.ENGAGE
    )


def test_thread_reply_in_cold_thread_is_dropped() -> None:
    assert route(thread_reply(), EngagePolicy(), session_exists=False, run_live=False) is None


@pytest.mark.parametrize("event", [make_event(), thread_reply()])
def test_listening_channel_engages_unaddressed_messages(event: Event) -> None:
    assert route(event, EngagePolicy(), listening=True) is Intent.ENGAGE


def test_listening_respects_the_channel_allowlist() -> None:
    policy = EngagePolicy(channel_allowlist=frozenset({"C99"}))
    assert route(make_event(), policy, listening=True) is None


def test_mention_only_wins_over_listening() -> None:
    assert route(make_event(), EngagePolicy(), listening=True, mention_only=True) is None


@pytest.mark.parametrize("run_live", [False, True])
def test_mention_only_drops_unaddressed_thread_reply(run_live: bool) -> None:
    assert (
        route(
            thread_reply(),
            EngagePolicy(),
            session_exists=True,
            run_live=run_live,
            mention_only=True,
        )
        is None
    )


@pytest.mark.parametrize(("run_live", "intent"), [(False, Intent.ENGAGE), (True, Intent.STEER)])
def test_mention_only_still_answers_mentions(run_live: bool, intent: Intent) -> None:
    event = mention(thread_ts="1700000000.000001")
    assert (
        route(event, EngagePolicy(), session_exists=True, run_live=run_live, mention_only=True)
        is intent
    )


def test_thread_reply_steers_first_live_run_before_session_exists() -> None:
    # The session row lands at run end, so a thread's first run has no session
    # yet — un-addressed replies must still steer it (regression: dogfooding).
    assert (
        route(thread_reply(), EngagePolicy(), session_exists=False, run_live=True) is Intent.STEER
    )
