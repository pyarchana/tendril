import json

import httpx
import pytest
import respx

from app.services.llm import LLMError, OllamaClient, parse_json_reply

CHAT_URL = "http://ollama.test:11434/api/chat"


@respx.mock
async def test_chat_json_sends_model_schema_and_returns_content():
    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, json={"message": {"role": "assistant", "content": '{"ok": true}'}})
    )
    client = OllamaClient(base_url="http://ollama.test:11434/", model="qwen2.5:3b", timeout=5)
    schema = {"type": "object"}

    reply = await client.chat_json([{"role": "user", "content": "hi"}], schema=schema)

    body = json.loads(route.calls.last.request.content)
    assert reply == '{"ok": true}'
    assert body["model"] == "qwen2.5:3b"
    assert body["format"] == schema
    assert body["stream"] is False


@respx.mock
async def test_chat_json_timeout_raises_llm_error():
    respx.post(CHAT_URL).mock(side_effect=httpx.ReadTimeout("slow model"))
    with pytest.raises(LLMError):
        await OllamaClient(base_url="http://ollama.test:11434").chat_json([])


@respx.mock
async def test_chat_json_missing_model_raises_llm_error():
    respx.post(CHAT_URL).mock(return_value=httpx.Response(404, json={"error": "model not found"}))
    with pytest.raises(LLMError):
        await OllamaClient(base_url="http://ollama.test:11434").chat_json([])


def test_parse_json_reply_strips_fences_and_prose():
    assert parse_json_reply('Sure!\n```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_json_reply_rejects_non_json():
    with pytest.raises(ValueError):
        parse_json_reply("no json here")
