"""Tests for the DeepSeek-compatible chat client."""

from __future__ import annotations

import json

import pytest
import requests
import tenacity

from agent.llm import ChatClient, LLMError, RetryableLLMError, parse_json_response


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


class _RaisingResponse:
    """A 200 response whose body is not JSON."""

    status_code = 200
    text = "<html>gateway</html>"

    def json(self) -> dict:
        raise json.JSONDecodeError("Expecting value", "", 0)


class _SequenceSession:
    """Returns a scripted sequence of responses, one per POST."""

    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.calls = 0

    def post(self, url: str, **kwargs):
        self.calls += 1
        return self._responses.pop(0)


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch) -> None:
    """Keep retry tests instant."""
    monkeypatch.setattr("agent.llm.RETRY_WAIT", tenacity.wait_none())


def _ok(content: str = "REDUNDANT") -> _FakeResponse:
    return _FakeResponse(payload={"choices": [{"message": {"content": content}}]})


def test_retries_on_429_then_succeeds() -> None:
    session = _SequenceSession([_FakeResponse(status_code=429, payload={}), _ok()])
    client = ChatClient(api_key="k", session=session)

    assert client.complete([{"role": "user", "content": "x"}]) == "REDUNDANT"
    assert session.calls == 2


def test_retries_on_500_then_succeeds() -> None:
    session = _SequenceSession([_FakeResponse(status_code=503, payload={}), _ok()])
    client = ChatClient(api_key="k", session=session)

    assert client.complete([{"role": "user", "content": "x"}]) == "REDUNDANT"
    assert session.calls == 2


def test_does_not_retry_on_401() -> None:
    session = _SequenceSession([_FakeResponse(status_code=401, payload={})])
    client = ChatClient(api_key="k", session=session)

    with pytest.raises(LLMError, match="401"):
        client.complete([{"role": "user", "content": "x"}])
    assert session.calls == 1


def test_retries_on_malformed_json_body() -> None:
    session = _SequenceSession([_RaisingResponse(), _ok()])
    client = ChatClient(api_key="k", session=session)

    assert client.complete([{"role": "user", "content": "x"}]) == "REDUNDANT"
    assert session.calls == 2


def test_retries_on_empty_content() -> None:
    session = _SequenceSession([_ok(content="   "), _ok()])
    client = ChatClient(api_key="k", session=session)

    assert client.complete([{"role": "user", "content": "x"}]) == "REDUNDANT"
    assert session.calls == 2


def test_retries_on_missing_choices() -> None:
    session = _SequenceSession([_FakeResponse(payload={}), _ok()])
    client = ChatClient(api_key="k", session=session)

    assert client.complete([{"role": "user", "content": "x"}]) == "REDUNDANT"
    assert session.calls == 2


def test_raises_after_exhausting_retries() -> None:
    session = _SequenceSession([_FakeResponse(status_code=429, payload={})] * 3)
    client = ChatClient(api_key="k", session=session, max_retries=3)

    with pytest.raises(RetryableLLMError, match="429"):
        client.complete([{"role": "user", "content": "x"}])
    assert session.calls == 3
