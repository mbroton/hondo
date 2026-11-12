from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import AsyncIterator, Iterable
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any, cast

import httpcore
import httpx
from pydantic import AnyHttpUrl, TypeAdapter

from hondo.mcp.models import MCPError

MAX_RESPONSE_BYTES = 2_000_000
_HTTP_URL = TypeAdapter(AnyHttpUrl)
_private_networks = False


def allow_private_networks(allowed: bool) -> None:
    """Let MCP servers use private addresses and plain HTTP, for self-hosted installs."""
    global _private_networks
    _private_networks = allowed


def request_not_sent(error: BaseException) -> bool:
    return isinstance(
        error,
        (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.PoolTimeout,
            httpcore.ConnectError,
            httpcore.ConnectTimeout,
            httpcore.PoolTimeout,
        ),
    ) or (isinstance(error, MCPError) and error.code == "invalid_server_url")


def public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if not address.is_global:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        return (
            address in ipaddress.IPv6Network("2000::/3")
            and address.sixtofour is None
            and address.teredo is None
        )
    return True


_METADATA_ADDRESSES = frozenset(
    ipaddress.ip_address(value) for value in ("fd00:ec2::254", "100.100.100.200", "192.0.0.192")
)


def metadata_address(value: str) -> bool:
    """Whether an address is link-local, where clouds serve instance metadata and credentials."""
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_link_local or address in _METADATA_ADDRESSES


def public_url(value: str) -> str:
    try:
        url = httpx.URL(value.strip())
        browser_url = _HTTP_URL.validate_python(str(url))
        host = (browser_url.host or "").removeprefix("[").removesuffix("]")
        request_host = url.raw_host.decode("ascii")
        if ":" in request_host:
            request_host = str(ipaddress.IPv6Address(request_host))
        if (
            url.scheme not in ("https", "http")
            or "\\" in value
            or not host
            or host != request_host
            or (browser_url.path or "/") != url.raw_path.split(b"?", 1)[0].decode("ascii")
            or url.userinfo
            or url.fragment
        ):
            raise ValueError
        name = host.lower().rstrip(".")
        if name == "metadata.google.internal":
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and metadata_address(str(address)):
            raise ValueError
        if _private_networks:
            return str(url)
        if url.scheme != "https" or name == "localhost":
            raise ValueError
        if address is not None and not public_address(str(address)):
            raise ValueError
        return str(url)
    except (ValueError, httpx.InvalidURL) as exc:
        raise MCPError("invalid_server_url", 422) from exc


class PublicNetwork(httpcore.AsyncNetworkBackend):
    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        budget = timeout if timeout is not None else 10
        try:
            async with asyncio.timeout(budget):
                addresses = await asyncio.get_running_loop().getaddrinfo(
                    host, port, type=socket.SOCK_STREAM
                )
                ips = list(dict.fromkeys(str(item[4][0]) for item in addresses))
                if (
                    not ips
                    or any(metadata_address(ip) for ip in ips)
                    or (not _private_networks and any(not public_address(ip) for ip in ips))
                ):
                    raise MCPError("invalid_server_url", 422)
                # Connect only to checked addresses; TLS uses the original hostname.
                backend = cast(httpcore.AsyncNetworkBackend, httpcore.AnyIOBackend())
                for index, ip in enumerate(ips):
                    try:
                        return await backend.connect_tcp(
                            ip, port, budget / len(ips), local_address, socket_options
                        )
                    except (httpcore.ConnectError, httpcore.ConnectTimeout):
                        if index == len(ips) - 1:
                            raise
        except TimeoutError as exc:
            raise httpcore.ConnectTimeout() from exc
        except socket.gaierror as exc:
            raise httpcore.ConnectError() from exc
        raise httpcore.ConnectError()


class LimitedStream(httpx.AsyncByteStream):
    def __init__(self, response: httpcore.Response) -> None:
        self.response = response

    async def __aiter__(self) -> AsyncIterator[bytes]:
        size = 0
        async for chunk in self.response.aiter_stream():
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise MCPError("response_too_large")
            yield chunk

    async def aclose(self) -> None:
        await self.response.aclose()


class PublicTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.pool = httpcore.AsyncConnectionPool(
            network_backend=PublicNetwork(), max_connections=10, retries=0
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        public_url(str(request.url))
        response = await self.pool.handle_async_request(
            httpcore.Request(
                method=request.method,
                url=httpcore.URL(
                    scheme=request.url.raw_scheme,
                    host=request.url.raw_host,
                    port=request.url.port,
                    target=request.url.raw_path,
                ),
                headers=request.headers.raw,
                content=request.stream,
                extensions=request.extensions,
            )
        )
        if any(
            value.strip().lower() != b"identity"
            for name, value in response.headers
            if name.lower() == b"content-encoding"
        ):
            await response.aclose()
            raise MCPError("connection_unavailable")
        return httpx.Response(
            response.status,
            headers=response.headers,
            stream=LimitedStream(response),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self.pool.aclose()


def public_client(**kwargs: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=PublicTransport(),
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(20, connect=10),
        headers={"Accept-Encoding": "identity"},
        cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])),
        **kwargs,
    )
