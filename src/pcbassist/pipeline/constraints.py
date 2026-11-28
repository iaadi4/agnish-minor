"""Deterministic constraints: rails + budgets, net classes, decoupling, outline, holes."""

from __future__ import annotations

import math

from ..checks.power import analyze_power
from ..fab import load_fab_profile
from ..kicad.footprint import load_footprint
from ..library import ComponentLibrary
from ..schemas import (
    BoardOutline,
    Component,
    ConnectorPlacement,
    Constraints,
    DecouplingRequirement,
    DesignPlan,
    Edge,
    ElectricalConstraints,
    FabProfile,
    Finding,
    FindingCategory,
    ImpedanceTarget,
    MechanicalConstraints,
    MountingHole,
    NetClassName,
    NetClassRule,
    Severity,
)

IPC_K_EXTERNAL = 0.048
IPC_K_INTERNAL = 0.024
MIL_PER_OZ = 1.378
MM_PER_MIL = 0.0254
HOLE_INSET_MM = 4.0


def ipc2221_width_mm(
    current_a: float, *, delta_t_c: float = 10.0, copper_oz: float = 1.0, external: bool = True
) -> float:
    """Minimum trace width (mm) for a current per IPC-2221: I = k * dT^0.44 * A^0.725."""
    if current_a <= 0:
        return 0.0
    k = IPC_K_EXTERNAL if external else IPC_K_INTERNAL
    area_mil2 = (current_a / (k * delta_t_c**0.44)) ** (1 / 0.725)
    width_mil = area_mil2 / (copper_oz * MIL_PER_OZ)
    return float(width_mil * MM_PER_MIL)


def _ceil_to(x: float, step: float) -> float:
    return round(math.ceil(round(x / step, 6)) * step, 3)


def choose_fab_profile(plan: DesignPlan, override: str | None = None) -> str:
    if override:
        return override
    req = plan.requirements
    if req is not None and (req.layer_count_target or 2) >= 4:
        return "generic_4layer"
    return "generic_2layer"


def _net_currents(plan: DesignPlan) -> dict[str, float]:
    """Worst-case current (mA) carried by each power net."""
    report = analyze_power(plan)
    cur: dict[str, float] = {name: b.peak_ma for name, b in report.budgets.items()}
    assert plan.netlist is not None
    for n in plan.netlist.nets:
        if n.net_class != NetClassName.POWER or n.name in cur:
            continue
        # series/derived power nets (VIN_RAW, SW_5V, MOTOR_OUT, RELAY_COIL): inherit
        best = 0.0
        for rail, amps in cur.items():
            if n.name.startswith(rail.strip("+")) or n.name.startswith(rail):
                best = max(best, amps)
        if n.name.startswith("SW_"):
            target = "+" + n.name[3:]
            best = max(best, cur.get(target, 0.0))
        if n.name.startswith("MOTOR") and plan.requirements:
            for a in plan.requirements.actuators:
                if a.kind == "dc_motor" and a.current_ma:
                    best = max(best, a.current_ma)
        if n.name.startswith("RELAY"):
            for c in plan.components:
                if c.category == "relay" and c.current.max_ma:
                    best = max(best, c.current.max_ma)
        cur[n.name] = best
    return cur


