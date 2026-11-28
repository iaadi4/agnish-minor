"""LLM client interface shared by the live Anthropic client and the offline mock."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol


@dataclass(frozen=True)
class Message:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class LLMRequest:
    stage: str
    system: str
    messages: tuple[Message, ...]
    max_tokens: int = 16000
    #: Free-form key the mock client uses to pick fixtures (usually the user prompt).
    fixture_key: str = ""


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    stop_reason: str | None = None
    request_id: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    #: Extra provenance (e.g. which mock fixture answered, whether a fallback model ran).
    meta: dict[str, str] = field(default_factory=dict)


class LLMError(RuntimeError):
    """Any LLM failure that the pipeline should surface to the user."""

    def __init__(self, message: str, *, retryable: bool = False, stage: str | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.stage = stage


class LLMRefusal(LLMError):
    """The model declined the request (stop_reason == 'refusal')."""


class LLMOutputInvalid(LLMError):
    """The model's output never validated against the stage schema."""


class LLMClient(Protocol):
    @property
    def model_name(self) -> str: ...

    @property
    def mode(self) -> Literal["live", "mock"]: ...

    def complete(self, request: LLMRequest) -> LLMResponse: ...


# ---------------------------------------------------------------------------
# Tool use (parts and netlist stages)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, object]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, object]


@dataclass(frozen=True)
class ToolRequest:
    stage: str
    system: str
    #: Anthropic-shaped messages: {"role": ..., "content": str | list[block]}
    messages: tuple[dict[str, object], ...]
    tools: tuple[ToolSpec, ...]
    max_tokens: int = 32000
    fixture_key: str = ""


@dataclass(frozen=True)
class ToolTurn:
    text: str
    calls: tuple[ToolCall, ...]
    #: assistant content to append verbatim to the history (SDK blocks or dicts)
    content: object
    model: str
    stop_reason: str | None = None
    request_id: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    meta: dict[str, str] = field(default_factory=dict)


class ToolCapable(Protocol):
    def complete_tools(self, request: ToolRequest) -> ToolTurn: ...
