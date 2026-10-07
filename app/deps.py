import datetime as dt
from collections.abc import Callable

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Garden
from app.services.plans import garden_now

Clock = Callable[[Garden], dt.datetime]


async def garden_from_token(token: str, session: AsyncSession = Depends(get_session)) -> Garden:
    garden = await session.scalar(select(Garden).where(Garden.checkin_token == token))
    if garden is None:
        raise HTTPException(status_code=404, detail="Unknown garden link")
    return garden


def get_clock() -> Clock:
    """The garden's local time. Overridden in tests."""
    return garden_now
