"""Review actions on a plan: comments, approve / reject / edit / lock / unlock."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from ..library import ComponentLibrary
from ..schemas import (
    CheckStatus,
    Component,
    DesignPlan,
    ItemRef,
    ItemStatus,
    Rect,
    ReviewComment,
    ReviewDecision,
    ReviewStatus,
)

EDITABLE: dict[str, set[str]] = {
    "component": {"mpn", "value", "footprint", "role", "rationale", "rail", "key_specs", "in_bom"},
    "net": {"net_class", "notes", "voltage_v", "connections"},
    "rail": {"nominal_v", "min_v", "max_v", "max_current_ma"},
    "net_class": {"trace_width_mm", "clearance_mm", "via_diameter_mm", "via_drill_mm"},
    "placement": {"x_mm", "y_mm", "rotation_deg"},
    "requirements": {
        "target_battery_life_days",
        "duty_active_s",
        "duty_period_s",
        "size",
        "layer_count_target",
        "environment",
    },
    "constraint": {"outline", "fab_profile", "max_component_height_mm"},
}
#: Edits to these kinds change the design; the caller should save a new version.
DESIGN_EDIT_KINDS = set(EDITABLE)


class ReviewError(ValueError):
    pass


def _next_id(prefix: str, existing: list[str]) -> str:
    n = 1 + max((int(x.split("-")[-1]) for x in existing if x.startswith(prefix)), default=0)
    return f"{prefix}-{n}"


def _target_obj(plan: DesignPlan, target: ItemRef) -> Any:
    k, i = target.kind, target.id
    if k == "component":
        obj: Any = plan.component(i)
    elif k == "net":
        obj = plan.netlist.net(i) if plan.netlist else None
    elif k == "rail":
        rails = plan.constraints.electrical.rails if plan.constraints else []
        obj = next((r for r in rails if r.name == i), None)
    elif k == "net_class":
        ncs = plan.constraints.electrical.net_classes if plan.constraints else []
        obj = next((n for n in ncs if n.name == i), None)
    elif k == "placement":
        pls = plan.placement.placements if plan.placement else []
        obj = next((p for p in pls if p.ref == i), None)
    elif k == "constraint":
        obj = plan.constraints
    elif k == "requirements":
        obj = plan.requirements
    elif k == "architecture":
        obj = plan.architecture
    elif k == "routing":
        obj = plan.routing
    elif k == "plan":
        obj = plan
    else:  # pragma: no cover - TargetKind is a closed Literal
        obj = None
    if obj is None:
        raise ReviewError(f"no {k} '{i}' in this plan")
    return obj


def add_comment(
    plan: DesignPlan, target: ItemRef, text: str, author: str = "reviewer"
) -> ReviewComment:
    _target_obj(plan, target)
    c = ReviewComment(
        id=_next_id("C", [x.id for x in plan.comments]), target=target, text=text, author=author
    )
    plan.comments.append(c)
    if plan.review_status == ReviewStatus.DRAFT:
        plan.review_status = ReviewStatus.IN_REVIEW
    return c


def resolve_comment(plan: DesignPlan, comment_id: str) -> ReviewComment:
    for c in plan.comments:
        if c.id == comment_id:
            c.resolved = True
            return c
    raise ReviewError(f"no comment {comment_id}")


def _apply_edit(
    plan: DesignPlan, target: ItemRef, obj: Any, edit: dict[str, Any], lib: ComponentLibrary | None
) -> None:
    allowed = EDITABLE.get(target.kind)
    if allowed is None:
        raise ReviewError(f"{target.kind} items cannot be edited")
    bad = sorted(set(edit) - allowed)
    if bad:
        raise ReviewError(f"cannot edit {bad} on a {target.kind}; editable: {sorted(allowed)}")
    if target.kind == "component" and "mpn" in edit and lib is not None:
        part = lib.get(str(edit["mpn"]))
        comp: Component = obj
        if part is None:
            edit = {**edit}
            comp.verified = False
            comp.pins = []
            comp.confidence = min(comp.confidence, 0.3)
        else:
            edit = {
                "footprint": part.footprint,
                "value": part.value,
                "key_specs": dict(part.key_specs),
                **edit,
                "mpn": part.mpn,
            }
            comp.pins = list(part.pins)
            comp.category = part.category
            comp.manufacturer = part.manufacturer
            comp.description = part.description
            comp.supply_range = part.supply_range
            comp.current = part.current
            comp.datasheet_url = part.datasheet_url
            comp.kicad_symbol = part.kicad_symbol
            comp.verified = True
        comp.origin = "human"
    try:
        for field, value in edit.items():
            setattr(obj, field, value)
    except ValidationError as e:
        raise ReviewError(f"invalid edit: {e.errors()[0]['msg']}") from e
    if target.kind == "placement" and obj.bbox is not None:
        old = obj.bbox
        host = plan.component(target.id)
        from ..kicad.footprint import load_footprint, placed_bbox

        g = load_footprint(host.footprint) if host and host.footprint else None
        if g is not None:
            x0, y0, x1, y1 = placed_bbox(g, obj.x_mm, obj.y_mm, obj.rotation_deg)
            obj.bbox = Rect(x_mm=x0, y_mm=y0, w_mm=x1 - x0, h_mm=y1 - y0)
        else:
            obj.bbox = old
    if hasattr(obj, "status"):
        obj.status = ItemStatus.EDITED


def apply_decision(
    plan: DesignPlan,
    target: ItemRef,
    decision: str,
    *,
    author: str = "reviewer",
    note: str = "",
    edit: dict[str, Any] | None = None,
    lib: ComponentLibrary | None = None,
    force: bool = False,
) -> ReviewDecision:
    obj = _target_obj(plan, target)
    if decision not in ("approve", "reject", "edit", "lock", "unlock"):
        raise ReviewError(f"unknown decision '{decision}'")
    if target.kind == "plan":
        if decision == "approve":
            failing = plan.check_summary is None or plan.check_summary.status == CheckStatus.FAIL
            if failing and not force:
                raise ReviewError(
                    "plan checks FAIL (or were not run); fix the errors or approve with force"
                )
            plan.review_status = ReviewStatus.APPROVED
        elif decision == "reject":
            plan.review_status = ReviewStatus.REJECTED
        else:
            raise ReviewError("a whole plan can only be approved or rejected")
    elif decision == "edit":
        if not edit:
            raise ReviewError("edit decision needs an 'edit' mapping")
        _apply_edit(plan, target, obj, edit, lib)
    elif decision in ("lock", "unlock"):
        locked = decision == "lock"
        if target.kind == "placement":
            comp = plan.component(target.id)
            if comp is None:
                raise ReviewError(f"no component {target.id}")
            comp.locked = locked
        elif hasattr(obj, "locked"):
            obj.locked = locked
        else:
            raise ReviewError(f"{target.kind} items cannot be locked")
    else:
        if not hasattr(obj, "status"):
            raise ReviewError(f"{target.kind} items have no approval state")
        obj.status = ItemStatus.APPROVED if decision == "approve" else ItemStatus.REJECTED
    d = ReviewDecision(
        id=_next_id("D", [x.id for x in plan.decisions]),
        target=target,
        decision=decision,
        author=author,
        note=note,
        edit=edit,
    )
    plan.decisions.append(d)
    if plan.review_status == ReviewStatus.DRAFT:
        plan.review_status = ReviewStatus.IN_REVIEW
    return d


def review_summary(plan: DesignPlan) -> dict[str, int]:
    counts = {s.value: 0 for s in ItemStatus}
    for c in plan.components:
        counts[c.status.value] += 1
    counts["locked"] = sum(1 for c in plan.components if c.locked)
    counts["open_comments"] = sum(1 for c in plan.comments if not c.resolved)
    return counts
