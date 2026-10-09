import datetime as dt
import logging
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.deps import Clock, garden_from_token, get_clock, get_llm, get_transcriber, limit_check_ins
from app.models import Garden
from app.services.checkin import record_check_in, replan_after_check_in
from app.services.llm import LLM
from app.services.speech import SpeechError, Transcriber
from app.templating import templates

log = logging.getLogger(__name__)
router = APIRouter()

MAX_SECONDS = 30
MAX_AUDIO_BYTES = 5 * 1024 * 1024
_EXTENSIONS = {
    "audio/webm": ".webm",
    "video/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "video/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
}


@router.get("/c/{token}", response_class=HTMLResponse)
async def checkin_page(request: Request, garden: Garden = Depends(garden_from_token)):
    return templates.TemplateResponse(
        request,
        "checkin.html",
        {"token": garden.checkin_token, "plants": garden.plants, "max_seconds": MAX_SECONDS},
    )


async def _save_audio(audio: UploadFile, garden: Garden, now: dt.datetime) -> Path:
    content_type = (audio.content_type or "").split(";")[0].strip().lower()
    extension = _EXTENSIONS.get(content_type)
    if extension is None:
        raise HTTPException(status_code=415, detail="Please send an audio recording.")
    data = await audio.read(MAX_AUDIO_BYTES + 1)
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="That recording is too long. Keep it under 30 seconds.")
    if not data:
        raise HTTPException(status_code=422, detail="The recording was empty. Hold the button while you talk.")
    path = get_settings().audio_dir / f"garden{garden.id}-{now:%Y%m%d-%H%M%S}{extension}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


async def _finish(
    background: BackgroundTasks,
    session: AsyncSession,
    garden: Garden,
    now: dt.datetime,
    transcript: str,
    audio_path: str | None,
    llm: LLM,
) -> dict:
    result = await record_check_in(session, garden, transcript=transcript, now=now, audio_path=audio_path, llm=llm)
    if result.replan_needed:
        background.add_task(replan_after_check_in, garden.id, now.date(), llm)
    return {
        "message": result.message,
        "transcript": transcript,
        "facts": result.facts.model_dump(),
        "marked": [task.action for task in result.marked],
        "plan_changed": result.replan_needed,
    }


@router.post("/c/{token}/audio", dependencies=[Depends(limit_check_ins)])
async def upload_voice_note(
    background: BackgroundTasks,
    audio: UploadFile = File(...),
    garden: Garden = Depends(garden_from_token),
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
    transcriber: Transcriber = Depends(get_transcriber),
    llm: LLM = Depends(get_llm),
):
    now = clock(garden)
    path = await _save_audio(audio, garden, now)
    hint = "Garden check-in about " + ", ".join(plant.name for plant in garden.plants) + "."
    try:
        transcript = await transcriber.transcribe(path, hint=hint)
    except SpeechError as exc:
        raise HTTPException(status_code=503, detail="Couldn't transcribe that just now. Please try again.") from exc
    if not transcript.strip():
        raise HTTPException(status_code=422, detail="Didn't catch any words. Try again a little closer to the mic.")
    return await _finish(background, session, garden, now, transcript, str(path), llm)


@router.post("/c/{token}/text", dependencies=[Depends(limit_check_ins)])
async def submit_text_note(
    background: BackgroundTasks,
    text: str = Form(..., min_length=2, max_length=500),
    garden: Garden = Depends(garden_from_token),
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
    llm: LLM = Depends(get_llm),
):
    """Typed fallback for when a microphone isn't available."""
    return await _finish(background, session, garden, clock(garden), text.strip(), None, llm)
