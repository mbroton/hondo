from __future__ import annotations

import os

import structlog

from hondo.config import LogfireConfig

log = structlog.get_logger(__name__)

_configured = False


def configure_observability(config: LogfireConfig | None = None) -> None:
    """Set up Logfire tracing for the whole teammate.

    The `logfire` package is a regular dependency; should it ever be absent,
    Hondo's spans no-op via `logfire-api` and this call is skipped — unless a
    token is configured, which is an error: silently dropping traces someone
    asked for is worse than failing.

    Tracing is off unless `enabled` is set. When on, traces are exported only
    when a Logfire project is reachable (a configured token, LOGFIRE_TOKEN, or
    local `logfire` CLI auth). Instruments Pydantic AI: agent runs, model
    requests, tool calls.

    The process entrypoint calls this before `serve()`. Calls are idempotent.
    """
    global _configured
    if _configured:
        return
    _configured = True

    config = config if config is not None else LogfireConfig()
    if not config.enabled:
        return

    try:
        import logfire
    except ImportError:
        if config.token is not None or os.environ.get("LOGFIRE_TOKEN"):
            raise RuntimeError(
                "A Logfire token is configured but the logfire package is not "
                "installed. Install it before starting the server."
            ) from None
        log.debug("logfire not installed; tracing disabled")
        return

    logfire.configure(
        token=config.token.get_secret_value() if config.token is not None else None,
        service_name=config.service_name,
        send_to_logfire="if-token-present",
        console=logfire.ConsoleOptions() if config.console else False,
    )
    # Images from Slack are never stored, so their bytes stay out of traces.
    logfire.instrument_pydantic_ai(include_binary_content=False)
