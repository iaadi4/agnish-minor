"""The top-level DesignPlan document."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from .architecture import Architecture
from .common import DISCLAIMER, SCHEMA_VERSION, Model, ReviewStatus
from .component import Component
from .constraints import Constraints, RailConstraint
from .findings import CheckSummary, Finding
from .llm_outputs import ComponentSelection
from .netlist import Netlist
from .placement import PlacementPlan
from .requirements import Assumption, Requirements
from .review import ReviewComment, ReviewDecision, utcnow
from .routing import RoutingGuidance


class LLMProvenance(Model):
    mode: Literal["live", "mock"]
    model: str
    fixture_set: str | None = None


class DesignPlan(Model):
    schema_version: str = SCHEMA_VERSION
    disclaimer: str = DISCLAIMER
    version: int = Field(default=1, ge=1)
    parent_version: int | None = None
    created_at: datetime = Field(default_factory=utcnow)
    prompt: str
    feedback_history: list[str] = Field(default_factory=list)
    llm: LLMProvenance | None = None
    requirements: Requirements | None = None
    architecture: Architecture | None = None
    selection: ComponentSelection | None = Field(
        default=None, description="Raw LLM component selection (before library cross-check)"
    )
    components: list[Component] = Field(default_factory=list)
    netlist: Netlist | None = None
    power_tree: list[RailConstraint] = Field(
        default_factory=list, description="Rails derived by the synthesizer"
    )
    constraints: Constraints | None = None
    placement: PlacementPlan | None = None
    routing: RoutingGuidance | None = None
    findings: list[Finding] = Field(default_factory=list)
    check_summary: CheckSummary | None = None
    assumptions: list[Assumption] = Field(default_factory=list)
    design_notes: list[str] = Field(
        default_factory=list, description="Deterministic notes from synthesis/constraints stages"
    )
    stages_run: list[str] = Field(default_factory=list, description="Stages (re)generated")
    review_status: ReviewStatus = ReviewStatus.DRAFT
    comments: list[ReviewComment] = Field(default_factory=list)
    decisions: list[ReviewDecision] = Field(default_factory=list)

    def component(self, ref: str) -> Component | None:
        return next((c for c in self.components if c.ref_des == ref), None)
