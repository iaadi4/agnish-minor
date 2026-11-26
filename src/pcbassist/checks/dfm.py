"""Design-for-manufacture checks against the chosen fab profile, plus placement checks."""

from __future__ import annotations

import math
import re

from ..kicad.footprint import load_footprint, pad_position, placed_rects
from ..schemas import DesignPlan, Edge, FabProfile, Finding, FindingCategory, Rect, Severity

IMPERIAL_ORDER = ["01005", "0201", "0402", "0603", "0805", "1206", "1210", "1812", "2512"]
METRIC_TO_IMPERIAL = {
    "0402Metric": "01005",
    "0603Metric": "0201",
    "1005Metric": "0402",
    "1608Metric": "0603",
    "2012Metric": "0805",
    "3216Metric": "1206",
    "3225Metric": "1210",
    "4532Metric": "1812",
    "6332Metric": "2512",
}


def _F(
    rule: str,
    subject: str,
    sev: Severity,
    msg: str,
    *,
    cat: FindingCategory = FindingCategory.DFM,
    refs: list[str] | None = None,
    fix: str | None = None,
) -> Finding:
    return Finding(
        id=f"{rule}:{subject}",
        rule=rule,
        severity=sev,
        category=cat,
        message=msg,
        refs=refs or [],
        suggested_fix=fix,
    )


def passive_size(footprint: str) -> str | None:
    m = re.search(r"_(\d{4})_(\d{4}Metric)", footprint)
    if m:
        return m.group(1) if m.group(1) in IMPERIAL_ORDER else METRIC_TO_IMPERIAL.get(m.group(2))
    return None


def _rect_tuple(r: Rect) -> tuple[float, float, float, float]:
    return (r.x_mm, r.y_mm, r.x2, r.y2)


def _overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def run_dfm(plan: DesignPlan, fab: FabProfile) -> list[Finding]:
    out: list[Finding] = []
    min_idx = IMPERIAL_ORDER.index(fab.min_passive_size)
    for c in plan.components:
        if not c.verified or not c.footprint:
            continue
        size = passive_size(c.footprint)
        if size and IMPERIAL_ORDER.index(size) < min_idx:
            out.append(
                _F(
                    "DFM-PASSIVE-SIZE",
                    c.ref_des,
                    Severity.ERROR,
                    f"{c.ref_des} is {size}, smaller than the fab minimum {fab.min_passive_size}.",
                    refs=[c.ref_des],
                    fix=f"Use a {fab.min_passive_size} or larger package.",
                )
            )
        g = load_footprint(c.footprint)
        if g is not None:
            pitch = _min_pitch(g.pads)
            if pitch is not None and pitch < fab.min_pad_pitch_mm - 1e-6:
                out.append(
                    _F(
                        "DFM-PAD-PITCH",
                        c.ref_des,
                        Severity.ERROR,
                        f"{c.ref_des} pad pitch {pitch:.2f} mm is below the assembler minimum "
                        f"{fab.min_pad_pitch_mm:g} mm.",
                        refs=[c.ref_des],
                    )
                )
            if not g.smd and not fab.allow_tht:
                out.append(
                    _F(
                        "DFM-THT",
                        c.ref_des,
                        Severity.ERROR,
                        f"{c.ref_des} is through-hole but the profile disallows THT.",
                        refs=[c.ref_des],
                    )
                )
            for pad in g.pads:
                if pad.drill is not None and pad.drill < fab.min_drill_mm - 1e-6:
                    thermal = any(q.number == pad.number and q.kind == "smd" for q in g.pads)
                    out.append(
                        _F(
                            "DFM-DRILL",
                            c.ref_des,
                            Severity.WARNING if thermal else Severity.ERROR,
                            f"{c.ref_des} pad {pad.number} drill {pad.drill:g} mm < fab minimum "
                            f"{fab.min_drill_mm:g} mm"
                            + (
                                " (thermal vias inside the footprint's ground pad)."
                                if thermal
                                else "."
                            ),
                            refs=[c.ref_des],
                            fix="Use a fab that drills this size, or edit the footprint's "
                            "thermal vias to the fab minimum."
                            if thermal
                            else None,
                        )
                    )
                    break
        if "BGA" in c.footprint.upper() and not fab.allow_bga:
            out.append(
                _F(
                    "DFM-BGA",
                    c.ref_des,
                    Severity.ERROR,
                    f"{c.ref_des} is a BGA; profile {fab.name} does not allow BGAs.",
                    refs=[c.ref_des],
                )
            )
    tht = [
        c.ref_des
        for c in plan.components
        if c.footprint
        and (g := load_footprint(c.footprint)) is not None
        and not g.smd
        and c.category != "mechanical"
    ]
    if tht:
        out.append(
            _F(
                "DFM-MIXED-ASSEMBLY",
                "plan",
                Severity.INFO,
                f"{len(tht)} through-hole part(s) ({', '.join(tht[:8])}) need a second "
                "assembly step (wave/selective/hand soldering).",
            )
        )
    if plan.constraints is not None:
        for nc in plan.constraints.electrical.net_classes:
            if nc.trace_width_mm < fab.min_trace_mm - 1e-9:
                out.append(
                    _F(
                        "DFM-NETCLASS",
                        nc.name,
                        Severity.ERROR,
                        f"Net class {nc.name} width {nc.trace_width_mm:g} mm < fab min "
                        f"{fab.min_trace_mm:g} mm.",
                    )
                )
            if nc.clearance_mm < fab.min_space_mm - 1e-9:
                out.append(
                    _F(
                        "DFM-NETCLASS",
                        f"{nc.name}.clearance",
                        Severity.ERROR,
                        f"Net class {nc.name} clearance {nc.clearance_mm:g} mm < fab min "
                        f"{fab.min_space_mm:g} mm.",
                    )
                )
            if nc.via_drill_mm < fab.min_via_drill_mm - 1e-9:
                out.append(
                    _F(
                        "DFM-NETCLASS",
                        f"{nc.name}.via",
                        Severity.ERROR,
                        f"Net class {nc.name} via drill {nc.via_drill_mm:g} mm < fab min "
                        f"{fab.min_via_drill_mm:g} mm.",
                    )
                )
        if plan.constraints.mechanical.layer_count > fab.layers:
            out.append(
                _F(
                    "DFM-LAYERS",
                    "plan",
                    Severity.ERROR,
                    f"{plan.constraints.mechanical.layer_count} layers > profile {fab.layers}.",
                )
            )
        if plan.constraints.mechanical.max_component_height_mm:
            out.append(
                _F(
                    "DFM-HEIGHT",
                    "plan",
                    Severity.INFO,
                    "A maximum component height is set but the library has no height data; "
                    "check tall parts (connectors, relays, inductors) manually.",
                )
            )
    out += run_placement_checks(plan, fab)
    return out


