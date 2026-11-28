"""Wrap a live client and save its answers as mock fixtures (``--record-fixtures``)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from .base import LLMClient, LLMRequest, LLMResponse, ToolRequest, ToolTurn
from .mock import fixture_hash


class RecordingClient:
    """Pass-through client that writes every response to ``<root>/<stage>/<name>.json``."""

    def __init__(self, inner: LLMClient, root: Path, name: str) -> None:
        self.inner = inner
        self.root = root
        self.name = name

    @property
    def model_name(self) -> str:
        return self.inner.model_name

    @property
    def mode(self) -> Literal["live", "mock"]:
        return self.inner.mode

    def complete(self, request: LLMRequest) -> LLMResponse:
        resp = self.inner.complete(request)
        self._append(request.stage, request.fixture_key, resp.model, resp.text)
        return resp

    def complete_tools(self, request: ToolRequest) -> ToolTurn:
        turn: ToolTurn = self.inner.complete_tools(request)  # type: ignore[attr-defined]
        calls = [c for c in turn.calls if c.name != "submit"]
        if calls:
            entry: Any = {"tool_calls": [{"name": c.name, "input": c.input} for c in calls]}
        else:
            sub = next((c for c in turn.calls if c.name == "submit"), None)
            entry = sub.input.get("json") if sub else turn.text
        self._append(request.stage, request.fixture_key, turn.model, entry)
        return turn

    def _append(self, stage: str, key: str, model: str, entry: Any) -> None:
        path = self.root / stage / f"{self.name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        doc: dict[str, Any] = (
            json.loads(path.read_text())
            if path.exists()
            else {
                "_note": f"Recorded from {model} (live model output).",
                "match": [],
                "prompt_sha256": fixture_hash(key),
                "responses": [],
            }
        )
        doc["responses"].append(entry)
        path.write_text(json.dumps(doc, indent=2) + "\n")
