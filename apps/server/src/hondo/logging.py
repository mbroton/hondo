from __future__ import annotations

import logging

import structlog

from hondo.config import LoggingConfig

# Libraries whose INFO output is per-request plumbing (every httpx call, MCP
# protocol negotiation), not application events. Capped at WARNING so the
# default INFO level tells the teammate's story — message received, run
# started, answer posted — instead of drowning it. Lower these levels after
# configure_logging() when debugging the underlying protocols.
_NOISY_LOGGERS = ("httpx", "httpx2", "httpcore", "httpcore2", "mcp")


def configure_logging(config: LoggingConfig | None = None, *, is_prod: bool = False) -> None:
    """Set up structlog for the process: text (dev) or json (prod) to stderr.

    Routes stdlib logging through the same renderer, so records from slack_bolt,
    asyncpg, and friends come out in the same shape as hondo's own logs. Chatty
    HTTP/MCP plumbing loggers are capped at WARNING (see _NOISY_LOGGERS).

    The process entrypoint calls this before configuring observability and
    starting `serve()`.
    """
    config = config if config is not None else LoggingConfig()
    resolved = config.resolved_format(is_prod=is_prod)

    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]
    renderer: structlog.typing.Processor
    if resolved == "json":
        shared_processors.append(structlog.processors.format_exc_info)
        renderer = structlog.processors.JSONRenderer()
    else:
        # Never use the default exception formatter: with rich installed it is
        # RichTracebackFormatter(show_locals=True), which pretty-prints every
        # traceback frame's locals. Agent-sized locals (message history, tool
        # schemas, MCP client objects) took ~46s of CPU per exc_info log and
        # froze the whole process — the event loop, Slack ingress included
        # (observed live as "the bot hangs after a failed tool call").
        # Exception rendering must be O(traceback), never O(frame locals).
        renderer = structlog.dev.ConsoleRenderer(exception_formatter=structlog.dev.plain_traceback)

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(config.level)
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
