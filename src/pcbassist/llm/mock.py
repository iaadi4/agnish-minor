"""Offline LLM client that answers from JSON fixtures.

Fixture layout: ``fixtures/llm/<stage>/<name>.json`` with::

    {
      "match": ["iot", "wifi", "battery"],      # keywords scored against the fixture key
      "prompt_sha256": "...",                   # optional exact match (sha256 of key)
      "responses": [ "<raw text>", ... ]        # successive answers (for retry tests)
    }

Tool stages (``complete_tools``): each fixture response becomes one assistant turn. A
response shaped ``{"tool_calls": [{"name": ..., "input": {...}}]}`` is replayed as those
tool calls (to exercise lookups); any other response is sent as a ``submit`` call carrying
that JSON document.

Selection: an exact ``prompt_sha256`` match wins; otherwise the fixture with the most
keyword hits (ties broken by name). A ``_default.json`` fixture is used when nothing scores.
If several responses are listed they are returned in order across calls for the same
stage+fixture, which lets tests exercise the validation-retry loop.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Literal

from ..paths import fixtures_dir
from .base import LLMError, LLMRequest, LLMResponse, ToolCall, ToolRequest, ToolTurn

MOCK_MODEL = "mock-llm"


def fixture_hash(key: str) -> str:
    return hashlib.sha256(" ".join(key.lower().split()).encode()).hexdigest()


class MockLLMClient:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or fixtures_dir() / "llm"
        self._cursor: dict[tuple[str, str], int] = defaultdict(int)
        self.calls: list[LLMRequest] = []
        self.tool_calls: list[ToolRequest] = []

    @property
    def model_name(self) -> str:
        return MOCK_MODEL

    @property
    def mode(self) -> Literal["live", "mock"]:
        return "mock"

    def _load(self, stage: str) -> list[tuple[str, dict[str, Any]]]:
        d = self.root / stage
        if not d.is_dir():
            raise LLMError(
                f"mock LLM has no fixtures for stage '{stage}' (looked in {d})", stage=stage
            )
        out = []
        for p in sorted(d.glob("*.json")):
            out.append((p.stem, json.loads(p.read_text())))
        return out

    def select(self, stage: str, key: str) -> tuple[str, dict[str, Any], str]:
        fixtures = self._load(stage)
        h = fixture_hash(key)
        for name, fx in fixtures:
            if fx.get("prompt_sha256") == h:
                return name, fx, "exact"
        words = set(re.findall(r"[a-z0-9][a-z0-9.+\-]*", key.lower()))
        best: tuple[int, str, dict[str, Any]] | None = None
        for name, fx in fixtures:
            if name.startswith("_"):
                continue
            score = sum(
                1
                for kw in fx.get("match", [])
                if kw.lower() in words or (" " in kw and kw.lower() in key.lower())
            )
            if score > 0 and (best is None or score > best[0]):
                best = (score, name, fx)
        if best is not None:
            return best[1], best[2], f"keyword(score={best[0]})"
        for name, fx in fixtures:
            if name == "_default":
                return name, fx, "default"
        raise LLMError(f"mock LLM: no fixture for stage '{stage}' matches this prompt", stage=stage)

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        name, fx, how = self.select(request.stage, request.fixture_key)
        responses = fx.get("responses")
        if not isinstance(responses, list) or not responses:
            raise LLMError(
                f"mock fixture {request.stage}/{name} has no responses", stage=request.stage
            )
        i = self._cursor[(request.stage, name)]
        self._cursor[(request.stage, name)] = i + 1
        raw = responses[min(i, len(responses) - 1)]
        text = raw if isinstance(raw, str) else json.dumps(raw, indent=1)
        return LLMResponse(
            text=text,
            model=MOCK_MODEL,
            stop_reason="end_turn",
            meta={"fixture": f"{request.stage}/{name}", "match": how},
        )

    def complete_tools(self, request: ToolRequest) -> ToolTurn:
        self.tool_calls.append(request)
        name, fx, how = self.select(request.stage, request.fixture_key)
        responses = fx.get("responses")
        if not isinstance(responses, list) or not responses:
            raise LLMError(
                f"mock fixture {request.stage}/{name} has no responses", stage=request.stage
            )
        i = self._cursor[(request.stage, name)]
        self._cursor[(request.stage, name)] = i + 1
        raw = responses[min(i, len(responses) - 1)]
        if isinstance(raw, dict) and "tool_calls" in raw:
            calls = tuple(
                ToolCall(id=f"mock_{i}_{k}", name=str(c["name"]), input=dict(c.get("input", {})))
                for k, c in enumerate(raw["tool_calls"])
            )
        else:
            doc = raw if isinstance(raw, str) else json.dumps(raw)
            calls = (ToolCall(id=f"mock_{i}_0", name="submit", input={"json": doc}),)
        content = [
            {"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in calls
        ]
        return ToolTurn(
            text="",
            calls=calls,
            content=content,
            model=MOCK_MODEL,
            stop_reason="tool_use",
            meta={"fixture": f"{request.stage}/{name}", "match": how},
        )