def build_constraints(
    plan: DesignPlan, lib: ComponentLibrary, fab_profile: str | None = None
) -> tuple[Constraints, list[Finding]]:
    del lib
    findings: list[Finding] = []
    fab = load_fab_profile(choose_fab_profile(plan, fab_profile))
    assert plan.netlist is not None
    nets = plan.netlist.nets

    # -- rails with budgets -------------------------------------------------------
    report = analyze_power(plan)
    rails = []
    for r in plan.power_tree:
        b = report.budgets.get(r.name)
        rails.append(r.model_copy(update={"budget_ma": round(b.peak_ma, 2) if b else None}))

    # -- net classes --------------------------------------------------------------
    currents = _net_currents(plan)
    sig_w = max(fab.min_trace_mm, 0.2)
    # Clearance = fab minimum: fine-pitch library footprints (0.5 mm LGA/QFN) have pad gaps
    # of ~0.15 mm, so a larger net-class clearance would flag the footprints themselves.
    clr = fab.min_space_mm
    via_d, via_drill = fab.min_via_diameter_mm, fab.min_via_drill_mm
    classes: list[NetClassRule] = []
    power_nets = [n.name for n in nets if n.net_class == NetClassName.POWER]
    pmax = max((currents.get(n, 0.0) for n in power_nets), default=0.0)
    p_w = max(_ceil_to(ipc2221_width_mm(pmax / 1000 * 1.25, copper_oz=fab.copper_oz), 0.05), 0.4)
    classes.append(
        NetClassRule(
            name="Default",
            kind=NetClassName.SIGNAL,
            trace_width_mm=sig_w,
            clearance_mm=clr,
            via_diameter_mm=via_d,
            via_drill_mm=via_drill,
            nets=[
                n.name for n in nets if n.net_class in (NetClassName.SIGNAL, NetClassName.ANALOG)
            ],
            rationale=f"fab minimum {fab.min_trace_mm} mm rounded up for yield",
        )
    )
    if power_nets:
        classes.append(
            NetClassRule(
                name="Power",
                kind=NetClassName.POWER,
                trace_width_mm=p_w,
                clearance_mm=clr,
                via_diameter_mm=max(via_d, 0.8),
                via_drill_mm=max(via_drill, 0.4),
                nets=power_nets,
                max_current_ma=round(pmax, 1),
                rationale=(
                    f"IPC-2221 external, dT=10C, {fab.copper_oz:g} oz: {pmax:.0f} mA x1.25 margin "
                    f"-> {ipc2221_width_mm(pmax / 1000 * 1.25, copper_oz=fab.copper_oz):.3f} mm "
                    f"(min 0.4 mm)"
                ),
            )
        )
    gnd = [n.name for n in nets if n.net_class == NetClassName.GROUND]
    if gnd:
        classes.append(
            NetClassRule(
                name="Ground",
                kind=NetClassName.GROUND,
                trace_width_mm=max(p_w, 0.4),
                clearance_mm=clr,
                via_diameter_mm=max(via_d, 0.6),
                via_drill_mm=max(via_drill, 0.3),
                nets=gnd,
                rationale="return currents; use a pour on B.Cu, short stubs elsewhere",
            )
        )
    hs = [n.name for n in nets if n.net_class == NetClassName.HIGH_SPEED]
    impedance: list[ImpedanceTarget] = []
    if hs:
        classes.append(
            NetClassRule(
                name="USB",
                kind=NetClassName.HIGH_SPEED,
                trace_width_mm=max(fab.min_trace_mm, 0.2),
                clearance_mm=max(fab.min_space_mm, 0.15),
                via_diameter_mm=via_d,
                via_drill_mm=via_drill,
                nets=hs,
                rationale="USB 2.0 FS pair; keep short and matched",
            )
        )
        impedance.append(
            ImpedanceTarget(
                target="USB",
                ohms=90,
                kind="differential",
                notes="USB 2.0 90 Ohm +/-15%; for Full Speed (12 Mbit/s) short traces dominate. "
                "Use the fab's stackup calculator for the exact width/gap.",
            )
        )
    rf = [n.name for n in nets if n.net_class == NetClassName.RF]
    if rf:
        impedance.append(ImpedanceTarget(target="RF", ohms=50, notes="coplanar waveguide to U.FL"))

    # -- decoupling -----------------------------------------------------------------
    groups: dict[tuple[str, str], list[str]] = {}
    for c in plan.components:
        if (
            c.support_for
            and c.support_pin
            and c.ref_des.startswith("C")
            and c.role.lower().startswith(("decoupling", "input capacitor", "output capacitor"))
        ):
            groups.setdefault((c.support_for, c.support_pin), []).append(c.ref_des)
    comps = {c.ref_des: c for c in plan.components}
    decoupling = []
    for (ref, pin), caps in sorted(groups.items()):
        host = comps.get(ref)
        big = host is not None and host.category in ("mcu_wireless", "motor_driver", "buck")
        decoupling.append(
            DecouplingRequirement(
                ref=ref, pin=pin, capacitors=sorted(caps), max_distance_mm=5.0 if big else 3.0
            )
        )

    electrical = ElectricalConstraints(
        rails=rails,
        net_classes=classes,
        decoupling=decoupling,
        impedance=impedance,
        notes=[
            "Trace widths are minimums; wider is fine where space allows.",
            f"Fab profile: {fab.name} - {fab.description}.",
        ],
    )

    # -- mechanical ---------------------------------------------------------------
    outline, size_note = _outline(plan, fab)
    if size_note:
        findings.append(size_note)
    holes = _mounting_holes(plan.components, outline)
    if len(holes) < sum(1 for c in plan.components if c.category == "mechanical"):
        findings.append(
            Finding(
                id="CON-HOLES:plan",
                rule="CON-HOLES",
                severity=Severity.WARNING,
                category=FindingCategory.DFM,
                message="Board too small for all requested mounting holes at the corners.",
            )
        )
    connectors = _connector_edges(plan)
    layer_target = plan.requirements.layer_count_target if plan.requirements else None
    if layer_target and layer_target > fab.layers:
        findings.append(
            Finding(
                id="CON-LAYERS:plan",
                rule="CON-LAYERS",
                severity=Severity.ERROR,
                category=FindingCategory.DFM,
                message=f"{layer_target} layers requested but fab profile {fab.name} is "
                f"{fab.layers}-layer.",
            )
        )
    max_h = plan.requirements.size.max_component_height_mm if plan.requirements else None
    mech = MechanicalConstraints(
        outline=outline,
        layer_count=fab.layers,
        mounting_holes=holes,
        connectors=connectors,
        max_component_height_mm=max_h,
        fab_profile=fab.name,
    )
    return Constraints(electrical=electrical, mechanical=mech), findings


