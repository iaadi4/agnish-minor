"""Append-only JSONL audit trail for LLM calls and review decisions.

Secrets are redacted before anything is written: Anthropic keys, generic bearer tokens and
anything that looks like ``api_key=...``.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

from ..schemas import AuditEntry

_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{16,}"),
    re.compile(
        r"(?i)((?:api[_-]?key|x-api-key|authorization)[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]{8,}"
    ),
]


def redact(text: str) -> str:
    out = _SECRET_PATTERNS[0].sub("[REDACTED]", text)
    out = _SECRET_PATTERNS[1].sub(lambda m: m.group(1) + "[REDACTED]", out)
    out = _SECRET_PATTERNS[2].sub(lambda m: m.group(1) + "[REDACTED]", out)
    return out


def _redact_any(v: Any) -> Any:
    if isinstance(v, str):
        return redact(v)
    if isinstance(v, dict):
        return {k: _redact_any(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_redact_any(x) for x in v]
    return v


class AuditLog:
    """Thread-safe JSONL writer. ``path=None`` keeps entries in memory only."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.entries: list[AuditEntry] = []
        self._lock = threading.Lock()
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, entry: AuditEntry) -> AuditEntry:
        clean = AuditEntry.model_validate(_redact_any(entry.model_dump(mode="json")))
        with self._lock:
            self.entries.append(clean)
            if self.path is not None:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(clean.model_dump_json() + "\n")
        return clean

    def log(self, kind: str, **fields: Any) -> AuditEntry:
        return self.record(AuditEntry.model_validate({"kind": kind, **fields}))

    @staticmethod
    def read(path: Path) -> list[AuditEntry]:
        if not path.exists():
            return []
        return [
            AuditEntry.model_validate_json(line) for line in path.read_text().splitlines() if line
        ]
