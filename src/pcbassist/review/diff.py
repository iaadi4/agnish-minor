"""Structured diff between two plan versions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..schemas import Component, DesignPlan

COMPONENT_FIELDS = ("mpn", "value", "footprint", "role", "rail", "status", "locked", "verified")


@dataclass
class PlanDiff:
    a_version: int
    b_version: int
    components_added: list[str] = field(default_factory=list)
    components_removed: list[str] = field(default_factory=list)
    components_changed: list[tuple[str, str, Any, Any]] = field(default_factory=list)
    nets_added: list[str] = field(default_factory=list)
    nets_removed: list[str] = field(default_factory=list)
    nets_changed: list[tuple[str, list[str], list[str]]] = field(default_factory=list)
    rails_changed: list[tuple[str, str, Any, Any]] = field(default_factory=list)
    outline_changed: tuple[str, str] | None = None
    moved: list[tuple[str, float]] = field(default_factory=list)
    findings_new: list[str] = field(default_factory=list)
    findings_resolved: list[str] = field(default_factory=list)
    status: tuple[str, str] | None = None
    requirements_changed: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not any(
            [
                self.components_added,
                self.components_removed,
                self.components_changed,
                self.nets_added,
                self.nets_removed,
                self.nets_changed,
                self.rails_changed,
                self.outline_changed,
                self.moved,
                self.findings_new,
                self.findings_resolved,
                self.status,
                self.requirements_changed,
            ]
        )

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


def _label(c: Component) -> str:
    return f"{c.ref_des} {c.mpn} ({c.role})"


def diff_plans(a: DesignPlan, b: DesignPlan) -> PlanDiff:
    d = PlanDiff(a.version, b.version)

    # components: match by stable synthesis key, then by ref
    def keyed(p: DesignPlan) -> dict[str, Component]:
        return {(c.key or c.ref_des): c for c in p.components}

    ka, kb = keyed(a), keyed(b)
    for k in kb.keys() - ka.keys():
        d.components_added.append(_label(kb[k]))
    for k in ka.keys() - kb.keys():
        d.components_removed.append(_label(ka[k]))
    for k in sorted(ka.keys() & kb.keys()):
        ca, cb = ka[k], kb[k]
        if ca.ref_des != cb.ref_des:
            d.components_changed.append((cb.ref_des, "ref_des", ca.ref_des, cb.ref_des))
        for f in COMPONENT_FIELDS:
            va, vb = getattr(ca, f), getattr(cb, f)
            if va != vb:
                d.components_changed.append(
                    (cb.ref_des, f, getattr(va, "value", va), getattr(vb, "value", vb))
                )
    d.components_added.sort()
    d.components_removed.sort()
    # nets
    na = {n.name: n for n in a.netlist.nets} if a.netlist else {}
    nb = {n.name: n for n in b.netlist.nets} if b.netlist else {}
    d.nets_added = sorted(nb.keys() - na.keys())
    d.nets_removed = sorted(na.keys() - nb.keys())
    for name in sorted(na.keys() & nb.keys()):
        sa = {f"{c.ref}.{c.pin}" for c in na[name].connections}
        sb = {f"{c.ref}.{c.pin}" for c in nb[name].connections}
        if sa != sb:
            d.nets_changed.append((name, sorted(sb - sa), sorted(sa - sb)))
    # rails and outline
    ra = {r.name: r for r in (a.constraints.electrical.rails if a.constraints else a.power_tree)}
    rb = {r.name: r for r in (b.constraints.electrical.rails if b.constraints else b.power_tree)}
    for name in sorted(ra.keys() | rb.keys()):
        x, y = ra.get(name), rb.get(name)
        if x is None or y is None:
            d.rails_changed.append((name, "exists", x is not None, y is not None))
            continue
        for f in ("nominal_v", "max_current_ma", "budget_ma", "source_ref", "parent_rail"):
            if getattr(x, f) != getattr(y, f):
                d.rails_changed.append((name, f, getattr(x, f), getattr(y, f)))
    if a.constraints and b.constraints:
        oa, ob = a.constraints.mechanical.outline, b.constraints.mechanical.outline
        if (oa.width_mm, oa.height_mm) != (ob.width_mm, ob.height_mm):
            d.outline_changed = (
                f"{oa.width_mm:g} x {oa.height_mm:g} mm",
                f"{ob.width_mm:g} x {ob.height_mm:g} mm",
            )
    pa = {p.ref: p for p in a.placement.placements} if a.placement else {}
    pb = {p.ref: p for p in b.placement.placements} if b.placement else {}
    for ref in sorted(pa.keys() & pb.keys()):
        dist = ((pa[ref].x_mm - pb[ref].x_mm) ** 2 + (pa[ref].y_mm - pb[ref].y_mm) ** 2) ** 0.5
        if dist > 0.01:
            d.moved.append((ref, round(dist, 2)))
    fa = {f.id for f in a.findings}
    fb = {f.id for f in b.findings}
    d.findings_new = sorted(fb - fa)
    d.findings_resolved = sorted(fa - fb)
    sa_ = a.check_summary.status.value if a.check_summary else "unchecked"
    sb_ = b.check_summary.status.value if b.check_summary else "unchecked"
    if sa_ != sb_:
        d.status = (sa_, sb_)
    if a.requirements and b.requirements:
        ja = a.requirements.model_dump(mode="json")
        jb = b.requirements.model_dump(mode="json")
        d.requirements_changed = sorted(k for k in ja if ja[k] != jb.get(k))
    return d


def format_diff(d: PlanDiff) -> str:
    L = [f"Diff v{d.a_version} -> v{d.b_version}"]
    if d.empty:
        return L[0] + ": no changes"
    if d.status:
        L.append(f"Check status: {d.status[0]} -> {d.status[1]}")
    if d.requirements_changed:
        L.append("Requirements changed: " + ", ".join(d.requirements_changed))
    for c in d.components_added:
        L.append(f"+ component {c}")
    for c in d.components_removed:
        L.append(f"- component {c}")
    for ref, f, x, y in d.components_changed:
        L.append(f"~ {ref}.{f}: {x} -> {y}")
    for n in d.nets_added:
        L.append(f"+ net {n}")
    for n in d.nets_removed:
        L.append(f"- net {n}")
    for name, add, rem in d.nets_changed:
        parts = []
        if add:
            parts.append("+" + " +".join(add))
        if rem:
            parts.append("-" + " -".join(rem))
        L.append(f"~ net {name}: {' '.join(parts)}")
    for name, f, x, y in d.rails_changed:
        L.append(f"~ rail {name}.{f}: {x} -> {y}")
    if d.outline_changed:
        L.append(f"~ board outline: {d.outline_changed[0]} -> {d.outline_changed[1]}")
    if d.moved:
        L.append("~ moved: " + ", ".join(f"{r} ({m:g} mm)" for r, m in d.moved[:20]))
    for f in d.findings_new:
        L.append(f"+ finding {f}")
    for f in d.findings_resolved:
        L.append(f"- finding {f} (resolved)")
    return "\n".join(L)