def courtyard_area(components: list[Component]) -> float:
    """Sum of courtyard areas (+0.25 mm spacing all round) of the given parts, mm^2."""
    total = 0.0
    for c in components:
        g = load_footprint(c.footprint) if c.footprint else None
        if g is not None:
            total += sum((r[2] - r[0] + 0.5) * (r[3] - r[1] + 0.5) for r in g.rects)
    return total


def _outline(plan: DesignPlan, fab: FabProfile) -> tuple[BoardOutline, Finding | None]:
    req = plan.requirements
    area = courtyard_area([c for c in plan.components if c.pins_known])
    if req is not None and req.size.max_width_mm and req.size.max_height_mm:
        w, h = req.size.max_width_mm, req.size.max_height_mm
        note = None
        if w * h < area * 1.3:
            note = Finding(
                id="CON-BOARD-AREA:plan",
                rule="CON-BOARD-AREA",
                severity=Severity.ERROR if w * h < area else Severity.WARNING,
                category=FindingCategory.PLACEMENT,
                message=(
                    f"Requested board {w:g} x {h:g} mm = {w * h:.0f} mm^2 but the parts' "
                    f"courtyards need about {area:.0f} mm^2 before any routing space."
                ),
                suggested_fix="Enlarge the board, use a double-sided assembly, or drop parts.",
            )
        return BoardOutline(width_mm=w, height_mm=h, thickness_mm=fab.board_thickness_mm), note
    # Auto-size: 2.2x courtyard area for routing, 4:3 aspect, rounded up to 5 mm.
    target = max(area * 2.2, 100.0)
    w = _ceil_to(math.sqrt(target * 4 / 3), 5.0)
    h = _ceil_to(target / w, 5.0)
    widest = max(
        (
            g.width
            for c in plan.components
            if c.footprint and (g := load_footprint(c.footprint)) is not None
        ),
        default=0.0,
    )
    tallest = max(
        (
            g.height
            for c in plan.components
            if c.footprint and (g := load_footprint(c.footprint)) is not None
        ),
        default=0.0,
    )
    w = max(w, _ceil_to(widest + 2, 5.0))
    h = max(h, _ceil_to(tallest + 2, 5.0))
    if req is not None and req.size.max_width_mm:
        w = min(w, req.size.max_width_mm)
    if req is not None and req.size.max_height_mm:
        h = min(h, req.size.max_height_mm)
    return BoardOutline(width_mm=w, height_mm=h, thickness_mm=fab.board_thickness_mm), None


