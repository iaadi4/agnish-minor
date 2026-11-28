"""Validated tool loop: the model looks things up, submits, and repairs until the checks pass.

This mirrors copperhead's stage loop (``draft_bom`` / ``draft_schematic``): the model gets
read-only lookup tools over the real KiCad libraries plus one ``submit`` tool. A submission
is parsed with Pydantic, then checked by a deterministic validator; every problem goes
back as an ``is_error`` tool result that names the ref, the pin and the fix. The stage
ends when a submission validates, or when the submission budget runs out (the last
schema-valid submission is kept and its open problems become plan findings).

Clients without tool support (none today, but the protocol allows it) fall back to the
JSON-in-text loop of :func:`generate_structured` with the same validator.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from .audit import AuditLog
from .base import LLMClient, LLMError, LLMOutputInvalid, ToolCall, ToolRequest, ToolSpec
from .structured import _format_errors, extract_json, schema_instructions

T = TypeVar("T", bound=BaseModel)

SUBMIT = "submit"


@dataclass
class Tool:
    spec: ToolSpec
    run: Callable[[dict[str, Any]], str]


@dataclass
class StageResult(Generic[T]):
    value: T
    accepted: bool
    submissions: int
    turns: int
    #: validator problems still open when the budget ran out
    open_problems: list[str] = field(default_factory=list)
    tool_calls: int = 0


def submit_spec(model: type[BaseModel]) -> ToolSpec:
    return ToolSpec(
        name=SUBMIT,
        description=(
            f"Submit the complete {model.__name__} as JSON text (schema in the system prompt). "
            "It is validated against the KiCad libraries and the electrical rules; the result "
            "lists every problem to fix, or says it was accepted. Resubmit the full document "
            "after fixing problems."
        ),
        input_schema={
            "type": "object",
            "properties": {"json": {"type": "string", "description": "the full JSON document"}},
            "required": ["json"],
        },
    )


def _audit_turn(
    audit: AuditLog | None, stage: str, turn_no: int, prompt: str, response: str, **data: str
) -> None:
    if audit:
        audit.log(
            "llm_call",
            stage=stage,
            attempt=turn_no,
            prompt=prompt,
            response=response,
            data=data,
        )


def run_validated_stage(
    client: LLMClient,
    *,
    stage: str,
    output_model: type[T],
    system: str,
    user: str,
    validate: Callable[[T], list[str]],
    tools: list[Tool] | None = None,
    fixture_key: str = "",
    audit: AuditLog | None = None,
    max_submissions: int = 5,
    max_turns: int = 30,
    max_tokens: int = 32000,
) -> StageResult[T]:
    tools = tools or []
    full_system = f"{system}\n\n{schema_instructions(output_model)}"
    complete_tools = getattr(client, "complete_tools", None)
    if complete_tools is None:
        return _text_loop(
            client,
            stage,
            output_model,
            full_system,
            user,
            validate,
            fixture_key,
            audit,
            max_submissions,
            max_tokens,
        )

    by_name = {t.spec.name: t for t in tools}
    specs = (*(t.spec for t in tools), submit_spec(output_model))
    messages: list[dict[str, object]] = [{"role": "user", "content": user}]
    best: T | None = None
    best_problems: list[str] = []
    submissions = 0
    n_tool_calls = 0
    last_prompt = user
    for turn_no in range(1, max_turns + 1):
        turn = complete_tools(
            ToolRequest(
                stage=stage,
                system=full_system,
                messages=tuple(messages),
                tools=specs,
                max_tokens=max_tokens,
                fixture_key=fixture_key or user,
            )
        )
        calls_txt = json.dumps(
            [{"name": c.name, "input": c.input} for c in turn.calls], separators=(",", ":")
        )
        _audit_turn(
            audit,
            stage,
            turn_no,
            last_prompt,
            (turn.text + ("\n" if turn.text else "") + calls_txt) if turn.calls else turn.text,
            model=turn.model,
            stop_reason=str(turn.stop_reason),
            request_id=str(turn.request_id),
            **turn.meta,
            **{k: str(v) for k, v in turn.usage.items()},
        )
        messages.append({"role": "assistant", "content": turn.content})
        if turn.stop_reason == "max_tokens" and any(c.name == SUBMIT for c in turn.calls):
            # a truncated submit carries partial JSON; ask for it again rather than parse it
            cut = [
                _result(
                    c,
                    "Your reply was cut off at max_tokens. Submit again; keep "
                    "notes and rationales short.",
                    True,
                )
                for c in turn.calls
            ]
            messages.append({"role": "user", "content": cut})
            last_prompt = "(truncated submit)"
            continue
        if not turn.calls:
            text_doc = _try_text_submission(turn.text)
            if text_doc is None:
                nudge = f"Call the {SUBMIT} tool with the complete JSON document."
                messages.append({"role": "user", "content": nudge})
                last_prompt = nudge
                continue
            # JSON in plain text: treat it as a submission and answer in plain text
            turn_calls: tuple[ToolCall, ...] = (ToolCall("text", SUBMIT, {"json": text_doc}),)
        else:
            turn_calls = turn.calls
        results: list[dict[str, object]] = []
        accepted: T | None = None
        for call in turn_calls:
            if call.name != SUBMIT:
                n_tool_calls += 1
                tool = by_name.get(call.name)
                if tool is None:
                    results.append(_result(call, f"unknown tool {call.name}", True))
                    continue
                try:
                    results.append(_result(call, tool.run(dict(call.input)), False))
                except Exception as e:  # a tool bug must not end the stage
                    results.append(_result(call, f"tool error: {e}", True))
                continue
            submissions += 1
            raw = call.input.get("json")
            try:
                data = json.loads(raw if isinstance(raw, str) else json.dumps(raw))
                value = output_model.model_validate(data)
            except (ValidationError, ValueError) as e:
                results.append(
                    _result(call, "Schema validation failed:\n" + _format_errors(e), True)
                )
                continue
            problems = validate(value)
            if best is None or len(problems) <= len(best_problems):
                best, best_problems = value, problems
            if problems:
                from ..pipeline.intent import cap_feedback  # local: avoid an import cycle

                left = max_submissions - submissions
                results.append(
                    _result(
                        call,
                        cap_feedback(problems)
                        + f"\nFix them and call {SUBMIT} again ({left} submission(s) left).",
                        True,
                    )
                )
            else:
                accepted = value
                results.append(_result(call, "accepted", False))
        if call_ids_are_text(turn_calls):
            if accepted is None:
                feedback = "\n".join(str(r["content"]) for r in results)
                messages.append({"role": "user", "content": feedback})
                last_prompt = feedback
        else:
            messages.append({"role": "user", "content": results})
            last_prompt = json.dumps([r["content"] for r in results])[:4000]
        if accepted is not None:
            return StageResult(accepted, True, submissions, turn_no, [], n_tool_calls)
        if submissions >= max_submissions:
            break
    if best is None:
        raise LLMOutputInvalid(
            f"stage '{stage}': no schema-valid submission within the turn budget", stage=stage
        )
    return StageResult(best, False, submissions, turn_no, best_problems, n_tool_calls)


def call_ids_are_text(calls: tuple[ToolCall, ...]) -> bool:
    return len(calls) == 1 and calls[0].id == "text"


def _result(call: ToolCall, content: str, is_error: bool) -> dict[str, object]:
    out: dict[str, object] = {"type": "tool_result", "tool_use_id": call.id, "content": content}
    if is_error:
        out["is_error"] = True
    return out


def _try_text_submission(text: str) -> str | None:
    if not text.strip():
        return None
    doc = extract_json(text)
    try:
        json.loads(doc)
    except ValueError:
        return None
    return doc


def _text_loop(
    client: LLMClient,
    stage: str,
    output_model: type[T],
    system: str,
    user: str,
    validate: Callable[[T], list[str]],
    fixture_key: str,
    audit: AuditLog | None,
    max_submissions: int,
    max_tokens: int,
) -> StageResult[T]:
    from .base import LLMRequest, Message

    messages = [Message("user", user)]
    best: T | None = None
    best_problems: list[str] = []
    for attempt in range(1, max_submissions + 1):
        resp = client.complete(
            LLMRequest(stage, system, tuple(messages), max_tokens, fixture_key or user)
        )
        _audit_turn(audit, stage, attempt, messages[-1].content, resp.text, model=resp.model)
        try:
            value = output_model.model_validate(json.loads(extract_json(resp.text)))
        except (ValidationError, ValueError) as e:
            fb = "Schema validation failed:\n" + _format_errors(e)
        else:
            problems = validate(value)
            if best is None or len(problems) <= len(best_problems):
                best, best_problems = value, problems
            if not problems:
                return StageResult(value, True, attempt, attempt)
            from ..pipeline.intent import cap_feedback

            fb = cap_feedback(problems)
        messages += [
            Message("assistant", resp.text),
            Message("user", fb + "\nReply with the corrected JSON only."),
        ]
    if best is None:
        raise LLMOutputInvalid(f"stage '{stage}': output never validated", stage=stage)
    return StageResult(best, False, max_submissions, max_submissions, best_problems)


__all__ = ["LLMError", "StageResult", "Tool", "run_validated_stage", "submit_spec"]
