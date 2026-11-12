from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from hondo.api.access import AdminAccess, admin_access
from hondo.api.deps import current_user
from hondo.api.schemas import MeResponse
from hondo.api.store import User

router = APIRouter()


@router.get("/me", responses={401: {"description": "Not signed in"}})
async def me(
    user: Annotated[User, Depends(current_user)],
    access: Annotated[AdminAccess, Depends(admin_access)],
) -> MeResponse:
    return MeResponse.from_domain(user, await access.team(user))
