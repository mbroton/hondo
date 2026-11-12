from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import httpx
from mcp.shared.exceptions import McpError as MCPProtocolError
from slack_sdk.errors import SlackApiError

from hondo.mcp.models import MCPError
from hondo.mcp.network import request_not_sent

type RequestEvent = Literal["dispatch", "refused"]


@dataclass(frozen=True)
class NotExecuted:
    error: MCPError


@dataclass(frozen=True)
class Unconfirmed:
    error: MCPError


@dataclass(frozen=True)
class ReadFailed:
    pass


@dataclass(frozen=True)
class Received:
    result: dict[str, Any]


type CallOutcome = NotExecuted | Unconfirmed | ReadFailed | Received


def error_details(error: BaseException) -> list[dict[str, Any]]:
    if isinstance(error, BaseExceptionGroup):
        return [detail for child in error.exceptions for detail in error_details(child)]
    details: dict[str, Any] = {"error_type": type(error).__name__}
    if isinstance(error, MCPError):
        details["error_code"] = error.code
    elif isinstance(error, SlackApiError):
        details["error_code"] = error.response.get("error")
    elif isinstance(error, httpx.HTTPStatusError):
        details["status_code"] = error.response.status_code
    elif isinstance(error, MCPProtocolError):
        details["error_code"] = error.error.code
    return [details]


def _call_failure(error: BaseException) -> NotExecuted | Unconfirmed | None:
    if request_not_sent(error):
        return NotExecuted(
            error if isinstance(error, MCPError) else MCPError("connection_unavailable")
        )
    if isinstance(error, MCPError):
        return Unconfirmed(error)
    if isinstance(error, MCPProtocolError) and error.error.code in (-32700, -32600, -32601, -32602):
        return NotExecuted(MCPError("tool_request_rejected", 400))
    if isinstance(error, BaseExceptionGroup):
        failures = [_call_failure(child) for child in error.exceptions]
        first = next((item for item in failures if item is not None), None)
        if first is not None:
            if all(isinstance(item, NotExecuted) for item in failures):
                return NotExecuted(first.error)
            return Unconfirmed(first.error)
    return None


def call_failure(error: BaseException) -> NotExecuted | Unconfirmed:
    return _call_failure(error) or Unconfirmed(MCPError("connection_unavailable"))


def outcome_result(outcome: CallOutcome) -> dict[str, Any]:
    match outcome:
        case Received(result):
            return result
        case Unconfirmed():
            return {
                "error": "The tool result could not be confirmed. Do not repeat the action. "
                "Ask a person to check the external service."
            }
        case ReadFailed():
            return {
                "error": "The tool did not return a result. It only reads data, "
                "so nothing changed and you can try again."
            }
        case NotExecuted(error):
            return {
                "error": {
                    "sign_in_required": "This connection needs an admin to reconnect it.",
                    "session_ended": (
                        "The service ended the connection before this request ran. "
                        "No action was sent, so you can try again."
                    ),
                    "tool_request_rejected": (
                        "The server rejected this tool request. Check the arguments."
                    ),
                    "access_denied": (
                        "The server rejected this tool request. "
                        "Ask an admin to review account permissions."
                    ),
                    "earlier_action_unconfirmed": (
                        "This action was not sent because an earlier action on this connector "
                        "has an unconfirmed result. Ask a person to check that earlier action "
                        "in the external service before making more requests."
                    ),
                }.get(
                    error.code,
                    "This connection is unavailable. No action was sent. "
                    "Ask an admin to check the connection.",
                )
            }
