"""Human review: comments, decisions and the audit trail."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import Field

from .common import Model

TargetKind = Literal[
    "component",
    "net",
    "constraint",
    "rail",
    "net_class",
    "placement",
    "routing",
    "requirements",
    "architecture",
    "plan",
]


def utcnow() -> datetime:
    return datetime.now(UTC)


class ItemRef(Model):
    kind: TargetKind
    id: str = Field(description="ref_des, net name, rail name ... ('*' for the whole section)")

    def key(self) -> str:
        return f"{self.kind}:{self.id}"


class ReviewComment(Model):
    id: str
    target: ItemRef
    author: str = "reviewer"
    text: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=utcnow)
    resolved: bool = False


class ReviewDecision(Model):
    id: str
    target: ItemRef
    decision: Literal["approve", "reject", "edit", "lock", "unlock"]
    author: str = "reviewer"
    note: str = ""
    edit: dict[str, Any] | None = Field(
        default=None, description="For 'edit': field -> new value, applied to the target item"
    )
    created_at: datetime = Field(default_factory=utcnow)


class AuditEntry(Model):
    timestamp: datetime = Field(default_factory=utcnow)
    kind: Literal["llm_call", "stage", "review", "check", "export", "info"]
    stage: str | None = None
    model: str | None = None
    attempt: int | None = None
    prompt: str | None = None
    response: str | None = None
    error: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
