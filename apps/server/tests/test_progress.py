import asyncio
from collections.abc import Callable

import pytest

from hondo.chat import ChatApiError
from hondo.runtime import progress as progress_module
from hondo.runtime.progress import ProgressReporter
from tests.fakes import FakeSlack


def make_reporter(slack: FakeSlack) -> ProgressReporter:
    return ProgressReporter(
        slack,
        channel="C01",
        thread_ts="1700000000.000100",
    )


async def test_post_threads_and_returns_ts() -> None:
    slack = FakeSlack()
    reporter = make_reporter(slack)

    ts = await reporter.post("On it — checking.")
    assert ts == slack.posted[0]["ts"]
    assert slack.posted[0]["thread_ts"] == "1700000000.000100"

    await reporter.post("Final answer.")
    assert [m["text"] for m in slack.posted] == ["On it — checking.", "Final answer."]
    assert slack.updates == []


async def test_post_is_plain_message_without_blocks() -> None:
    slack = FakeSlack()
    await make_reporter(slack).post("On it — checking.")

    assert slack.posted[0]["blocks"] is None
    assert slack.reactions == []  # no reactions anywhere in progress UX


async def test_post_notice_renders_as_muted_context_block() -> None:
    slack = FakeSlack()
    text = "⚠️ Hondo hit an error and this run died."
    await make_reporter(slack).post_notice(text)

    notice = slack.posted[0]
    assert notice["text"] == text  # notification fallback
    assert notice["thread_ts"] == "1700000000.000100"
    assert notice["blocks"] == [{"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}]


async def wait_until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(2):
        while not condition():
            await asyncio.sleep(0.005)


async def stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    await asyncio.wait({task})


def refresh_every(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    monkeypatch.setattr(progress_module, "STATUS_REFRESH_SECONDS", seconds)


async def test_status_is_sent_again_on_a_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    refresh_every(monkeypatch, 0.02)
    slack = FakeSlack()
    reporter = make_reporter(slack)
    keeper = asyncio.create_task(reporter.keep_status(lambda: True))

    await wait_until(lambda: len(slack.statuses) >= 3)
    await stop(keeper)

    assert {s["text"] for s in slack.statuses} == {"is working…"}
    assert {(s["channel"], s["thread_ts"]) for s in slack.statuses} == {
        ("C01", "1700000000.000100")
    }


async def test_a_post_sends_the_status_again_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    refresh_every(monkeypatch, 60)
    slack = FakeSlack()
    reporter = make_reporter(slack)
    keeper = asyncio.create_task(reporter.keep_status(lambda: True))
    await wait_until(lambda: len(slack.statuses) == 1)

    await reporter.post("on it")
    await wait_until(lambda: len(slack.statuses) == 2)
    await stop(keeper)

    # Slack drops the status when the app posts, so it goes out again after the post.
    assert slack.statuses[1]["posts"] == 1


async def test_the_status_starts_when_hondo_starts_working(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refresh_every(monkeypatch, 0.01)
    slack = FakeSlack()
    reporter = make_reporter(slack)
    working = [False]
    keeper = asyncio.create_task(reporter.keep_status(lambda: working[0]))
    await asyncio.sleep(0.05)
    assert slack.statuses == []

    working[0] = True
    await wait_until(lambda: bool(slack.statuses))
    await stop(keeper)


async def test_a_hidden_status_is_never_sent_or_cleared(monkeypatch: pytest.MonkeyPatch) -> None:
    refresh_every(monkeypatch, 0.01)
    slack = FakeSlack()
    reporter = make_reporter(slack)
    keeper = asyncio.create_task(reporter.keep_status(lambda: False))
    await asyncio.sleep(0.05)
    await reporter.post("not for me")
    await asyncio.sleep(0.05)
    await stop(keeper)
    await reporter.clear_status()

    assert slack.statuses == []


async def test_clear_status_removes_a_sent_status(monkeypatch: pytest.MonkeyPatch) -> None:
    refresh_every(monkeypatch, 60)
    slack = FakeSlack()
    reporter = make_reporter(slack)
    keeper = asyncio.create_task(reporter.keep_status(lambda: True))
    await wait_until(lambda: len(slack.statuses) == 1)
    await stop(keeper)

    await reporter.clear_status()

    assert [s["text"] for s in slack.statuses] == ["is working…", ""]


async def test_a_slow_clear_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    refresh_every(monkeypatch, 60)
    monkeypatch.setattr(progress_module, "STATUS_CALL_TIMEOUT_SECONDS", 0.05)

    class SlowSlack(FakeSlack):
        async def set_status(self, *, channel: str, thread_ts: str, text: str) -> None:
            if not text:
                await asyncio.sleep(60)
            await super().set_status(channel=channel, thread_ts=thread_ts, text=text)

    slack = SlowSlack()
    reporter = make_reporter(slack)
    keeper = asyncio.create_task(reporter.keep_status(lambda: True))
    await wait_until(lambda: len(slack.statuses) == 1)
    await stop(keeper)

    # The run waits for the clear; a slow Slack must not hold it.
    async with asyncio.timeout(1):
        await reporter.clear_status()

    assert [s["text"] for s in slack.statuses] == ["is working…"]


async def test_a_status_call_in_flight_reaches_slack_before_a_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refresh_every(monkeypatch, 60)
    started, release = asyncio.Event(), asyncio.Event()

    class GatedSlack(FakeSlack):
        async def set_status(self, *, channel: str, thread_ts: str, text: str) -> None:
            started.set()
            await release.wait()
            await super().set_status(channel=channel, thread_ts=thread_ts, text=text)

    slack = GatedSlack()
    reporter = make_reporter(slack)
    keeper = asyncio.create_task(reporter.keep_status(lambda: True))
    await started.wait()

    post = asyncio.create_task(reporter.post("The deploy is healthy."))
    await asyncio.sleep(0.05)
    assert slack.posted == []  # the post waits for the status call
    release.set()
    await post
    await stop(keeper)

    assert slack.statuses[0]["posts"] == 0
    assert [m["text"] for m in slack.posted] == ["The deploy is healthy."]


async def test_a_slow_status_call_does_not_hold_a_post(monkeypatch: pytest.MonkeyPatch) -> None:
    refresh_every(monkeypatch, 60)
    monkeypatch.setattr(progress_module, "STATUS_CALL_TIMEOUT_SECONDS", 0.05)
    started = asyncio.Event()

    class StuckSlack(FakeSlack):
        async def set_status(self, *, channel: str, thread_ts: str, text: str) -> None:
            started.set()
            await asyncio.sleep(60)

    slack = StuckSlack()
    reporter = make_reporter(slack)
    keeper = asyncio.create_task(reporter.keep_status(lambda: True))
    await started.wait()

    async with asyncio.timeout(1):
        await reporter.post("on it")
    await stop(keeper)

    assert [m["text"] for m in slack.posted] == ["on it"]


async def test_a_dm_gets_no_status() -> None:
    slack = FakeSlack()
    reporter = ProgressReporter(slack, channel="D01", thread_ts=None)

    async with asyncio.timeout(1):
        await reporter.keep_status(lambda: True)
    await reporter.clear_status()

    assert slack.statuses == []


async def test_a_failing_status_call_keeps_the_heartbeat_going(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refresh_every(monkeypatch, 0.01)

    class RefusingSlack(FakeSlack):
        async def set_status(self, *, channel: str, thread_ts: str, text: str) -> None:
            await super().set_status(channel=channel, thread_ts=thread_ts, text=text)
            raise ChatApiError("not_allowed")

    slack = RefusingSlack()
    reporter = make_reporter(slack)
    keeper = asyncio.create_task(reporter.keep_status(lambda: True))

    await wait_until(lambda: len(slack.statuses) >= 3)
    await stop(keeper)
    await reporter.clear_status()  # never raises

    assert slack.statuses[-1]["text"] == ""
