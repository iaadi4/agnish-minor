"""LLM-backed pipeline stages and the deterministic checks that wrap them."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..library import ComponentLibrary, SearchQuery
from ..llm import AuditLog, LLMClient, generate_structured
from ..schemas import (
    Architecture,
    Component,
    ComponentSelection,
    Finding,
    FindingCategory,
    LibraryPart,
    Requirements,
    Severity,
)
from . import prompts
from .synth import PASSIVE_CATEGORIES, Selected

#: Library categories offered as candidates for each architecture block kind.
BLOCK_CATEGORIES: dict[str, list[str]] = {
    "power_input": [
        "connector_usb",
        "connector_power",
        "connector_terminal",
        "fuse",
        "diode",
        "tvs",
        "esd",
    ],
    "charging": ["charger", "led"],
    "battery": ["connector_battery", "battery_holder", "fuel_gauge"],
    "regulation": ["ldo", "buck", "boost", "load_switch"],
    "mcu_wireless": ["mcu_wireless"],
    "mcu": ["mcu_wireless"],
    "sensor": ["sensor_env", "sensor_motion", "sensor_temp", "sensor_light", "load_switch"],
    "actuator_driver": [
        "motor_driver",
        "relay",
        "mosfet",
        "diode",
        "transistor_array",
        "connector_terminal",
    ],
    "connector": [
        "connector_usb",
        "connector_header",
        "connector_terminal",
        "connector_battery",
        "connector_power",
        "connector_rf",
    ],
    "protection": ["esd", "tvs", "fuse", "diode", "mosfet"],
    "debug_programming": ["connector_debug", "connector_header", "usb_uart", "switch"],
    "user_io": ["led", "switch"],
    "clock": ["rtc"],
    "isolation": ["optocoupler"],
    "other": ["mechanical", "test_point", "connector_rf", "optocoupler"],
}


@dataclass
class StageContext:
    client: LLMClient
    audit: AuditLog | None = None
    fixture_key: str = ""
    feedback: list[str] = field(default_factory=list)


def _revision_block(ctx: StageContext, previous: str | None, locked: list[str]) -> str:
    if not ctx.feedback:
        return ""
    lines = [prompts.REFINE_SUFFIX, "Reviewer feedback:"]
    lines += [f"- {f}" for f in ctx.feedback]
    if locked:
        lines += ["LOCKED items (keep unchanged):"] + [f"- {x}" for x in locked]
    if previous:
        lines += ["Previous output for this stage:", previous]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stage 1: requirements
# ---------------------------------------------------------------------------


def run_requirements(
    ctx: StageContext, prompt: str, previous: Requirements | None = None
) -> Requirements:
    user = f"User request:\n{prompt}"
    rev = _revision_block(ctx, previous.model_dump_json() if previous else None, [])
    if rev:
        user += "\n\n" + rev
    return generate_structured(
        ctx.client,
        stage="requirements",
        output_model=Requirements,
        system=prompts.REQUIREMENTS,
        user=user,
        fixture_key=ctx.fixture_key or prompt,
        audit=ctx.audit,
    )


def requirements_findings(req: Requirements) -> list[Finding]:
    out: list[Finding] = []
    for i, c in enumerate(req.conflicts, start=1):
        out.append(
            Finding(
                id=f"REQ-CONFLICT:{i}",
                rule="REQ-CONFLICT",
                severity=Severity.WARNING,
                category=FindingCategory.REQUIREMENTS,
                message=f"Requirement conflict: {c}",
                suggested_fix="Resolve with the requester before sign-off.",
            )
        )
    for i, q in enumerate(req.out_of_scope, start=1):
        out.append(
            Finding(
                id=f"REQ-OUT-OF-SCOPE:{i}",
                rule="REQ-OUT-OF-SCOPE",
                severity=Severity.WARNING,
                category=FindingCategory.REQUIREMENTS,
                message=f"Out of scope for this tool, not designed: {q}",
            )
        )
    if not req.power_sources:
        out.append(
            Finding(
                id="REQ-NO-POWER:plan",
                rule="REQ-NO-POWER",
                severity=Severity.ERROR,
                category=FindingCategory.REQUIREMENTS,
                message="No power source identified in the requirements.",
                suggested_fix="State how the board is powered.",
            )
        )
    return out


# ---------------------------------------------------------------------------
# Stage 2: architecture
# ---------------------------------------------------------------------------


def run_architecture(
    ctx: StageContext, req: Requirements, previous: Architecture | None = None
) -> Architecture:
    user = "Requirements (validated JSON):\n" + req.model_dump_json(exclude_defaults=True)
    rev = _revision_block(ctx, previous.model_dump_json() if previous else None, [])
    if rev:
        user += "\n\n" + rev
    return generate_structured(
        ctx.client,
        stage="architecture",
        output_model=Architecture,
        system=prompts.ARCHITECTURE,
        user=user,
        fixture_key=ctx.fixture_key,
        audit=ctx.audit,
    )


def architecture_findings(req: Requirements, arch: Architecture) -> list[Finding]:
    """Deterministic coverage checks: does the architecture address the requirements?"""
    kinds = {b.kind for b in arch.blocks}
    out: list[Finding] = []

    def need(cond: bool, rule: str, msg: str) -> None:
        if cond:
            out.append(
                Finding(
                    id=f"ARCH-COVERAGE:{rule}",
                    rule="ARCH-COVERAGE",
                    severity=Severity.ERROR,
                    category=FindingCategory.REQUIREMENTS,
                    message=msg,
                )
            )

    need(
        bool(req.wireless) and "mcu_wireless" not in kinds,
        "wireless",
        "Wireless is required but no mcu_wireless block exists.",
    )
    need(
        bool(req.sensors) and "sensor" not in kinds,
        "sensor",
        "Sensors are required but there is no sensor block.",
    )
    need(
        bool(req.actuators) and "actuator_driver" not in kinds,
        "actuator",
        "Actuators are required but there is no actuator_driver block.",
    )
    batt = {"li_ion_1s", "lipo_1s", "coin_cell", "alkaline_aa", "alkaline_aaa"}
    need(
        any(p.kind in batt for p in req.power_sources) and "battery" not in kinds,
        "battery",
        "A battery is required but there is no battery block.",
    )
    need(
        any(p.rechargeable for p in req.power_sources) and "charging" not in kinds,
        "charging",
        "A rechargeable battery is required but there is no charging block.",
    )
    need(
        not ({"mcu", "mcu_wireless"} & kinds),
        "mcu",
        "No MCU block: the synthesizer needs a controller to attach peripherals to.",
    )
    return out


# ---------------------------------------------------------------------------
# Stage 3: component selection
# ---------------------------------------------------------------------------


def candidates_for(
    arch: Architecture, lib: ComponentLibrary, per_category: int = 12
) -> dict[str, list[LibraryPart]]:
    out: dict[str, list[LibraryPart]] = {}
    for b in arch.blocks:
        cats = BLOCK_CATEGORIES.get(b.kind, BLOCK_CATEGORIES["other"])
        parts: list[LibraryPart] = []
        for cat in cats:
            parts += lib.search(SearchQuery(categories=[cat], limit=per_category))
        out[b.id] = parts
    return out


def _candidate_table(cands: dict[str, list[LibraryPart]], arch: Architecture) -> str:
    lines = []
    for b in arch.blocks:
        lines.append(f"## block `{b.id}` ({b.kind}): {b.name}")
        for p in cands.get(b.id, []):
            sr = f"{p.supply_range.min_v:g}-{p.supply_range.max_v:g} V" if p.supply_range else "-"
            cur = p.current.max_ma if p.current.max_ma is not None else p.current.typ_ma
            specs = json.dumps(p.key_specs, separators=(",", ":"))[:220]
            lines.append(
                f"- {p.mpn} | {p.category} | {p.description} | supply {sr} | "
                f"I {cur if cur is not None else '-'} mA | {specs}"
            )
    return "\n".join(lines)


def run_components(
    ctx: StageContext,
    req: Requirements,
    arch: Architecture,
    lib: ComponentLibrary,
    previous: ComponentSelection | None = None,
    locked: list[Component] | None = None,
    rejected: list[Component] | None = None,
) -> ComponentSelection:
    cands = candidates_for(arch, lib)
    user = (
        "Requirements:\n"
        + req.model_dump_json(exclude_defaults=True)
        + "\n\nArchitecture:\n"
        + arch.model_dump_json(exclude_defaults=True)
        + "\n\nCandidates per block (from the verified library):\n"
        + _candidate_table(cands, arch)
    )
    locked_txt = [f"{c.ref_des} {c.mpn} ({c.role})" for c in locked or []]
    rev = _revision_block(ctx, previous.model_dump_json() if previous else None, locked_txt)
    if rejected:
        rev += "\nREJECTED by the reviewer (do not choose again):\n" + "\n".join(
            f"- {c.mpn} ({c.role})" for c in rejected
        )
    if rev:
        user += "\n\n" + rev
    return generate_structured(
        ctx.client,
        stage="components",
        output_model=ComponentSelection,
        system=prompts.COMPONENTS,
        user=user,
        fixture_key=ctx.fixture_key,
        audit=ctx.audit,
    )


def resolve_selection(
    sel: ComponentSelection,
    arch: Architecture,
    lib: ComponentLibrary,
    locked: list[Component] | None = None,
    rejected_mpns: set[str] | None = None,
) -> tuple[list[Selected], list[Finding]]:
    """Cross-check the LLM's choices against the library and the architecture."""
    cands = candidates_for(arch, lib)
    kinds = {b.id: b.kind for b in arch.blocks}
    out: list[Selected] = []
    findings: list[Finding] = []
    locked = [c for c in locked or [] if c.origin != "synth"]
    rejected_mpns = rejected_mpns or set()
    locked_mpns = {c.mpn for c in locked}

    for c in locked:
        out.append(
            Selected(
                mpn=c.mpn,
                role=c.role,
                block_id=c.block_id,
                block_kind=kinds.get(c.block_id or ""),
                rationale=c.rationale,
                confidence=c.confidence,
                alternates=list(c.alternates),
                origin=c.origin,
                status=c.status,
                locked=True,
                ref_hint=c.ref_des,
            )
        )

    for i, sp in enumerate(sel.parts):
        part = lib.get(sp.mpn)
        if part is not None and part.mpn in locked_mpns:
            continue  # already carried over verbatim
        block_kind = kinds.get(sp.block_id)
        if block_kind is None:
            findings.append(
                Finding(
                    id=f"SEL-UNKNOWN-BLOCK:{i}",
                    rule="SEL-UNKNOWN-BLOCK",
                    severity=Severity.WARNING,
                    category=FindingCategory.LIBRARY,
                    message=f"{sp.mpn} assigned to unknown block '{sp.block_id}'.",
                )
            )
        if part is not None and part.category in PASSIVE_CATEGORIES:
            findings.append(
                Finding(
                    id=f"SEL-PASSIVE-DROPPED:{part.mpn}",
                    rule="SEL-PASSIVE-DROPPED",
                    severity=Severity.INFO,
                    category=FindingCategory.LIBRARY,
                    message=(
                        f"{part.mpn} ({sp.role}) dropped: passives are sized and added by the "
                        "deterministic synthesizer."
                    ),
                )
            )
            continue
        if part is not None and part.mpn in rejected_mpns:
            findings.append(
                Finding(
                    id=f"SEL-REJECTED-AGAIN:{part.mpn}",
                    rule="SEL-REJECTED-AGAIN",
                    severity=Severity.WARNING,
                    category=FindingCategory.LIBRARY,
                    message=f"{part.mpn} was rejected by the reviewer and is not used.",
                )
            )
            continue
        if part is not None and part not in cands.get(sp.block_id, []):
            findings.append(
                Finding(
                    id=f"SEL-OFF-CANDIDATE:{part.mpn}",
                    rule="SEL-OFF-CANDIDATE",
                    severity=Severity.INFO,
                    category=FindingCategory.LIBRARY,
                    message=(
                        f"{part.mpn} chosen for block '{sp.block_id}' but was not in that "
                        "block's candidate list (it is in the library)."
                    ),
                )
            )
        for alt in sp.alternates:
            if lib.get(alt) is None:
                findings.append(
                    Finding(
                        id=f"SEL-ALT-UNKNOWN:{alt}",
                        rule="SEL-ALT-UNKNOWN",
                        severity=Severity.INFO,
                        category=FindingCategory.LIBRARY,
                        message=f"Alternate {alt} for {sp.mpn} is not in the library.",
                    )
                )
        for q in range(sp.quantity):
            out.append(
                Selected(
                    mpn=part.mpn if part else sp.mpn,
                    role=sp.role if sp.quantity == 1 else f"{sp.role} #{q + 1}",
                    block_id=sp.block_id if block_kind else None,
                    block_kind=block_kind,
                    rationale=sp.rationale,
                    confidence=sp.confidence if part else min(sp.confidence, 0.3),
                    alternates=[a for a in sp.alternates if a != sp.mpn],
                )
            )
    return out, findings
