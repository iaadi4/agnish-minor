"""Wrap a live client and save its answers as mock fixtures (``--record-fixtures``)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from .base import LLMClient, LLMRequest, LLMResponse
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
        path = self.root / request.stage / f"{self.name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        doc: dict[str, Any] = (
            json.loads(path.read_text())
            if path.exists()
            else {
                "_note": f"Recorded from {resp.model} (live model output).",
                "match": [],
                "prompt_sha256": fixture_hash(request.fixture_key),
                "responses": [],
            }
        )
        doc["responses"].append(resp.text)
        path.write_text(json.dumps(doc, indent=2) + "\n")
        return resp
