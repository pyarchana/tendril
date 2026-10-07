"""Speech to text with faster-whisper on the CPU."""

import asyncio
import logging
import threading
from pathlib import Path
from typing import Protocol

from app.config import get_settings

log = logging.getLogger(__name__)


class SpeechError(Exception):
    """Transcription failed or took too long."""


class Transcriber(Protocol):
    async def transcribe(self, path: Path, hint: str = "") -> str: ...


class WhisperTranscriber:
    """Loads the model on first use and keeps it in memory."""

    def __init__(self, model_size: str | None = None, language: str | None = None, timeout: float | None = None):
        settings = get_settings()
        self.model_size = model_size or settings.whisper_model
        chosen = language or settings.whisper_language
        self.language = None if chosen == "auto" else chosen
        self.timeout = timeout or settings.whisper_timeout
        self.download_root = settings.models_dir
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                log.info("Loading faster-whisper model '%s'", self.model_size)
                self._model = WhisperModel(
                    self.model_size, device="cpu", compute_type="int8", download_root=str(self.download_root)
                )
            return self._model

    def _transcribe_sync(self, path: Path, hint: str) -> str:
        segments, _info = self._load().transcribe(
            str(path),
            language=self.language,
            beam_size=1,
            vad_filter=True,
            initial_prompt=hint or None,  # plant names help with spelling
        )
        return " ".join(segment.text.strip() for segment in segments).strip()

    async def transcribe(self, path: Path, hint: str = "") -> str:
        try:
            return await asyncio.wait_for(asyncio.to_thread(self._transcribe_sync, path, hint), self.timeout)
        except TimeoutError as exc:
            raise SpeechError("transcription timed out") from exc
        except Exception as exc:  # decoding errors, missing model download, ...
            log.warning("Transcription failed: %s", exc)
            raise SpeechError(str(exc)) from exc
