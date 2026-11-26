"""Refinement: turn reviewer feedback into a partial regeneration (a new plan version).

The stage to restart from is chosen deterministically from the feedback text (rules below,
first match wins) so a reviewer can predict what will change; ``stage=`` overrides it.
Simple numeric directives ("board 60 x 40 mm", "4 layers") are applied to the plan
directly, because the stages that use them are deterministic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..library import ComponentLibrary
from ..llm import AuditLog, LLMClient
from ..pipeline.engine import STAGES, PipelineOptions, run_pipeline
from ..schemas import DesignPlan, ReviewStatus
from ..schemas.review import utcnow

RULES: list[tuple[str, str, re.Pattern[str]]] = [
    (
        "components",
        "names a part / asks for a substitution",
        re.compile(
            r"\b(replace|instead|swap|substitut|use (a|an|the)|cheaper|alternative|different "
            r"(part|chip|sensor|regulator|mcu))\b",
            re.I,
        ),
    ),
    (
        "placement",
        "asks to move or arrange parts",
        re.compile(
            r"\b(move|place|placement|closer|further away|rotate|reposition|rearrange)\b", re.I
        ),
    ),
    (
        "routing",
        "concerns routing / stackup",
        re.compile(r"\b(route|routing|trace|ground plane|pour|stackup|impedance|via)\b", re.I),
    ),
    (
        "constraints",
        "changes board size / layers / fab rules",
        re.compile(
            r"(\b\d+(\.\d+)?\s*[x\u00d7]\s*\d+(\.\d+)?\s*mm\b|\b\d\s*-?\s*layers?\b|board size|"
            r"bigger board|smaller board|mounting hole|fab profile|net ?class)",
            re.I,
        ),
    ),
    (
        "architecture",
        "changes the block structure",
        re.compile(r"\b(architecture|block|topology|separate (rail|supply))\b", re.I),
    ),
]


@dataclass(frozen=True)
class Classification:
    stage: str
    reason: str


def classify_feedback(feedback: str, lib: ComponentLibrary | None = None) -> Classification:
    if lib is not None:
        for p in lib.all():
            if len(p.mpn) >= 4 and re.search(re.escape(p.mpn.split("-")[0]), feedback, re.I):
                return Classification("components", f"mentions library part {p.mpn}")
    for stage, why, pat in RULES:
        if pat.search(feedback):
            return Classification(stage, why)
    return Classification("requirements", "general feedback: re-interpret the requirements")


_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*[x\u00d7]\s*(\d+(?:\.\d+)?)\s*mm", re.I)
_LAYERS = re.compile(r"\b(\d)\s*-?\s*layers?\b", re.I)


def apply_directives(plan: DesignPlan, feedback: str) -> list[str]:
    """Apply numeric directives to the requirements; returns notes describing them."""
    notes: list[str] = []
    req = plan.requirements
    if req is None:
        return notes
    m = _SIZE.search(feedback)
    if m:
        w, h = float(m.group(1)), float(m.group(2))
        req.size.max_width_mm, req.size.max_height_mm = w, h
        notes.append(f"Board size set to {w:g} x {h:g} mm from reviewer feedback.")
    m = _LAYERS.search(feedback)
    if m:
        req.layer_count_target = int(m.group(1))
        notes.append(f"Layer count set to {m.group(1)} from reviewer feedback.")
    return notes


def refine_plan(
    plan: DesignPlan,
    feedback: str,
    *,
    client: LLMClient,
    lib: ComponentLibrary,
    audit: AuditLog | None = None,
    stage: str | None = None,
    version: int | None = None,
    options: PipelineOptions | None = None,
) -> tuple[DesignPlan, Classification]:
    """Return a new plan version regenerated from the affected stage."""
    if stage is not None and stage not in STAGES:
        raise ValueError(f"unknown stage '{stage}'; choose from {STAGES}")
    cls = (
        Classification(stage, "chosen by the reviewer")
        if stage
        else classify_feedback(feedback, lib)
    )
    base = plan.model_copy(deep=True)
    base.parent_version = plan.version
    base.version = version or plan.version + 1
    base.created_at = utcnow()
    base.feedback_history = [*plan.feedback_history, feedback]
    base.review_status = ReviewStatus.DRAFT
    notes = apply_directives(base, feedback)
    start = cls.stage
    if notes and STAGES.index(start) > STAGES.index("constraints"):
        start = "constraints"
    if audit is not None:
        audit.log(
            "review",
            stage=start,
            data={"feedback": feedback, "reason": cls.reason, "version": str(base.version)},
        )
    new = run_pipeline(base, start=start, client=client, lib=lib, audit=audit, options=options)
    new.design_notes = notes + new.design_notes
    return new, Classification(start, cls.reason)
