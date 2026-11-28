"""Pipeline orchestration: run stages in order, or re-run from a given stage.

Stages, in order (each depends only on the outputs of earlier ones)::

    requirements -> architecture -> components -> netlist -> constraints
                 -> placement -> routing -> checks

Two engines produce the components and the netlist:

* ``intent`` (default): the LLM chooses real parts (curated library or any stock KiCad
  symbol/footprint) and writes the pin-level netlist; deterministic validators check every
  part, pin and rail and run ERC/power checks, feeding problems back until they pass.
  Problems that need a part (missing decoupling, pull-ups ...) loop back to the parts stage.
* ``synth``: the LLM picks curated MPNs only and the rule-based synthesizer wires them.

Everything from ``constraints`` onward is deterministic. A refinement re-runs from the
earliest affected stage; locked/edited items are carried over and the LLM is told about them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..checks import run_checks
from ..library import ComponentLibrary
from ..llm import AuditLog, LLMClient
from ..schemas import (
    Component,
    DesignPlan,
    Finding,
    FindingCategory,
    ItemStatus,
    LLMProvenance,
    Severity,
)
from .constraints import run_constraints_stage
from .design import Libraries, run_netlist, run_parts
from .intent import ManifestResult, resolve_manifest
from .placement import run_placement_stage
from .routing import run_routing_stage
from .stages import (
    StageContext,
    architecture_findings,
    requirements_findings,
    resolve_selection,
    run_architecture,
    run_components,
    run_requirements,
)
from .synth import synthesize

STAGES = [
    "requirements",
    "architecture",
    "components",
    "netlist",
    "constraints",
    "placement",
    "routing",
    "checks",
]
LLM_STAGES = {"requirements", "architecture", "components", "netlist"}

#: Stage-owned finding rule prefixes, so a re-run replaces only that stage's findings.
STAGE_RULES: dict[str, tuple[str, ...]] = {
    "requirements": ("REQ-",),
    "architecture": ("ARCH-",),
    "components": ("SEL-", "LIB-CLAIMED-RATINGS", "LIB-NOTE"),
    "netlist": ("SYNTH-", "LIB-UNVERIFIED", "REVIEW-LOCK", "NET-"),
    "constraints": ("CON-",),
    "placement": ("PLACE-",),
    "routing": ("ROUTE-",),
    "checks": ("ERC-", "PWR-", "DFM-", "LIB-", "DRC-", "MOCK-"),
}


@dataclass
class PipelineOptions:
    fab_profile: str | None = None
    #: Path of a KiCad DRC JSON report to merge into the findings (see export.drc)
    drc_report: Path | None = None
    #: "intent" or "synth"; None keeps the plan's engine
    engine: str | None = None
    #: parts <-> netlist repair rounds when ERC needs a part change
    repair_rounds: int = 2
    extra_stage_hooks: dict[str, Callable[[DesignPlan], None]] = field(default_factory=dict)


def fixture_key(prompt: str, feedback: list[str]) -> str:
    if not feedback:
        return prompt
    return prompt + "\n--- feedback ---\n" + "\n".join(feedback)


def _drop_stage_findings(plan: DesignPlan, stages: list[str]) -> list[Finding]:
    prefixes = tuple(p for s in stages for p in STAGE_RULES[s])
    return [f for f in plan.findings if not f.id.startswith(prefixes)]


def _carried(plan: DesignPlan) -> tuple[list[Component], list[Component]]:
    keep = [
        c
        for c in plan.components
        if (c.locked or c.status == ItemStatus.EDITED) and c.origin != "synth"
    ]
    rejected = [c for c in plan.components if c.status == ItemStatus.REJECTED]
    return keep, rejected


def run_pipeline(
    plan: DesignPlan,
    *,
    start: str,
    client: LLMClient,
    lib: ComponentLibrary,
    audit: AuditLog | None = None,
    options: PipelineOptions | None = None,
) -> DesignPlan:
    """Run stages from ``start`` to the end on a copy of ``plan`` and return it."""
    if start not in STAGES:
        raise ValueError(f"unknown stage '{start}'; choose from {STAGES}")
    options = options or PipelineOptions()
    audit = audit if audit is not None else AuditLog(None)
    previous = plan
    plan = plan.model_copy(deep=True)
    todo = STAGES[STAGES.index(start) :]
    plan.findings = _drop_stage_findings(plan, todo)
    plan.stages_run = list(todo)
    ctx = StageContext(
        client=client,
        audit=audit,
        fixture_key=fixture_key(plan.prompt, plan.feedback_history),
        feedback=list(plan.feedback_history),
    )
    audit.log("stage", stage=start, data={"rerun": ",".join(todo), "version": str(plan.version)})

    if "requirements" in todo:
        plan.requirements = run_requirements(ctx, plan.prompt, previous.requirements)
        plan.assumptions = list(plan.requirements.assumptions)
        plan.findings += requirements_findings(plan.requirements)
    if plan.requirements is None:
        raise ValueError("plan has no requirements; start from 'requirements'")

    if "architecture" in todo:
        plan.architecture = run_architecture(ctx, plan.requirements, previous.architecture)
        plan.findings += architecture_findings(plan.requirements, plan.architecture)
    if plan.architecture is None:
        raise ValueError("plan has no architecture; start from 'architecture' or earlier")

    keep, rejected = _carried(previous)
    engine = options.engine or plan.engine
    plan.engine = engine
    if engine == "synth":
        _synth_stages(plan, previous, todo, ctx, lib, keep, rejected)
    else:
        _intent_stages(plan, previous, todo, ctx, lib, keep, rejected, options)

    if "constraints" in todo:
        run_constraints_stage(plan, lib, options.fab_profile)
    if "placement" in todo and plan.constraints is not None:
        run_placement_stage(plan)
    if "routing" in todo and plan.constraints is not None:
        run_routing_stage(plan)
    if "checks" in todo:
        run_checks(plan, drc_report=options.drc_report)
    for stage in todo:
        hook = options.extra_stage_hooks.get(stage)
        if hook:
            hook(plan)

    plan.llm = LLMProvenance(
        mode=client.mode,
        model=client.model_name,
        fixture_set=_fixtures_used(audit) if client.mode == "mock" else None,
    )
    if client.mode == "mock":
        plan.findings = [f for f in plan.findings if not f.id.startswith("MOCK-")]
        nearest = [e for e in audit.entries if str(e.data.get("match", "exact")) != "exact"]
        if nearest and any(e.kind == "llm_call" for e in nearest):
            plan.findings.append(
                Finding(
                    id="MOCK-NEAREST:plan",
                    rule="MOCK-NEAREST",
                    severity=Severity.WARNING,
                    category=FindingCategory.REQUIREMENTS,
                    message=(
                        "Mock LLM mode: no fixture was recorded for this exact prompt, so the "
                        "nearest example fixture answered. The plan describes that example, "
                        "not necessarily your request."
                    ),
                    suggested_fix="Run without --mock-llm (needs ANTHROPIC_API_KEY).",
                )
            )
    return plan


def _synth_stages(
    plan: DesignPlan,
    previous: DesignPlan,
    todo: list[str],
    ctx: StageContext,
    lib: ComponentLibrary,
    keep: list[Component],
    rejected: list[Component],
) -> None:
    assert plan.requirements is not None and plan.architecture is not None
    if "components" in todo:
        plan.selection = run_components(
            ctx,
            plan.requirements,
            plan.architecture,
            lib,
            previous.selection,
            locked=keep,
            rejected=rejected,
        )
    if "netlist" in todo:
        if plan.selection is None:
            raise ValueError("plan has no component selection; start from 'components'")
        selected, sel_findings = resolve_selection(
            plan.selection,
            plan.architecture,
            lib,
            locked=keep,
            rejected_mpns={c.mpn for c in rejected},
        )
        if "components" in todo:
            plan.findings += sel_findings
        prev_refs = {c.key: c.ref_des for c in previous.components if c.key}
        result = synthesize(lib, plan.requirements, selected, prev_refs)
        plan.components = _carry_review_state(previous.components, result.components)
        plan.netlist = result.netlist
        plan.findings += result.findings + _lock_findings(previous, plan)
        plan.design_notes = list(result.notes)
        plan.power_tree = result.rails


def _intent_stages(
    plan: DesignPlan,
    previous: DesignPlan,
    todo: list[str],
    ctx: StageContext,
    lib: ComponentLibrary,
    keep: list[Component],
    rejected: list[Component],
    options: PipelineOptions,
) -> None:
    """Parts and netlist authored by the LLM, verified (and repaired) in loops."""
    assert plan.requirements is not None and plan.architecture is not None
    req, arch = plan.requirements, plan.architecture
    libs = Libraries(lib)
    repair: list[str] = []
    last_manifest = None
    rounds = options.repair_rounds if "components" in todo else 0
    for round_no in range(rounds + 1):
        if "components" in todo:
            part_ctx = ctx
            if repair:
                part_ctx = StageContext(
                    client=ctx.client,
                    audit=ctx.audit,
                    fixture_key=ctx.fixture_key,
                    feedback=[*ctx.feedback, "ERC after wiring needs part changes:", *repair],
                )
            res, resolved = run_parts(
                part_ctx,
                req,
                arch,
                libs,
                plan.manifest if repair else previous.manifest,
                locked=keep,
                rejected=rejected,
            )
            plan.manifest = res.value
            plan.findings = [
                f for f in plan.findings if not f.id.startswith(STAGE_RULES["components"])
            ]
            plan.findings += _manifest_findings(res.open_problems, resolved)
        if "netlist" not in todo:
            return
        if plan.manifest is None:
            raise ValueError("plan has no parts manifest; start from 'components'")
        resolved = resolve_manifest(plan.manifest, arch, lib, libs.symbols, libs.footprints)
        comps = _apply_carried(resolved.components, keep)
        outcome = run_netlist(
            ctx,
            req,
            arch,
            plan.manifest,
            comps,
            libs,
            plan.netlist_intent if repair else previous.netlist_intent,
            locked_nets=_locked_net_text(previous),
        )
        plan.netlist_intent = outcome.result.value
        plan.netlist = outcome.netlist
        plan.power_tree = outcome.rails
        plan.components = _carry_review_state(previous.components, outcome.components)
        rejected_mpns = {c.mpn for c in rejected}
        for c in plan.components:
            if c.mpn in rejected_mpns:
                c.status = ItemStatus.REJECTED  # the model re-proposed it: keep it flagged
        plan.findings = [f for f in plan.findings if not f.id.startswith(("NET-", "REVIEW-LOCK"))]
        plan.findings += outcome.findings + _lock_findings(previous, plan)
        # open problems are reported by NET-INTENT (structure) or by run_checks (ERC/power)
        notes = [
            f"Parts: {len(plan.manifest.parts)} entries; netlist: {len(plan.netlist.nets)} nets, "
            f"accepted after {outcome.result.submissions} submission(s)"
            + ("" if outcome.result.accepted else " (NOT accepted: open problems listed)"),
            *plan.netlist_intent.notes,
        ]
        plan.design_notes = notes
        if repair and plan.manifest == last_manifest:
            break  # the parts stage made no change: another round would repeat this one
        last_manifest = plan.manifest
        repair = outcome.part_problems
        if not repair or round_no == rounds:
            break
        ctx.audit.log(
            "stage", stage="components", data={"repair_round": str(round_no + 1)}
        ) if ctx.audit else None


def _apply_carried(comps: list[Component], keep: list[Component]) -> list[Component]:
    """Reviewer-edited/locked parts win over the manifest's version of the same ref."""
    by_ref = {c.ref_des: c for c in keep}
    return [by_ref.get(c.ref_des, c) if by_ref.get(c.ref_des, c).mpn == c.mpn else c for c in comps]


