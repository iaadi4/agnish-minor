"""Schemas for what the LLM is allowed to return at each stage.

These are intentionally narrower than the plan schemas: the LLM names parts and roles,
never pins or coordinates.
"""

from __future__ import annotations

from pydantic import Field

from .common import Confidence, Model


class SelectedPart(Model):
    role: str = Field(description="Function in the design, e.g. 'main MCU', '3.3 V regulator'")
    block_id: str = Field(description="Architecture block id this part belongs to")
    mpn: str = Field(description="Must be one of the candidate MPNs given, unless none fit")
    quantity: int = Field(default=1, ge=1)
    rationale: str
    confidence: Confidence
    alternates: list[str] = Field(
        default_factory=list, description="Other candidate MPNs that would work"
    )


class ComponentSelection(Model):
    parts: list[SelectedPart]
    notes: list[str] = Field(default_factory=list)
    rejected_candidates: list[str] = Field(
        default_factory=list, description="'MPN: reason' for notable candidates not chosen"
    )
