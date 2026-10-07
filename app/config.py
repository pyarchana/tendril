from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from the environment or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    public_base_url: str = "http://localhost:8000"

    data_dir: Path = Path("./data")
    database_url: str = "sqlite+aiosqlite:///./data/tendril.db"

    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:3b"
    ollama_timeout: float = 300.0  # a CPU-only first call loads the model and reads the whole prompt

    whisper_model: str = "small"
    whisper_language: Literal["en", "hi", "auto"] = "auto"
    whisper_timeout: float = 180.0
    whisper_preload: bool = False  # load (and download) the model at startup instead of on the first note

    weather_timeout: float = 10.0

    scheduler_enabled: bool = True
    morning_hour: int = 7
    setup_password: str = ""
    log_level: str = "INFO"

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.images_dir, self.audio_dir, self.models_dir):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
