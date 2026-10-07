from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import Clock, garden_from_token, get_clock
from app.models import Garden, TaskStatus
from app.services.today import set_today_status

router = APIRouter()


async def _settle(request: Request, garden: Garden, session: AsyncSession, clock: Clock, status: TaskStatus):
    count = await set_today_status(session, garden, clock(garden).date(), status)
    if "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(f"/today/{garden.checkin_token}?updated={status}", status_code=303)
    return {"status": status, "updated": count}


@router.post("/t/{token}/done")
async def mark_done(
    request: Request,
    garden: Garden = Depends(garden_from_token),
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
):
    return await _settle(request, garden, session, clock, TaskStatus.done)


@router.post("/t/{token}/skip")
async def mark_skipped(
    request: Request,
    garden: Garden = Depends(garden_from_token),
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
):
    return await _settle(request, garden, session, clock, TaskStatus.skipped)
