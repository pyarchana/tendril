import datetime as dt
import logging
import secrets
import threading
from collections.abc import Callable
from functools import lru_cache

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.models import Garden
from app.services.llm import LLM, OllamaClient
from app.services.plans import garden_now
from app.services.speech import Transcriber, WhisperTranscriber

log = logging.getLogger(__name__)

Clock = Callable[[Garden], dt.datetime]


async def garden_from_token(token: str, session: AsyncSession = Depends(get_session)) -> Garden:
    garden = await session.scalar(select(Garden).where(Garden.checkin_token == token))
    if garden is None:
        raise HTTPException(status_code=404, detail="Unknown garden link")
    return garden


_basic = HTTPBasic(auto_error=False, realm="Tendril setup")


def require_setup_access(credentials: HTTPBasicCredentials | None = Depends(_basic)) -> None:
    """When SETUP_PASSWORD is set, /setup needs it (any username)."""
    password = get_settings().setup_password
    if not password:
        return
    if credentials is None or not secrets.compare_digest(credentials.password.encode(), password.encode()):
        raise HTTPException(
            status_code=401, detail="Setup password required", headers={"WWW-Authenticate": 'Basic realm="Tendril setup"'}
        )


def limit_check_ins(request: Request, garden: Garden = Depends(garden_from_token)) -> None:
    """Cap voice and typed notes per garden, checked before anything is saved or transcribed."""
    wait = request.app.state.checkin_limiter.check(garden.id, get_settings().checkin_limit_per_hour)
    if wait is not None:
        minutes = max(1, round(wait / 60))
        raise HTTPException(
            status_code=429,
            detail=f"That's a lot of notes for one hour. Please try again in about {minutes} minutes.",
            headers={"Retry-After": str(round(wait))},
        )


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


def preload_whisper() -> None:
    """Download and load the whisper model in a daemon thread so startup is not blocked."""

    def run() -> None:
        try:
            _whisper()._load()
            log.info("Whisper model ready")
        except Exception:
            log.exception("Could not preload the whisper model; it will load on the first voice note")

    threading.Thread(target=run, name="whisper-preload", daemon=True).start()
