"""LLM access: live Anthropic client, offline mock, validated structured generation, audit."""

from __future__ import annotations

from .audit import AuditLog, redact
from .base import (
    LLMClient,
    LLMError,
    LLMOutputInvalid,
    LLMRefusal,
    LLMRequest,
    LLMResponse,
    Message,
)
from .mock import MockLLMClient
from .structured import extract_json, generate_structured


def make_client(mock: bool, model: str | None = None) -> LLMClient:
    """Factory used by the CLI/API. The live client is imported lazily."""
    if mock:
        return MockLLMClient()
    from .anthropic_client import AnthropicClient

    return AnthropicClient(model=model)


__all__ = [
    "AuditLog",
    "LLMClient",
    "LLMError",
    "LLMOutputInvalid",
    "LLMRefusal",
    "LLMRequest",
    "LLMResponse",
    "Message",
    "MockLLMClient",
    "extract_json",
    "generate_structured",
    "make_client",
    "redact",
]
