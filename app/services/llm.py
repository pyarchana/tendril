"""Thin client for a local Ollama model that must answer in JSON."""

import json
import logging
from typing import Protocol

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)


class LLMError(Exception):
    """The model could not be reached or did not return usable JSON text."""


class LLM(Protocol):
    async def chat_json(self, messages: list[dict], schema: dict | None = None) -> str: ...


class OllamaClient:
    def __init__(self, base_url: str | None = None, model: str | None = None, timeout: float | None = None):
        settings = get_settings()
        self.base_url = (base_url or settings.ollama_url).rstrip("/")
        self.model = model or settings.ollama_model
        self.timeout = timeout or settings.ollama_timeout

    async def chat_json(self, messages: list[dict], schema: dict | None = None) -> str:
        """Send a chat and return the raw reply text. `schema` constrains the output when given."""
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "format": schema or "json",
            "options": {"temperature": 0.2},
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(f"{self.base_url}/api/chat", json=payload)
                response.raise_for_status()
                return response.json()["message"]["content"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            log.warning("Ollama request failed: %s", exc)
            raise LLMError(str(exc)) from exc


def parse_json_reply(text: str) -> dict:
    """Parse a model reply, tolerating code fences or stray prose around the JSON object."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        raise ValueError("reply contains no JSON object")
    return json.loads(text[start : end + 1])
