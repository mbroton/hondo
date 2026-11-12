from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any
from urllib.parse import quote_plus
from uuid import UUID, uuid4

import httpcore
import httpx
import structlog
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    extract_resource_metadata_from_www_auth,
    extract_scope_from_www_auth,
)
from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata
from mcp.shared.auth_utils import check_resource_allowed, resource_url_from_server_url
from mcp.types import LATEST_PROTOCOL_VERSION

from hondo.crypto import TokenCipher
from hondo.mcp.models import REFRESH_LEASE, MCPConnection, MCPError
from hondo.mcp.network import MAX_RESPONSE_BYTES, public_url, request_not_sent
from hondo.mcp.store import MCPStore

logger = structlog.get_logger("hondo.mcp")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class MCPOAuth:
    def __init__(
        self, store: MCPStore, cipher: TokenCipher, client: httpx.AsyncClient, base_url: str
    ) -> None:
        self.store = store
        self.cipher = cipher
        self.client = client
        self.base_url = base_url.rstrip("/")

    def client_metadata(self) -> dict[str, Any]:
        return {
            "client_id": f"{self.base_url}/api/mcp/client-metadata",
            "client_name": "Hondo",
            "redirect_uris": [f"{self.base_url}/api/mcp/oauth/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }

    async def client_metadata_available(self) -> bool:
        metadata = self.client_metadata()
        try:
            # An HTTPS callback can still be reachable only from the user's browser.
            async with asyncio.timeout(5):
                document = await self.document([metadata["client_id"]])
            return all(document.get(key) == value for key, value in metadata.items())
        except (
            MCPError,
            httpx.HTTPError,
            httpcore.NetworkError,
            httpcore.ProtocolError,
            httpcore.TimeoutException,
            OSError,
            ValueError,
        ):
            return False

    async def document(
        self, urls: list[str], validate: Callable[[dict[str, Any]], None] | None = None
    ) -> dict[str, Any]:
        unavailable: Exception | None = None
        for url in urls:
            try:
                response = await self.client.get(public_url(url), follow_redirects=False)
                if response.is_server_error or response.status_code == 429:
                    response.raise_for_status()
                if not response.is_success:
                    continue
                document = self.json_response(response)
                if validate is not None:
                    validate(document)
            except (
                httpx.HTTPError,
                httpcore.NetworkError,
                httpcore.ProtocolError,
                httpcore.TimeoutException,
                OSError,
            ) as exc:
                unavailable = exc
                continue
            except (ValueError, MCPError):
                continue
            return document
        if unavailable is not None:
            raise MCPError("connection_unavailable") from unavailable
        raise MCPError("oauth_not_supported", 400)

    @staticmethod
    def json_response(response: httpx.Response) -> dict[str, Any]:
        if not response.is_success or len(response.content) > MAX_RESPONSE_BYTES:
            raise MCPError("connection_unavailable")
        data = response.json()
        if not isinstance(data, dict):
            raise MCPError("connection_unavailable")
        return data

    async def start(
        self,
        saved: MCPConnection,
        user_id: UUID,
        session_hash: str,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        scope: str | None = None,
    ) -> str:
        if saved.kind != "mcp":
            raise MCPError("not_mcp", 409)
        async with asyncio.timeout(40):
            async with self.client.stream(
                "POST",
                saved.url,
                headers={"Accept": "application/json, text/event-stream"},
                json={
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": LATEST_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "Hondo", "version": "1.0"},
                    },
                },
                follow_redirects=False,
            ) as response:
                challenge = response

            def validate_resource(document: dict[str, Any]) -> None:
                resource = ProtectedResourceMetadata.model_validate(document)
                resource_id = public_url(document["resource"])
                if not resource.authorization_servers or not check_resource_allowed(
                    resource_url_from_server_url(saved.url), resource_id
                ):
                    raise MCPError("oauth_not_supported", 400)
                public_url(document["authorization_servers"][0])

            resource_doc = await self.document(
                build_protected_resource_metadata_discovery_urls(
                    extract_resource_metadata_from_www_auth(challenge), saved.url
                ),
                validate_resource,
            )
            resource = ProtectedResourceMetadata.model_validate(resource_doc)
            resource_id = resource_doc["resource"]
            issuer = resource_doc["authorization_servers"][0]
            selected_scope = (
                scope
                if scope is not None
                else (
                    extract_scope_from_www_auth(challenge)
                    or " ".join(resource.scopes_supported or [])
                )
            )

            def validate_metadata(document: dict[str, Any]) -> None:
                metadata = OAuthMetadata.model_validate(document)
                if document["issuer"] != issuer or "S256" not in (
                    metadata.code_challenge_methods_supported or []
                ):
                    raise MCPError("oauth_not_supported", 400)
                if "code" not in metadata.response_types_supported or (
                    metadata.grant_types_supported is not None
                    and "authorization_code" not in metadata.grant_types_supported
                ):
                    raise MCPError("oauth_not_supported", 400)
                public_url(str(metadata.authorization_endpoint))
                public_url(str(metadata.token_endpoint))

            metadata_doc = await self.document(
                build_oauth_authorization_server_metadata_discovery_urls(issuer, saved.url),
                validate_metadata,
            )
            metadata = OAuthMetadata.model_validate(metadata_doc)
            authorization_url = public_url(str(metadata.authorization_endpoint))
            token_url = public_url(str(metadata.token_endpoint))
            supported = metadata.token_endpoint_auth_methods_supported or ["client_secret_basic"]
            method: str | None = None
            registration: dict[str, Any] = {}
            if not client_id:
                if (
                    metadata_doc.get("client_id_metadata_document_supported")
                    and "none" in supported
                    and await self.client_metadata_available()
                ):
                    client_id = self.client_metadata()["client_id"]
                    method = "none"
                elif metadata.registration_endpoint:
                    method = next(
                        (
                            item
                            for item in ("none", "client_secret_basic", "client_secret_post")
                            if item in supported
                        ),
                        None,
                    )
                    if method is None:
                        raise MCPError("oauth_not_supported", 400)
                    registration_data = self.client_metadata()
                    registration_data.pop("client_id")
                    registration_data["grant_types"] = ["authorization_code"]
                    if "refresh_token" in (metadata.grant_types_supported or []):
                        registration_data["grant_types"].append("refresh_token")
                    registration_data["token_endpoint_auth_method"] = method
                    if selected_scope:
                        registration_data["scope"] = selected_scope
                    response = await self.client.post(
                        public_url(str(metadata.registration_endpoint)),
                        json=registration_data,
                        follow_redirects=False,
                    )
                    registration = self.json_response(response)
                    client_id = registration.get("client_id")
                    client_secret = registration.get("client_secret")
                    method = registration.get("token_endpoint_auth_method", method)
                else:
                    raise MCPError("oauth_client_required", 400)
            if not isinstance(client_id, str) or not client_id:
                raise MCPError("oauth_not_supported", 400)
            if not method:
                candidates = (
                    ("client_secret_basic", "client_secret_post") if client_secret else ("none",)
                )
                method = next(
                    (item for item in candidates if item in supported),
                    None,
                )
            if method not in supported or method not in (
                "none",
                "client_secret_basic",
                "client_secret_post",
            ):
                raise MCPError("oauth_not_supported", 400)
            if method != "none" and not client_secret:
                raise MCPError("oauth_client_required", 400)
            state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
            challenge_code = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .rstrip(b"=")
                .decode()
            )
            redirect_uri = self.client_metadata()["redirect_uris"][0]
            payload = {
                "workspace_id": str(saved.workspace_id),
                "issuer": issuer,
                "issuer_required": bool(
                    metadata_doc.get("authorization_response_iss_parameter_supported")
                ),
                "resource": resource_id,
                "token_url": token_url,
                "client_id": client_id,
                "client_secret": client_secret,
                "method": method,
                "verifier": verifier,
                "redirect_uri": redirect_uri,
                "scope": selected_scope,
            }
            await self.store.begin_oauth(
                saved,
                user_id,
                session_hash,
                digest(state),
                self.cipher.encrypt(json.dumps(payload)),
            )
            params = {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "state": state,
                "code_challenge": challenge_code,
                "code_challenge_method": "S256",
                "resource": resource_id,
            }
            if selected_scope:
                params["scope"] = selected_scope
            return str(httpx.URL(authorization_url).copy_merge_params(params))

    async def exchange(self, config: dict[str, Any], data: dict[str, str]) -> dict[str, Any]:
        data = {**data, "resource": config["resource"], "client_id": config["client_id"]}
        auth: httpx.Auth = httpx.Auth()
        if config["method"] == "client_secret_basic":
            auth = httpx.BasicAuth(
                quote_plus(config["client_id"]), quote_plus(config["client_secret"] or "")
            )
        elif config["method"] == "client_secret_post":
            data["client_secret"] = config["client_secret"] or ""
        response = await self.client.post(
            public_url(config["token_url"]),
            data=data,
            auth=auth,
            follow_redirects=False,
        )
        if response.status_code in (400, 401, 403):
            raise MCPError("sign_in_required", 409)
        tokens = self.json_response(response)
        if (
            not isinstance(tokens.get("access_token"), str)
            or not tokens["access_token"]
            or str(tokens.get("token_type", "")).lower() != "bearer"
        ):
            raise MCPError("oauth_not_supported", 400)
        expires_in = tokens.get("expires_in")
        tokens["expires_at"] = time.time() + float(expires_in) if expires_in is not None else None
        return tokens

    async def complete(
        self,
        attempt: dict[str, Any],
        user_id: UUID,
        code: str | None,
        issuer: str | None,
        error: str | None,
    ) -> MCPConnection:
        payload = json.loads(self.cipher.decrypt(attempt["payload"]))
        if (payload["issuer_required"] and not issuer) or (issuer and issuer != payload["issuer"]):
            raise MCPError("authorization_invalid", 400)
        if error or not code:
            raise MCPError("authorization_cancelled", 400)
        tokens = await self.exchange(
            payload,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": payload["redirect_uri"],
                "code_verifier": payload.pop("verifier"),
            },
        )
        credentials = self.cipher.encrypt(json.dumps({"config": payload, "tokens": tokens}))
        return await self.store.authorize(
            attempt["connection_id"],
            attempt["credential_version"],
            user_id,
            credentials,
        )

    async def access_token(
        self, saved: MCPConnection, *, rejected_credentials: str | None = None
    ) -> tuple[MCPConnection, str]:
        version = saved.credential_version
        deadline = time.monotonic() + REFRESH_LEASE.total_seconds()
        while time.monotonic() < deadline:
            saved = await self.store.reload_credentials(saved)
            if saved.credential_version != version:
                raise MCPError("connection_access_changed", 409)
            if not saved.credentials or saved.status == "sign_in_required":
                raise MCPError("sign_in_required", 409)
            credential = json.loads(self.cipher.decrypt(saved.credentials))
            tokens = credential["tokens"]
            expiry = tokens.get("expires_at")
            rejected = saved.credentials == rejected_credentials
            if not rejected and (expiry is None or float(expiry) > time.time()):
                return saved, tokens["access_token"]
            if not tokens.get("refresh_token"):
                await self.store.record_status(saved, "sign_in_required")
                raise MCPError("sign_in_required", 409)
            claim = uuid4()
            acquire = asyncio.create_task(self.store.claim_refresh(saved, claim))
            try:
                claimed = await asyncio.shield(acquire)
            except asyncio.CancelledError:

                async def release_unsent() -> None:
                    if await acquire:
                        await self.store.release_refresh(saved, claim)

                try:
                    await asyncio.shield(release_unsent())
                except Exception as exc:
                    logger.warning("mcp refresh cleanup failed", error_type=type(exc).__name__)
                raise
            if claimed:
                try:
                    refreshed = await self.exchange(
                        credential["config"],
                        {
                            "grant_type": "refresh_token",
                            "refresh_token": tokens["refresh_token"],
                        },
                    )
                    refreshed.setdefault("refresh_token", tokens["refresh_token"])
                    credential["tokens"] = refreshed
                    encrypted = self.cipher.encrypt(json.dumps(credential))
                except BaseException as exc:
                    if request_not_sent(exc):
                        await asyncio.shield(self.store.release_refresh(saved, claim))
                        raise
                    await asyncio.shield(self.store.finish_refresh(saved, claim, None))
                    if not isinstance(exc, Exception):
                        raise
                    raise MCPError("sign_in_required", 409) from exc
                if not await self.store.finish_refresh(saved, claim, encrypted):
                    raise MCPError("connection_changed", 409)
                current = await self.store.reload_credentials(saved)
                if current.credential_version != version:
                    raise MCPError("connection_access_changed", 409)
                return replace(saved, credentials=encrypted, status="ready"), refreshed[
                    "access_token"
                ]
            await asyncio.sleep(0.25)
        raise MCPError("connection_unavailable")
