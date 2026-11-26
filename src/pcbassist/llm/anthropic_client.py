"""Live client for the Anthropic Messages API.

* Model: ``PCBASSIST_MODEL`` env var, default ``claude-sonnet-5-5``.
* Server-side refusal fallback is enabled by default (``fallbacks="default"``); set
  ``PCBASSIST_LLM_FALLBACKS=off`` to disable.
* Transport errors (429/5xx/connection/timeouts) are retried by the SDK (``max_retries``);
  whatever still fails is converted to :class:`LLMError` with a readable message.
* The API key is read by the SDK from the environment and is never logged.
"""

from __future__ import annotations

import os
from typing import Any, Literal

import anthropic

from .base import LLMError, LLMRefusal, LLMRequest, LLMResponse

DEFAULT_MODEL = "claude-sonnet-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicClient:
    def __init__(
        self,
        model: str | None = None,
        *,
        max_retries: int = 4,
        timeout_s: float = 300.0,
        fallbacks: bool | None = None,
        effort: str | None = None,
        client: Any = None,
    ) -> None:
        self._model = model or os.environ.get("PCBASSIST_MODEL") or DEFAULT_MODEL
        if fallbacks is None:
            fallbacks = os.environ.get("PCBASSIST_LLM_FALLBACKS", "default").lower() != "off"
        self._fallbacks = fallbacks
        self._effort = effort or os.environ.get("PCBASSIST_LLM_EFFORT") or None
        self._client = client or anthropic.Anthropic(max_retries=max_retries, timeout=timeout_s)

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def mode(self) -> Literal["live", "mock"]:
        return "live"

    def complete(self, request: LLMRequest) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": request.max_tokens,
            "system": request.system,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        }
        if self._effort:
            kwargs["output_config"] = {"effort": self._effort}
        if self._fallbacks:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        try:
            resp = self._client.beta.messages.create(**kwargs)
        except anthropic.AuthenticationError as e:
            raise LLMError(
                "Anthropic authentication failed - set ANTHROPIC_API_KEY or run with --mock-llm.",
                stage=request.stage,
            ) from e
        except anthropic.NotFoundError as e:
            raise LLMError(
                f"Model '{self._model}' not found - check PCBASSIST_MODEL.", stage=request.stage
            ) from e
        except anthropic.BadRequestError as e:
            raise LLMError(
                f"Anthropic API rejected the request: {e.message}", stage=request.stage
            ) from e
        except anthropic.RateLimitError as e:
            raise LLMError(
                "Rate limited by the Anthropic API after retries; try again later.",
                retryable=True,
                stage=request.stage,
            ) from e
        except anthropic.APIStatusError as e:
            raise LLMError(
                f"Anthropic API error {e.status_code}: {e.message}",
                retryable=e.status_code >= 500,
                stage=request.stage,
            ) from e
        except anthropic.APIConnectionError as e:
            raise LLMError(
                "Could not reach the Anthropic API (network error or timeout).",
                retryable=True,
                stage=request.stage,
            ) from e

        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            cat = getattr(details, "category", None) if details else None
            raise LLMRefusal(
                f"The model declined this request (category: {cat}).", stage=request.stage
            )

        text = "".join(
            getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text"
        )
        usage: dict[str, int] = {}
        if resp.usage is not None:
            for k in ("input_tokens", "output_tokens", "cache_read_input_tokens"):
                v = getattr(resp.usage, k, None)
                if isinstance(v, int):
                    usage[k] = v
        meta: dict[str, str] = {}
        if resp.model and resp.model != self._model:
            meta["served_by"] = str(resp.model)
        return LLMResponse(
            text=text,
            model=str(resp.model or self._model),
            stop_reason=resp.stop_reason,
            request_id=getattr(resp, "_request_id", None),
            usage=usage,
            meta=meta,
        )