def _locked_net_text(previous: DesignPlan) -> list[str]:
    if previous.netlist is None:
        return []
    return [
        f"net {n.name}: {', '.join(f'{c.ref}.{c.pin}' for c in n.connections)}"
        for n in previous.netlist.nets
        if n.locked
    ]


def _manifest_findings(open_problems: list[str], resolved: ManifestResult) -> list[Finding]:
    out = [
        _open_problem("SEL-REJECTED-AGAIN" if "rejected by the reviewer" in m else "SEL-OPEN", i, m)
        for i, m in enumerate(open_problems, 1)
    ]
    out += resolved.findings
    for i, w in enumerate(resolved.warnings, 1):
        out.append(
            Finding(
                id=f"LIB-NOTE:{i}",
                rule="LIB-NOTE",
                severity=Severity.INFO,
                category=FindingCategory.LIBRARY,
                message=w,
            )
        )
    return out


def _open_problem(rule: str, i: int, msg: str) -> Finding:
    return Finding(
        id=f"{rule}:{i}",
        rule=rule,
        severity=Severity.ERROR,
        category=FindingCategory.LIBRARY if rule.startswith("SEL") else FindingCategory.ERC,
        message=f"Validator problem left open by the model: {msg}",
        suggested_fix="Fix by hand (review edit) or refine with feedback.",
    )