def _mounting_holes(components: list[Component], outline: BoardOutline) -> list[MountingHole]:
    refs = [c for c in components if c.category == "mechanical"]
    w, h = outline.width_mm, outline.height_mm
    corners = [
        (HOLE_INSET_MM, HOLE_INSET_MM),
        (w - HOLE_INSET_MM, HOLE_INSET_MM),
        (HOLE_INSET_MM, h - HOLE_INSET_MM),
        (w - HOLE_INSET_MM, h - HOLE_INSET_MM),
    ]
    if w < 4 * HOLE_INSET_MM or h < 4 * HOLE_INSET_MM:
        return []
    out = []
    for c, (x, y) in zip(refs, corners, strict=False):
        drill = c.key_specs.get("drill_mm", 3.2)
        out.append(
            MountingHole(
                ref=c.ref_des,
                x_mm=x,
                y_mm=y,
                drill_mm=float(drill) if isinstance(drill, (int, float)) else 3.2,
            )
        )
    return out


#: Which board edge a connector goes on, by category / role. Inputs left, outputs right.
def _connector_edges(plan: DesignPlan) -> list[ConnectorPlacement]:
    out: list[ConnectorPlacement] = []
    kinds = {b.id: b.kind for b in plan.architecture.blocks} if plan.architecture else {}
    for c in plan.components:
        cat = c.category or ""
        kind = kinds.get(c.block_id or "", "")
        if cat in ("connector_usb", "connector_power") or (
            cat == "connector_terminal" and kind == "power_input"
        ):
            out.append(ConnectorPlacement(ref=c.ref_des, edge=Edge.LEFT, reason="power input"))
        elif cat == "connector_terminal":
            out.append(ConnectorPlacement(ref=c.ref_des, edge=Edge.RIGHT, reason="field wiring"))
        elif cat == "connector_battery":
            out.append(ConnectorPlacement(ref=c.ref_des, edge=Edge.BOTTOM, reason="battery lead"))
        elif cat == "connector_header":
            out.append(ConnectorPlacement(ref=c.ref_des, edge=Edge.BOTTOM, reason="cable access"))
        elif cat == "mcu_wireless" and _has_pcb_antenna(c):
            out.append(
                ConnectorPlacement(
                    ref=c.ref_des, edge=Edge.TOP, reason="PCB antenna must overhang/face the edge"
                )
            )
    return out


def _has_pcb_antenna(c: Component) -> bool:
    """Modules with an integrated antenna (PCB trace or chip) belong at a board edge."""
    ant = str(c.key_specs.get("antenna", "")).lower()
    return any(k in ant for k in ("pcb", "trace", "chip"))


def run_constraints_stage(plan: DesignPlan, lib: ComponentLibrary, fab_profile: str | None) -> None:
    cons, findings = build_constraints(plan, lib, fab_profile)
    if plan.constraints is not None and plan.constraints.locked:
        findings.append(
            Finding(
                id="CON-LOCKED:plan",
                rule="CON-LOCKED",
                severity=Severity.INFO,
                category=FindingCategory.DFM,
                message="Constraints are locked; regenerated values were not applied.",
            )
        )
        cons = plan.constraints.model_copy(
            update={
                "electrical": plan.constraints.electrical.model_copy(
                    update={"rails": cons.electrical.rails}
                )
            }
        )
    plan.constraints = cons
    plan.findings += findings
