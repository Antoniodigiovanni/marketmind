"""LLM researcher agent — hypothesis generation and report writing."""

from __future__ import annotations


class Researcher:
    """Wraps the LLM and orchestrates the research lifecycle.

    Model: claude-sonnet-4-5. Prompt templates loaded from agent/prompts/.
    LLM calls are wrapped with tenacity retry and explicit timeout.
    """

    pass
