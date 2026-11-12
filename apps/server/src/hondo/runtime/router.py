from __future__ import annotations

from hondo.config import EngagePolicy
from hondo.models import Event, Intent


def route(
    event: Event,
    policy: EngagePolicy,
    *,
    session_exists: bool = False,
    run_live: bool = False,
    mention_only: bool = False,
    listening: bool = False,
) -> Intent | None:
    """Deterministic trigger policy: which intent, if any, does this event carry?

    Addressed messages (DMs, mentions) always warrant a response: a live run absorbs
    them as STEER, an idle session starts a fresh ENGAGE. Un-addressed thread replies
    steer a live run, and — once the bot is engaged in the thread (a session exists)
    — wake an idle one too; whether that run actually answers is the model's call
    (it may end silently). Cold threads stay mention-only, and so does a thread or
    channel set to mention-only: there an un-addressed reply does nothing. In a
    channel that listens for a goal, any un-addressed message also starts a run,
    which first screens it.
    """
    if event.is_dm:
        if not policy.respond_to_dms:
            return None
        return Intent.STEER if run_live else Intent.ENGAGE
    if event.mentions_bot:
        if not (policy.respond_to_mentions and policy.allows_channel(event.channel)):
            return None
        return Intent.STEER if run_live else Intent.ENGAGE
    if mention_only:
        return None
    if event.thread_ts is not None:
        # A live run IS engagement — steer regardless of session_exists. The
        # session row is only written at run end, so gating on it dropped
        # un-addressed replies during a thread's *first* run (found dogfooding).
        if run_live:
            return Intent.STEER
        # Engaged-thread following: once tagged into a thread, the bot hears
        # un-addressed replies when idle too. The relevance judgment is the
        # model's — it has the session history and may answer with silence.
        if session_exists:
            return Intent.ENGAGE
    return Intent.ENGAGE if listening and policy.allows_channel(event.channel) else None
