"""Schema-validated generation with error-feedback retries.

The model is given the JSON Schema of the expected output. Its reply is parsed as JSON and
validated with Pydantic; on failure the concrete validation errors are sent back and the
model gets another attempt (``max_attempts`` total, default 3). Every attempt is written to
the audit log.
"""

from __future__ import annotations

import json
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from .audit import AuditLog
from .base import LLMClient, LLMOutputInvalid, LLMRequest, Message

T = TypeVar("T", bound=BaseModel)

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> str:
    """Pull the JSON object out of a reply that may contain fences or surrounding prose."""
    m = _FENCE.search(text)
    if m:
        return m.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text.strip()


def schema_instructions(model: type[BaseModel]) -> str:
    schema = json.dumps(model.model_json_schema(), separators=(",", ":"))
    return (
        "Respond with a single JSON object and nothing else (no prose, no markdown fences). "
        f"It must validate against this JSON Schema for `{model.__name__}`:\n{schema}"
    )


def _format_errors(err: ValidationError | ValueError) -> str:
    if isinstance(err, ValidationError):
        lines = []
        for e in err.errors()[:25]:
            loc = ".".join(str(x) for x in e["loc"]) or "<root>"
            lines.append(f"- {loc}: {e['msg']}")
        return "\n".join(lines)
    return f"- {err}"


def generate_structured(
    client: LLMClient,
    *,
    stage: str,
    output_model: type[T],
    system: str,
    user: str,
    fixture_key: str = "",
    audit: AuditLog | None = None,
    max_attempts: int = 3,
    max_tokens: int = 16000,
) -> T:
    """Call the LLM until its output validates as ``output_model`` (or attempts run out)."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    full_system = f"{system}\n\n{schema_instructions(output_model)}"
    messages: list[Message] = [Message("user", user)]
    last_error = ""
    for attempt in range(1, max_attempts + 1):
        req = LLMRequest(
            stage=stage,
            system=full_system,
            messages=tuple(messages),
            max_tokens=max_tokens,
            fixture_key=fixture_key or user,
        )
        resp = client.complete(req)
        try:
            if resp.stop_reason == "max_tokens":
                raise ValueError(
                    "response was truncated at max_tokens; return a shorter, complete JSON object"
                )
            data = json.loads(extract_json(resp.text))
            result = output_model.model_validate(data)
        except (ValidationError, ValueError) as e:  # JSONDecodeError is a ValueError
            last_error = _format_errors(e)
            if audit:
                audit.log(
                    "llm_call",
                    stage=stage,
                    model=resp.model,
                    attempt=attempt,
                    prompt=messages[-1].content,
                    response=resp.text,
                    error=f"validation failed:\n{last_error}",
                    data={
                        "request_id": resp.request_id,
                        **resp.meta,
                        **{k: str(v) for k, v in resp.usage.items()},
                    },
                )
            messages += [
                Message("assistant", resp.text),
                Message(
                    "user",
                    "Your previous reply did not validate. Fix these problems and reply "
                    f"with the corrected JSON object only:\n{last_error}",
                ),
            ]
            continue
        if audit:
            audit.log(
                "llm_call",
                stage=stage,
                model=resp.model,
                attempt=attempt,
                prompt=messages[-1].content,
                response=resp.text,
                data={
                    "request_id": resp.request_id,
                    **resp.meta,
                    **{k: str(v) for k, v in resp.usage.items()},
                },
            )
        return result
    raise LLMOutputInvalid(
        f"stage '{stage}': model output failed schema validation after {max_attempts} attempts. "
        f"Last errors:\n{last_error}",
        stage=stage,
    )
