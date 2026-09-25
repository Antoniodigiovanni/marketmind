"""Live contract check for the configured chat-completions provider."""

from __future__ import annotations

import pytest

from agent.llm import ChatClient

pytestmark = pytest.mark.live


def test_live_chat_completion_contract() -> None:
    """Validate the configured provider's chat-completions response envelope."""
    content = ChatClient(max_retries=1).complete(
        [{"role": "user", "content": "Reply with OK."}],
        temperature=0.0,
        max_tokens=4,
    )

    assert content.strip()
