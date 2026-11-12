"""HTTP tools: one admin-defined request per tool.

The admin fixes the method, the address and the shape of the request. The
model gives only parameter values. Each value is encoded for its place in the
request, so it cannot change the scheme, the host or the path structure.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from hondo.mcp.models import HTTPRequest
from hondo.mcp.network import public_client, public_url

PLACEHOLDER_NAME = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"
PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]{0,63})\}")
MAX_VALUE_CHARS = 8192
TEST_BODY_CHARS = 20_000


class ArgumentError(ValueError):
    """The arguments do not fit the tool. The message is safe to show."""


def placeholders(text: str | None) -> set[str]:
    return set(PLACEHOLDER.findall(text or ""))


def check_template(url: str, body: str | None, params: set[str]) -> None:
    """Raise ValueError when a definition can let a value change the address or the body shape."""
    literal = PLACEHOLDER.sub("", url)
    if "{" in literal or "}" in literal:
        raise ValueError("braces in the URL are only for parameters")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc or "#" in url:
        raise ValueError("the URL needs a scheme and a host, and no fragment")
    if placeholders(parts.scheme + parts.netloc):
        raise ValueError("parameters cannot be in the scheme or the host")
    if placeholders(url) | placeholders(body) != params:
        raise ValueError("each parameter must be used, and each placeholder described")
    if body is not None:
        try:
            json.loads(body)
        except ValueError as exc:
            raise ValueError("the body must be JSON, with each parameter inside a string") from exc


def same_origin(url: str, base: str) -> bool:
    """Whether a tool URL keeps the connector's scheme, host and port, and passes the guard."""
    sample = httpx.URL(PLACEHOLDER.sub("x", url))
    expected = httpx.URL(base)
    if (sample.scheme, sample.host, sample.port) != (
        expected.scheme,
        expected.host,
        expected.port,
    ):
        return False
    public_url(str(sample))
    return True


def _text(name: str, value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if not isinstance(value, str):
        raise ArgumentError(f"Give {name} as a string or a number.")
    if len(value) > MAX_VALUE_CHARS:
        raise ArgumentError(f"{name} is too long.")
    return value


def build_request(request: HTTPRequest, arguments: Mapping[str, Any]) -> tuple[str, bytes | None]:
    """The URL and the body for these arguments."""
    names = {param.name for param in request.params}
    if set(arguments) != names:
        expected = ", ".join(sorted(names)) or "no arguments"
        raise ArgumentError(f"Give exactly these arguments: {expected}.")
    values = {name: _text(name, value) for name, value in arguments.items()}

    def segment(text: str) -> str:
        filled = PLACEHOLDER.sub(lambda match: quote(values[match[1]], safe=""), text)
        if filled != text and filled in ("", ".", ".."):
            raise ArgumentError("A path value cannot be empty, '.' or '..'.")
        return filled

    parts = urlsplit(request.url)
    path = "/".join(segment(text) for text in parts.path.split("/"))
    query = PLACEHOLDER.sub(lambda match: quote(values[match[1]], safe=""), parts.query)
    url = urlunsplit((parts.scheme, parts.netloc, path, query, ""))
    sent = httpx.URL(url)
    expected = httpx.URL(PLACEHOLDER.sub("x", request.url))
    if (sent.scheme, sent.host, sent.port) != (expected.scheme, expected.host, expected.port):
        raise ArgumentError("These values change the address of the request.")
    body = None
    if request.body is not None:
        body = PLACEHOLDER.sub(
            lambda match: json.dumps(values[match[1]])[1:-1], request.body
        ).encode()
    return url, body


async def send(
    request: HTTPRequest, arguments: Mapping[str, Any], headers: Mapping[str, str]
) -> httpx.Response:
    url, body = build_request(request, arguments)
    sent = dict(headers)
    if body is not None:
        sent["Content-Type"] = "application/json"
    async with public_client() as client:
        return await client.request(request.method, url, content=body, headers=sent)


REDACTED = "[redacted]"


def response_result(response: httpx.Response, secrets: Iterable[str] = ()) -> dict[str, Any]:
    """The status and the body, with a JSON body parsed and header values redacted."""
    try:
        text = response.content.decode(response.encoding or "utf-8")
    except (UnicodeDecodeError, LookupError):
        return {
            "status": response.status_code,
            "body": f"[{len(response.content)} bytes of binary content]",
        }
    hidden = sorted({value for value in secrets if value}, key=len, reverse=True)
    if "json" in response.headers.get("content-type", ""):
        try:
            return {"status": response.status_code, "body": _redact(json.loads(text), hidden)}
        except ValueError:
            pass
    return {"status": response.status_code, "body": _redact(text, hidden)}


def _redact(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, REDACTED)
        return value
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {_redact(key, secrets): _redact(item, secrets) for key, item in value.items()}
    return value


def shown_body(response: httpx.Response, secrets: Iterable[str] = ()) -> str:
    body = response_result(response, secrets)["body"]
    text = body if isinstance(body, str) else json.dumps(body, indent=2, ensure_ascii=False)
    return text if len(text) <= TEST_BODY_CHARS else text[: TEST_BODY_CHARS - 1] + "…"
