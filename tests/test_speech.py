import time

import pytest

from app.services.speech import SpeechError, WhisperTranscriber


def test_language_setting(monkeypatch):
    monkeypatch.setenv("WHISPER_LANGUAGE", "hi")
    from app.config import get_settings

    get_settings.cache_clear()
    assert WhisperTranscriber().language == "hi"
    assert WhisperTranscriber(language="auto").language is None
    assert WhisperTranscriber(language="en").language == "en"


async def test_transcribe_runs_off_the_event_loop(tmp_path):
    transcriber = WhisperTranscriber()
    transcriber._transcribe_sync = lambda path, hint: f"heard {path.name} ({hint})"
    assert await transcriber.transcribe(tmp_path / "note.webm", hint="Tulsi") == "heard note.webm (Tulsi)"


async def test_errors_become_speech_error(tmp_path):
    transcriber = WhisperTranscriber()

    def broken(path, hint):
        raise RuntimeError("Invalid data found when processing input")

    transcriber._transcribe_sync = broken
    with pytest.raises(SpeechError, match="Invalid data"):
        await transcriber.transcribe(tmp_path / "note.webm")


async def test_slow_transcription_times_out(tmp_path):
    transcriber = WhisperTranscriber(timeout=0.05)
    transcriber._transcribe_sync = lambda path, hint: time.sleep(0.3) or "late"
    with pytest.raises(SpeechError, match="timed out"):
        await transcriber.transcribe(tmp_path / "note.webm")
