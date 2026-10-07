import datetime as dt
from collections.abc import Callable
from functools import lru_cache

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Garden
from app.services.llm import LLM, OllamaClient
from app.services.plans import garden_now
from app.services.speech import Transcriber, WhisperTranscriber

Clock = Callable[[Garden], dt.datetime]


async def garden_from_token(token: str, session: AsyncSession = Depends(get_session)) -> Garden:
    garden = await session.scalar(select(Garden).where(Garden.checkin_token == token))
    if garden is None:
        raise HTTPException(status_code=404, detail="Unknown garden link")
    return garden


def get_clock() -> Clock:
    """The garden's local time. Overridden in tests."""
    return garden_now


def get_llm() -> LLM:
    return OllamaClient()


@lru_cache
def _whisper() -> WhisperTranscriber:
    return WhisperTranscriber()


def get_transcriber() -> Transcriber:
    """One shared model instance, loaded on the first voice note."""
    return _whisper()
