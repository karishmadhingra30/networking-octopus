"""Thin wrapper around the Anthropic SDK used by enrich/rank/draft/learn.

Centralizes client creation, JSON-from-text parsing (stripping stray code
fences), and model-aware request parameters. Adaptive thinking is only applied
to Opus-tier models — Haiku 4.5 rejects `thinking`/`effort`, so the ranker
(which runs on Haiku) calls Claude plainly.
"""

from __future__ import annotations

import json
import re
from typing import Any

import anthropic

from .config import Config, PROMPTS_DIR

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def load_prompt(name: str) -> str:
    """Read a system prompt file from the package `prompts/` directory."""
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def _supports_adaptive_thinking(model: str) -> bool:
    # Opus 4.6+ and Sonnet 4.6 support adaptive thinking; Haiku 4.5 does not.
    return any(tag in model for tag in ("opus-4-8", "opus-4-7", "opus-4-6", "sonnet-4-6", "fable-5"))


class ClaudeClient:
    def __init__(self, config: Config):
        self._config = config
        self._client = anthropic.Anthropic(api_key=config.anthropic_api_key)

    def complete(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 4096,
    ) -> str:
        """Run a single completion and return the concatenated text output."""
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if _supports_adaptive_thinking(model):
            kwargs["thinking"] = {"type": "adaptive"}

        response = self._client.messages.create(**kwargs)
        return "".join(b.text for b in response.content if b.type == "text").strip()

    def complete_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 4096,
    ) -> Any:
        """Run a completion expected to return JSON, and parse it safely."""
        text = self.complete(
            model=model, system=system, user=user, max_tokens=max_tokens
        )
        return parse_json(text)


def parse_json(text: str) -> Any:
    """Parse JSON that may be wrapped in ```json fences or surrounded by prose."""
    cleaned = text.strip()

    # Strip a leading/trailing fenced block if present.
    if cleaned.startswith("```"):
        cleaned = _FENCE_RE.sub("", cleaned).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Fall back to the first balanced {...} or [...] span.
    span = _first_json_span(cleaned)
    if span is not None:
        return json.loads(span)
    raise ValueError(f"Could not parse JSON from model output:\n{text[:500]}")


def _first_json_span(text: str) -> str | None:
    start_chars = {"{": "}", "[": "]"}
    for i, ch in enumerate(text):
        if ch in start_chars:
            closing = start_chars[ch]
            depth = 0
            in_str = False
            escape = False
            for j in range(i, len(text)):
                c = text[j]
                if in_str:
                    if escape:
                        escape = False
                    elif c == "\\":
                        escape = True
                    elif c == '"':
                        in_str = False
                    continue
                if c == '"':
                    in_str = True
                elif c == ch:
                    depth += 1
                elif c == closing:
                    depth -= 1
                    if depth == 0:
                        return text[i : j + 1]
            return None
    return None
