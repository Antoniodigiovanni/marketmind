"""OpenAI-compatible chat client (DeepSeek-compatible) for extraction calls."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import requests
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_MAX_RETRIES = 3

RETRYABLE = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
)


class LLMError(RuntimeError):
    """Raised when the chat API call fails after retries or returns an error."""


class ChatClient:
    """Thin client for the OpenAI-compatible chat completions endpoint.

    Configured via env vars: AI_API_KEY (or LLM_API_KEY),
    LLM_BASE_URL (default https://api.deepseek.com), LLM_MODEL
    (default deepseek-chat). A session may be injected for tests.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        session: requests.Session | None = None,
    ) -> None:
        """Initialise the client from explicit args or environment variables."""
        self.api_key = api_key or os.getenv("AI_API_KEY") or os.getenv("LLM_API_KEY")
        if not self.api_key:
            raise LLMError("no API key: set AI_API_KEY or LLM_API_KEY")
        self.base_url = (base_url or os.getenv("LLM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or os.getenv("LLM_MODEL") or DEFAULT_MODEL
        self.timeout_seconds = int(os.getenv("LLM_TIMEOUT_SECONDS", timeout_seconds))
        self.max_retries = int(os.getenv("LLM_MAX_RETRIES", max_retries))
        self._session = session or requests.Session()

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> str:
        """Send a chat completion request and return the assistant text.

        Args:
            messages: OpenAI-style message list (role/content).
            temperature: Sampling temperature.
            json_mode: Request a JSON object response via response_format.
            max_tokens: Optional token cap for the response.

        Returns:
            The assistant message content.

        Raises:
            LLMError: On HTTP errors or exhausted retries.
        """
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens

        retry = Retrying(
            retry=retry_if_exception_type(RETRYABLE),
            wait=wait_exponential(multiplier=0.5, max=8),
            stop=stop_after_attempt(self.max_retries),
            reraise=True,
        )
        for attempt in retry:
            with attempt:
                response = self._session.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                    timeout=self.timeout_seconds,
                )
                if response.status_code >= 400:
                    raise LLMError(f"chat API error {response.status_code}: {response.text[:500]}")
                data = response.json()
                return str(data["choices"][0]["message"]["content"])
        raise LLMError("chat API request failed")  # pragma: no cover - unreachable


def parse_json_response(content: str) -> Any:
    """Parse a model response, stripping optional markdown code fences.

    Args:
        content: Raw assistant text.

    Returns:
        Parsed JSON value.

    Raises:
        ValueError: If the text is not valid JSON.
    """
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)