def _carry_review_state(old: list[Component], new: list[Component]) -> list[Component]:
    by_key = {c.key: c for c in old if c.key}
    out = []
    for c in new:
        prev = by_key.get(c.key or "")
        same = prev is not None and prev.mpn == c.mpn and not c.locked
        if (
            same
            and prev is not None
            and (prev.status in (ItemStatus.APPROVED, ItemStatus.EDITED) or prev.locked)
        ):
            c = c.model_copy(update={"status": prev.status, "locked": prev.locked})
        out.append(c)
    return out


def _lock_findings(previous: DesignPlan, plan: DesignPlan) -> list[Finding]:
    if previous.netlist is None or plan.netlist is None:
        return []
    out = []
    for n in previous.netlist.nets:
        if not n.locked:
            continue
        now = plan.netlist.net(n.name)
        before = sorted((c.ref, c.pin) for c in n.connections)
        after = sorted((c.ref, c.pin) for c in now.connections) if now else None
        if after != before:
            out.append(
                Finding(
                    id=f"REVIEW-LOCK:{n.name}",
                    rule="REVIEW-LOCK",
                    severity=Severity.ERROR,
                    category=FindingCategory.ERC,
                    message=f"Locked net {n.name} changed during regeneration.",
                    nets=[n.name],
                    suggested_fix="Unlock it, or revert the change that affected it.",
                )
            )
        elif now is not None:
            now.locked = True
            now.status = n.status
    return out


def _fixtures_used(audit: AuditLog) -> str | None:
    names = sorted({str(e.data["fixture"]) for e in audit.entries if "fixture" in e.data})
    return ", ".join(names) or None


def generate_plan(
    prompt: str,
    *,
    client: LLMClient,
    lib: ComponentLibrary | None = None,
    audit: AuditLog | None = None,
    options: PipelineOptions | None = None,
) -> DesignPlan:
    """Full pipeline from a natural-language prompt."""
    lib = lib or ComponentLibrary.load()
    return run_pipeline(
        DesignPlan(prompt=prompt),
        start="requirements",
        client=client,
        lib=lib,
        audit=audit,
        options=options,
    )