def _min_pitch(pads: tuple[object, ...]) -> float | None:
    pts: list[tuple[str, float, float]] = []
    for p in pads:
        number = getattr(p, "number", "")
        kind = getattr(p, "kind", "")
        if not number or kind == "np_thru_hole":
            continue
        pts.append((number, getattr(p, "x", 0.0), getattr(p, "y", 0.0)))
    best: float | None = None
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            if pts[i][0] == pts[j][0]:
                continue
            d = math.dist(pts[i][1:], pts[j][1:])
            if d > 1e-6 and (best is None or d < best):
                best = d
    return best


def run_placement_checks(plan: DesignPlan, fab: FabProfile) -> list[Finding]:
    out: list[Finding] = []
    if plan.placement is None or plan.constraints is None:
        return out
    mech = plan.constraints.mechanical
    w, h = mech.outline.width_mm, mech.outline.height_mm
    edge_refs = {cp.ref: cp.edge for cp in mech.connectors}
    placements = [p for p in plan.placement.placements if p.bbox is not None]
    P = FindingCategory.PLACEMENT
    comps_by_ref = {c.ref_des: c for c in plan.components}

    def rects_of(p: object) -> list[tuple[float, float, float, float]]:
        ref = getattr(p, "ref", "")
        c = comps_by_ref.get(ref)
        g = load_footprint(c.footprint) if c and c.footprint else None
        if g is None:
            bb = getattr(p, "bbox", None)
            return [_rect_tuple(bb)] if bb is not None else []
        return placed_rects(
            g, getattr(p, "x_mm", 0.0), getattr(p, "y_mm", 0.0), getattr(p, "rotation_deg", 0.0)
        )

    shapes = {p.ref: rects_of(p) for p in placements}
    for i, a in enumerate(placements):
        assert a.bbox is not None
        ra = _rect_tuple(a.bbox)
        for b in placements[i + 1 :]:
            assert b.bbox is not None
            if any(_overlap(x, y) > 1e-6 for x in shapes[a.ref] for y in shapes[b.ref]):
                out.append(
                    _F(
                        "DFM-COURTYARD-OVERLAP",
                        f"{a.ref}-{b.ref}",
                        Severity.ERROR,
                        f"Courtyards of {a.ref} and {b.ref} overlap.",
                        cat=P,
                        refs=[a.ref, b.ref],
                    )
                )
        if ra[0] < -1e-6 or ra[1] < -1e-6 or ra[2] > w + 1e-6 or ra[3] > h + 1e-6:
            out.append(
                _F(
                    "DFM-OUTSIDE-BOARD",
                    a.ref,
                    Severity.ERROR,
                    f"{a.ref} courtyard extends outside the {w:g} x {h:g} mm board.",
                    cat=P,
                    refs=[a.ref],
                )
            )
        elif a.ref not in edge_refs:
            m = fab.component_to_edge_mm
            if ra[0] < m or ra[1] < m or ra[2] > w - m or ra[3] > h - m:
                out.append(
                    _F(
                        "DFM-EDGE-CLEARANCE",
                        a.ref,
                        Severity.WARNING,
                        f"{a.ref} is within {m:g} mm of the board edge.",
                        cat=P,
                        refs=[a.ref],
                    )
                )
        for ko in plan.constraints.electrical.rf_keepouts + mech.keepouts:
            owner = ko.name.split("_")[0]
            if owner == a.ref:
                continue
            if any(_overlap(x, _rect_tuple(ko.rect)) > 1e-6 for x in shapes[a.ref]):
                out.append(
                    _F(
                        "DFM-KEEPOUT",
                        f"{a.ref}:{ko.name}",
                        Severity.ERROR,
                        f"{a.ref} sits inside keepout {ko.name} ({ko.kind}).",
                        cat=P,
                        refs=[a.ref],
                    )
                )
    placed = {p.ref for p in plan.placement.placements}
    missing = [
        c.ref_des for c in plan.components if c.verified and c.footprint and c.ref_des not in placed
    ]
    if missing:
        out.append(
            _F(
                "DFM-UNPLACED",
                "plan",
                Severity.ERROR,
                f"{len(missing)} part(s) could not be placed: {', '.join(missing[:12])}.",
                cat=P,
                refs=missing,
                fix="Enlarge the board or remove parts.",
            )
        )
    pos = {p.ref: p for p in plan.placement.placements}
    comps = {c.ref_des: c for c in plan.components}
    for d in plan.constraints.electrical.decoupling:
        host = pos.get(d.ref)
        hc = comps.get(d.ref)
        if host is None or hc is None or not hc.footprint:
            continue
        g = load_footprint(hc.footprint)
        pin_xy = pad_position(g, d.pin, host.x_mm, host.y_mm, host.rotation_deg) if g else None
        if pin_xy is None:
            continue
        for cap in d.capacitors:
            cp = pos.get(cap)
            if cp is None or cp.bbox is None:
                continue
            cx = cp.bbox.x_mm + cp.bbox.w_mm / 2
            cy = cp.bbox.y_mm + cp.bbox.h_mm / 2
            dist = math.dist(pin_xy, (cx, cy))
            if dist > d.max_distance_mm:
                out.append(
                    _F(
                        "DFM-DECOUPLING-DISTANCE",
                        f"{cap}->{d.ref}.{d.pin}",
                        Severity.WARNING,
                        f"{cap} is {dist:.1f} mm from {d.ref} pin {d.pin} "
                        f"(limit {d.max_distance_mm:g} mm).",
                        cat=P,
                        refs=[cap, d.ref],
                        fix="Move the capacitor next to the pin.",
                    )
                )
    for ref, edge in edge_refs.items():
        p = pos.get(ref)
        if p is None or p.bbox is None:
            continue
        bb = p.bbox
        gap = {
            Edge.LEFT: bb.x_mm,
            Edge.RIGHT: w - bb.x2,
            Edge.TOP: bb.y_mm,
            Edge.BOTTOM: h - bb.y2,
        }[edge]
        if gap > 1.0:
            out.append(
                _F(
                    "DFM-CONNECTOR-EDGE",
                    ref,
                    Severity.WARNING,
                    f"{ref} should sit on the {edge.value} edge but is {gap:.1f} mm inside.",
                    cat=P,
                    refs=[ref],
                )
            )
    return out
