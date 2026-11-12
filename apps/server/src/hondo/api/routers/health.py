from __future__ import annotations

import asyncio

import structlog
from fastapi import APIRouter, HTTPException, Request

from hondo.api.store import ControlStore

logger = structlog.get_logger("hondo.api.health")

router = APIRouter()

# Bounded so a stalled database or exhausted pool degrades the health check
# to a fast 503 instead of hanging the probe.
_PING_TIMEOUT_SECONDS = 5


@router.get("/healthz")
async def healthz(request: Request) -> dict[str, str]:
    store: ControlStore = request.app.state.store
    try:
        async with asyncio.timeout(_PING_TIMEOUT_SECONDS):
            await store.ping()
    except Exception as exc:
        # The cause stays in the logs; the response body never names internals.
        logger.warning("health check failed", exc_info=True)
        raise HTTPException(status_code=503, detail="unavailable") from exc
    return {"status": "ok"}
