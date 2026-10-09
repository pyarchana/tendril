"""Thin client for a local Ollama model that must answer in JSON."""

import json
import logging
from collections.abc import Callable
from typing import Protocol, TypeVar

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

T = TypeVar("T")


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
            # A week plan is ~350 tokens; the cap stops a runaway reply on a slow CPU.
            "options": {"temperature": 0.2, "num_predict": 1024},
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(f"{self.base_url}/api/chat", json=payload)
                response.raise_for_status()
                return response.json()["message"]["content"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            problem = f"{type(exc).__name__}: {exc}".rstrip(": ")  # timeouts have an empty message
            log.warning("Ollama request failed: %s", problem)
            raise LLMError(problem) from exc


def parse_json_reply(text: str) -> dict:
    """Parse the first JSON object in a model reply, ignoring code fences or prose around it."""
    start = text.find("{")
    if start == -1:
        raise ValueError("reply contains no JSON object")
    # raw_decode reads exactly one complete object and stops, so trailing text can't corrupt it.
    value, _end = json.JSONDecoder().raw_decode(text, start)
    return value


async def ask_validated(llm: LLM, messages: list[dict], schema: dict, validate: Callable[[dict], T]) -> T | None:
    """Two attempts; the second one is told what was wrong with the first."""
    for attempt in (1, 2):
        try:
            reply = await llm.chat_json(messages, schema)
        except LLMError as exc:
            log.warning("Model call failed (attempt %d): %s", attempt, exc)
            continue
        try:
            return validate(parse_json_reply(reply))
        except ValueError as exc:  # includes JSON decode errors and pydantic ValidationError
            problem = str(exc)[:500]
            log.info("Model reply invalid (attempt %d): %s", attempt, problem)
            messages = [
                *messages,
                {"role": "assistant", "content": reply},
                {"role": "user", "content": f"That reply was invalid: {problem}\nReply again with only valid JSON in the required shape."},
            ]
    return None
