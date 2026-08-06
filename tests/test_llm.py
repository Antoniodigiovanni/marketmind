"""Tests for the DeepSeek-compatible chat client."""

from __future__ import annotations

import json

import pytest
import requests

from agent.llm import ChatClient, LLMError, parse_json_response


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self) -> dict:
        return self._payload or {}


def test_client_requires_api_key(monkeypatch) -> None:
    monkeypatch.delenv("AI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    with pytest.raises(LLMError, match="API key"):
        ChatClient(api_key=None)


def test_complete_posts_chat_completions_and_returns_content() -> None:
    captured: dict = {}

    class _Session:
        def post(self, url: str, **kwargs) -> _FakeResponse:
            captured["url"] = url
            captured["headers"] = kwargs["headers"]
            captured["json"] = kwargs["json"]
            return _FakeResponse(payload={"choices": [{"message": {"content": "REDUNDANT"}}]})

    client = ChatClient(api_key="test-key", base_url="https://api.deepseek.com", session=_Session())

    content = client.complete(
        [{"role": "user", "content": "extract facts"}],
        json_mode=True,
        max_tokens=512,
    )

    assert content == "REDUNDANT"
    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    body = captured["json"]
    assert body["model"] == "deepseek-chat"
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_tokens"] == 512
    assert body["temperature"] == 0.0


def test_complete_retries_transient_errors_then_succeeds() -> None:
    attempts = 0

    class _Session:
        def post(self, url: str, **kwargs) -> _FakeResponse:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise requests.exceptions.ConnectionError("boom")
            return _FakeResponse(payload={"choices": [{"message": {"content": "[]"}}]})

    client = ChatClient(api_key="test-key", session=_Session(), max_retries=3)

    assert client.complete([{"role": "user", "content": "hi"}]) == "[]"
    assert attempts == 2


def test_complete_raises_on_http_error_without_retry() -> None:
    attempts = 0

    class _Session:
        def post(self, url: str, **kwargs) -> _FakeResponse:
            nonlocal attempts
            attempts += 1
            return _FakeResponse(status_code=401, payload={"error": {"message": "bad key"}})

    client = ChatClient(api_key="test-key", session=_Session())

    with pytest.raises(LLMError, match="401"):
        client.complete([{"role": "user", "content": "hi"}])
    assert attempts == 1


def test_parse_json_response_strips_fences() -> None:
    raw = '```json\n{"statement": "CEO is Tim Cook."}\n```'

    parsed = parse_json_response(raw)

    assert parsed == {"statement": "CEO is Tim Cook."}
