"""Exception rendering must be O(traceback), never O(frame locals).

Regression guard for a live incident: structlog's default dev exception
formatter (rich, show_locals=True) pretty-printed every traceback frame's
locals. With agent-sized locals (message history, tool schemas, MCP clients)
one exc_info log burned ~46s of CPU on the event loop and froze the whole
process — Slack ingress included.
"""

import io
import logging

import structlog

from hondo.logging import configure_logging


class LoudRepr:
    """A frame local whose repr must never run during log rendering."""

    def __init__(self) -> None:
        self.repr_calls = 0

    def __repr__(self) -> str:
        self.repr_calls += 1
        return "LoudRepr()"


def test_noisy_library_loggers_are_capped_at_warning() -> None:
    root = logging.getLogger()
    old_handlers = root.handlers[:]
    old_levels = {
        name: logging.getLogger(name).level
        for name in ("httpx", "httpx2", "httpcore", "httpcore2", "mcp")
    }
    try:
        structlog.reset_defaults()
        configure_logging()
        for name in old_levels:
            assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING, name
    finally:
        structlog.reset_defaults()
        root.handlers.clear()
        root.handlers.extend(old_handlers)
        for name, level in old_levels.items():
            logging.getLogger(name).setLevel(level)


def test_exc_info_rendering_never_touches_frame_locals() -> None:
    root = logging.getLogger()
    old_handlers = root.handlers[:]
    try:
        structlog.reset_defaults()
        configure_logging()
        stream = io.StringIO()
        assert root.handlers, "configure_logging must install a handler"
        root.handlers[0].stream = stream  # type: ignore[attr-defined]

        canary = LoudRepr()

        def failing_tool_call() -> None:
            heavy_local = canary  # noqa: F841  # stand-in for RunContext/history
            raise ValueError("MCP error -32603: 404")

        logger = structlog.get_logger("hondo.test")
        try:
            failing_tool_call()
        except ValueError:
            logger.warning("tool failed", exc_info=True)

        output = stream.getvalue()
        assert "MCP error -32603: 404" in output
        assert "failing_tool_call" in output  # a real traceback was rendered
        assert canary.repr_calls == 0  # ...without touching frame locals
    finally:
        structlog.reset_defaults()
        root.handlers.clear()
        root.handlers.extend(old_handlers)
